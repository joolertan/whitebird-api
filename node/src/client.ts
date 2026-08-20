import type { CodeProvider } from './auth.js';
import { type ApiRequest, Core } from './core.js';
import { Resources } from './generated/index.js';
import { type Environment, OAUTH, SERVICE_URLS, type ServiceName } from './generated/services.js';
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
        store: options.tokenStore ?? new MemoryTokenStore(),
        username: options.username,
        password: options.password,
        otp: options.otp,
      }),
    );
  }

  /**
   * Operation MFA token, sent as `x-mfa-token` with every request while set. Sensitive
   * operations such as withdrawals need it; set it to `undefined` to stop sending it.
   */
  get mfaToken(): string | undefined {
    return this._client.mfaToken;
  }

  set mfaToken(token: string | undefined) {
    this._client.mfaToken = token;
  }

  /**
   * Logs in now instead of on the first authenticated call.
   *
   * @throws {@link MFARequiredError} if the account has two-factor authentication and no `otp`
   * is configured.
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
