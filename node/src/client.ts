import type { CodeProvider } from './auth.js';
import type { CaptchaSolver } from './captcha.js';
import { type ApiRequest, Core } from './core.js';
import { Resources } from './generated/index.js';
import {
  type Environment,
  OAUTH,
  OPERATION_MFA,
  SERVICE_URLS,
  type ServiceName,
} from './generated/services.js';
import type { RequestOptions } from './http.js';
import { MemoryTokenStore, type TokenStore } from './tokens.js';

/** Options of the {@link Whitebird} client. */
export interface WhitebirdOptions {
  /** Account email, used to log in. */
  username?: string;
  /** Account password, used to log in and to start a new session when a refresh fails. */
  password?: string;
  /**
   * Two-factor code for accounts with 2FA: a code, a function returning one, or
   * `totp(secret)` from `whitebird-api/otp` to compute codes from the authenticator secret.
   */
  otp?: CodeProvider;
  /**
   * Solves the slider captcha that accounts without two-factor authentication get when logging
   * in. Without it such a login fails with {@link CaptchaRequiredError} when the captcha is
   * enforced.
   */
  captcha?: CaptchaSolver;
  /**
   * Platform contour: `demo` is the sandbox for trading. Services without a demo host use
   * production.
   * @defaultValue `'production'`
   */
  environment?: Environment;
  /** Base URLs per service, overriding the environment, e.g. `{ brokerage: 'https://...' }`. */
  baseUrls?: Partial<Record<ServiceName | (string & {}), string>>;
  /** `fetch` implementation, for proxies, testing or instrumentation. @defaultValue `globalThis.fetch` */
  fetch?: typeof fetch;
  /** Where the session is kept between runs. @defaultValue a new {@link MemoryTokenStore} */
  tokenStore?: TokenStore;
  /** Retries of idempotent requests on network errors, 429, 502, 503 and 504. @defaultValue 2 */
  maxRetries?: number;
  /** Request timeout in milliseconds; `Infinity` disables it. @defaultValue 30000 */
  timeout?: number;
}

/** Arguments of {@link Whitebird.request}. */
export interface RawRequest
  extends RequestOptions,
    Pick<ApiRequest, 'query' | 'body' | 'bodyType' | 'responseType' | 'auth' | 'envelope'> {}

/**
 * Client of the Whitebird API.
 *
 * Logs in on the first authenticated call (or when {@link Whitebird.login} is called), refreshes
 * the access token before it expires, and retries idempotent requests on transient failures.
 * Endpoints are grouped into resources, such as `client.brokerage.otcOrders`.
 *
 * Amounts are JavaScript numbers: values with more than 15 significant digits lose precision.
 * Timestamps are ISO 8601 strings.
 *
 * @example
 * ```ts
 * import { Whitebird } from 'whitebird-api';
 * import { totp } from 'whitebird-api/otp';
 *
 * const client = new Whitebird({
 *   username: 'me@example.com',
 *   password: process.env.WHITEBIRD_PASSWORD,
 *   otp: totp(process.env.WHITEBIRD_TOTP_SECRET!),
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
   * does the client on its own when `otp` is configured. Set it to `undefined` to stop sending it.
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
   * With `otp` configured there is no need to call this: operations that need a token get one
   * before they are sent, and a new one when theirs is refused.
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
   * Logs in now instead of on the first authenticated call.
   *
   * @throws {@link MFARequiredError} if the account has two-factor authentication and no `otp`
   * is configured.
   * @throws {@link CaptchaRequiredError} if a captcha must be solved and no `captcha` solver is
   * configured, or it failed three times.
   * @throws {@link AuthenticationError} if the credentials or the two-factor code are rejected.
   */
  login(): Promise<void> {
    return this._client.session.login();
  }

  /** Forgets the session and clears the token store. */
  logout(): Promise<void> {
    return this._client.session.logout();
  }

  /**
   * Calls an endpoint this SDK has no method for yet, with the same authentication, retries
   * and error handling as the generated methods.
   *
   * @param service - Service key, such as `brokerage`.
   * @param method - HTTP method.
   * @param path - Path from the host root, such as `/api/v1/brokerage/broker/balances`.
   * @returns The decoded response: parsed JSON by default.
   * @throws {@link APIError} (or a subclass) when the API rejects the request.
   * @throws {@link TransportError} when no response arrives.
   *
   * @example
   * ```ts
   * const methods = await client.request<{ id: string }[]>(
   *   'brokerage',
   *   'GET',
   *   '/api/v1/brokerage/payment/methods',
   * );
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
        responseType,
        auth,
        envelope,
      },
      options,
    );
  }
}
