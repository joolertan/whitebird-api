import type { CodeProvider } from './auth.js';
import type { CaptchaSolver } from './captcha.js';
import { type ApiRequest, Core } from './core.js';
import { Resources } from './generated/index.js';
import {
  type Environment,
  LOGOUT,
  OAUTH,
  OPERATION_MFA,
  SERVICE_URLS,
  type ServiceName,
} from './generated/services.js';
import { DECIMALS } from './generated/shapes.js';
import type { RequestOptions } from './http.js';
import { MemoryTokenStore, type TokenStore } from './tokens.js';

/** Options of the {@link Whitebird} client. */
export interface WhitebirdOptions {
  /** Account email, used to log in. */
  username?: string | undefined;
  /** Account password, used to log in and to start a new session when a refresh fails. */
  password?: string | undefined;
  /**
   * Two-factor code for accounts with 2FA, asked for at login and to confirm operations such as
   * withdrawals: a code, a function returning one (such as a prompt), or `totp(secret)` from
   * `whitebird-api/otp` to compute codes from the authenticator secret.
   */
  otp?: CodeProvider | undefined;
  /**
   * Solves the slider captcha that accounts without two-factor authentication get when logging
   * in. Without it such a login fails with {@link CaptchaRequiredError} before the password is
   * sent.
   */
  captcha?: CaptchaSolver | undefined;
  /**
   * Platform contour: `demo` is the sandbox for trading. Services without a demo host use
   * production.
   * @defaultValue `'production'`
   */
  environment?: Environment | undefined;
  /** Base URLs per service, overriding the environment, e.g. `{ brokerage: 'https://...' }`. */
  baseUrls?: Partial<Record<ServiceName | (string & {}), string>> | undefined;
  /**
   * Language of messages and texts in responses, sent as `Accept-Language` with every request,
   * such as `'en'` or `'ru'`. By default no preference is sent.
   */
  language?: string | undefined;
  /** `fetch` implementation, for proxies, testing or instrumentation. @defaultValue `globalThis.fetch` */
  fetch?: typeof fetch | undefined;
  /** Where the session is kept between runs. @defaultValue a new {@link MemoryTokenStore} */
  tokenStore?: TokenStore | undefined;
  /**
   * Retries of a request on transient failures: network errors, timeouts, 429, 502, 503 and 504
   * for GET, HEAD and OPTIONS; for other methods only failed connections and 429.
   * @defaultValue 2
   */
  maxRetries?: number | undefined;
  /** Request timeout in milliseconds; `Infinity` disables it. @defaultValue 30000 */
  timeout?: number | undefined;
}

/** Arguments of {@link Whitebird.request}. */
export interface RawRequest
  extends RequestOptions,
    Pick<ApiRequest, 'query' | 'body' | 'bodyType' | 'responseType' | 'auth' | 'envelope'> {}

/**
 * Client of the Whitebird API.
 *
 * Logs in on the first authenticated call (or when {@link Whitebird.login} is called), refreshes
 * the access token before it expires, and retries requests on transient failures when that is
 * safe. Endpoints are grouped into resources, such as `client.brokerage.otcOrders`.
 *
 * Decimal amounts are strings in responses, with exactly the digits the API sent, and may be
 * given as strings or numbers in requests; strings are sent with their exact digits. Timestamps
 * are ISO 8601 strings in responses; in requests they may also be `Date`s.
 *
 * @example
 * ```ts
 * import { Whitebird } from 'whitebird-api';
 * import { totp } from 'whitebird-api/otp';
 *
 * const client = new Whitebird({
 *   username: 'me@example.com',
 *   password: process.env.WHITEBIRD_PASSWORD,
 *   otp: totp(process.env.WHITEBIRD_TOTP_SECRET ?? ''),
 * });
 *
 * const balances = await client.brokerage.balances();
 * for await (const order of client.brokerage.otcOrders.list({ size: 100 })) {
 *   console.log(order.uuid, order.status);
 * }
 * ```
 */
export class Whitebird extends Resources {
  constructor(options: WhitebirdOptions = {}) {
    const environment = options.environment ?? 'production';
    const defaultUrls = Object.fromEntries(
      Object.entries(SERVICE_URLS).map(([service, urls]) => [service, urls[environment]]),
    );
    const fetchImpl = options.fetch ?? globalThis.fetch;
    super(
      new Core({
        // Called as a plain function: browsers reject fetch invoked with a foreign `this`.
        fetch: (input, init) => fetchImpl(input, init),
        timeout: options.timeout ?? 30_000,
        maxRetries: options.maxRetries ?? 2,
        baseUrls: { ...defaultUrls, ...options.baseUrls },
        authService: OAUTH.service,
        tokenPath: OAUTH.tokenPath,
        basic: OAUTH.basic,
        mfaStatusPath: OAUTH.mfaStatusPath,
        captchaPath: OAUTH.captchaPath,
        operationMfa: OPERATION_MFA,
        logout: LOGOUT,
        language: options.language,
        shapes: DECIMALS,
        store: options.tokenStore ?? new MemoryTokenStore(),
        username: options.username,
        password: options.password,
        otp: options.otp,
        captcha: options.captcha,
      }),
    );
  }

  /**
   * Operation MFA token, sent as `x-mfa-token` with every request while set. Sensitive
   * operations such as withdrawals need it: {@link Whitebird.confirmOperation} sets it, and so
   * does the client on its own when such an operation is refused and `otp` is configured. Set it
   * to `undefined` to stop sending it.
   */
  get mfaToken(): string | undefined {
    return this._client.mfaToken;
  }

  set mfaToken(token: string | undefined) {
    this._client.mfaToken = token;
  }

  /**
   * Confirms a sensitive operation with a two-factor code: exchanges the code for an operation
   * MFA token, which is then sent with every request (see {@link Whitebird.mfaToken}).
   *
   * With `otp` configured there is no need to call this: when the API refuses an operation for
   * want of a token, the client gets one with a code from `otp` and sends the operation again.
   * Concurrent operations refused together share one confirmation.
   *
   * @param operation - Operation to confirm, such as `WITHDRAWAL`.
   * @param code - Current two-factor code. @defaultValue the next code of `otp`
   * @returns The operation MFA token.
   * @throws {@link MFARequiredError} if no code is given and no `otp` is configured.
   * @throws {@link APIError} (or a subclass) when the code is rejected.
   *
   * @example
   * ```ts
   * await client.confirmOperation('WITHDRAWAL', '123456');
   * await client.balance.withdrawals.createCrypto({ accountType: 'WALLET', calculationId });
   * ```
   */
  confirmOperation(operation: string, code?: string): Promise<string> {
    return this._client.confirmOperation(operation, code);
  }

  /**
   * Logs in now instead of on the first authenticated call: asks
   * whether the account has two-factor authentication, sends the password (with a solved captcha
   * when it has not) and, when Whitebird asks for it, the two-factor code.
   *
   * @param code - Two-factor code for this login. @defaultValue the next code of `otp`
   * @param options - `signal` stops waiting, for example for a code nobody enters.
   * @throws {@link MFARequiredError} if Whitebird asks for a two-factor code and none is given
   * or configured.
   * @throws {@link CaptchaRequiredError} if the account has no two-factor authentication and no
   * `captcha` solver is configured, or the solved captchas were rejected three times.
   * @throws {@link AuthenticationError} if there is no account with this username, or the
   * password or the code is wrong.
   */
  login(code?: string, options: Pick<RequestOptions, 'signal'> = {}): Promise<void> {
    return this._client.session.login(code, options.signal);
  }

  /**
   * Signs out: revokes the session on the server when there is one (failures are ignored), then
   * forgets it, its operation MFA token and the tokens in the token store. Logins and refreshes
   * in progress no longer count. The next authenticated call logs in again.
   */
  logout(options?: RequestOptions): Promise<void> {
    return this._client.logout(options);
  }

  /**
   * Calls an endpoint this SDK has no method for yet, with the same authentication, retries
   * and error handling as the generated methods.
   *
   * @param service - Service key, such as `brokerage`.
   * @param method - HTTP method.
   * @param path - Path from the host root, such as `/api/v1/brokerage/broker/balances`.
   * @returns The decoded response: parsed JSON, or the text when the body is not JSON. Numbers
   * are JavaScript numbers here, since nothing tells which ones are decimals.
   * @throws {@link APIError} (or a subclass) when the API rejects the request.
   * @throws {@link TransportError} when no response arrives.
   *
   * @example
   * ```ts
   * // An endpoint added to Whitebird after this version of the SDK:
   * const result = await client.request('brokerage', 'POST', '/api/v1/brokerage/new-endpoint', {
   *   body: { from: new Date('2026-01-01'), amount: '0.15' },
   * });
   * ```
   */
  request<T = unknown>(
    service: ServiceName | (string & {}),
    method: string,
    path: string,
    init: RawRequest = {},
  ): Promise<T> {
    const { query, body, bodyType, responseType, auth, envelope, ...options } = init;
    return this._client.call<T>(
      {
        service,
        method: method.toUpperCase(),
        path,
        query,
        body,
        bodyType,
        responseType: responseType ?? 'auto',
        auth,
        envelope,
      },
      options,
    );
  }
}
