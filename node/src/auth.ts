import { APIError, AuthenticationError, MFARequiredError } from './errors.js';
import { type HttpClient, readJson } from './http.js';
import { jwtExpiry, type TokenStore, type Tokens } from './tokens.js';

/**
 * Two-factor code for logging in: a code, or a function returning a fresh one for every login,
 * such as `totp(secret)` from `whitebird-api/otp`.
 */
export type CodeProvider = string | (() => string | Promise<string>);

export interface SessionConfig {
  http: HttpClient;
  /** Base URL of the authorization server. */
  authUrl: string;
  tokenPath: string;
  /** Value of the `Authorization: Basic` header of the OAuth client. */
  basic: string;
  headers: Record<string, string>;
  store: TokenStore;
  username?: string | undefined;
  password?: string | undefined;
  otp?: CodeProvider | undefined;
}

interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  mfaEnabled?: boolean;
}

const MFA_CHECK_PATH = '/api/v1/client/mfa';
const REFRESH_MARGIN = 30_000;
const MFA_HINT =
  'Two-factor authentication is enabled for this account: pass `otp` (a code, a function ' +
  "returning one, or totp(secret) from 'whitebird-api/otp')";

/**
 * Owns the OAuth tokens: logs in lazily, refreshes shortly before expiry or after a 401, and
 * makes concurrent callers share a single login or refresh.
 */
export class Session {
  readonly #config: SessionConfig;
  #tokens: Tokens | undefined;
  #restoring: Promise<void> | undefined;
  #renewing: Promise<Tokens> | undefined;

  constructor(config: SessionConfig) {
    this.#config = config;
  }

  /** Returns an access token that is valid for at least 30 more seconds. */
  async accessToken(): Promise<string> {
    this.#restoring ??= this.#restore();
    await this.#restoring;
    const tokens = this.#tokens;
    if (tokens && !expiresSoon(tokens)) return tokens.accessToken;
    return (await this.#renew(() => this.#refreshOrLogin())).accessToken;
  }

  /** Returns a replacement for an access token the server rejected. */
  async renew(rejected: string): Promise<string> {
    const tokens = this.#tokens;
    // Another request may have replaced the token already.
    if (!this.#renewing && tokens && tokens.accessToken !== rejected) return tokens.accessToken;
    return (await this.#renew(() => this.#refreshOrLogin())).accessToken;
  }

  /** Starts a new session with the password grant. */
  async login(): Promise<void> {
    await this.#renew(() => this.#passwordGrant());
  }

  /** Forgets the session, also in the token store. */
  async logout(): Promise<void> {
    this.#tokens = undefined;
    await this.#config.store.clear();
  }

  async #restore(): Promise<void> {
    const saved = await this.#config.store.load();
    // A login that finished while the store was loading wins over the saved session.
    if (!saved || this.#tokens) return;
    this.#tokens = { ...saved, expiresAt: saved.expiresAt ?? jwtExpiry(saved.accessToken) };
  }

  #renew(obtain: () => Promise<Tokens>): Promise<Tokens> {
    this.#renewing ??= obtain()
      .then(async (tokens) => {
        this.#tokens = tokens;
        await this.#config.store.save(tokens);
        return tokens;
      })
      .finally(() => {
        this.#renewing = undefined;
      });
    return this.#renewing;
  }

  async #refreshOrLogin(): Promise<Tokens> {
    const refreshToken = this.#tokens?.refreshToken;
    if (refreshToken) {
      try {
        return await this.#refresh(refreshToken);
      } catch (error) {
        if (!(error instanceof APIError) || !this.#config.password) throw error;
      }
    }
    return this.#passwordGrant();
  }

  async #refresh(refreshToken: string): Promise<Tokens> {
    const response = await this.#token({
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
    });
    if (!response.access_token) {
      throw new AuthenticationError('The token refresh returned no access token', {
        body: response,
      });
    }
    return toTokens(response.access_token, response, refreshToken);
  }

  async #passwordGrant(): Promise<Tokens> {
    const { username, password, otp } = this.#config;
    if (!username || !password) {
      throw new AuthenticationError(
        'Not logged in: pass `username` and `password`, or a `tokenStore` holding a session',
      );
    }
    const code = typeof otp === 'function' ? await otp() : otp;
    // Asking first gives a clear error instead of an opaque invalid_grant.
    const mfaEnabled = code === undefined ? await this.#mfaEnabled(username) : undefined;
    if (mfaEnabled) throw new MFARequiredError(MFA_HINT);

    let response: TokenResponse;
    try {
      response = await this.#token({
        grant_type: 'password',
        username: username.toLowerCase(),
        password,
        ...(code !== undefined && { code }),
      });
    } catch (error) {
      // Without the MFA check result an invalid_grant may just mean a code was expected.
      const unknownMfa = code === undefined && mfaEnabled === undefined;
      if (
        unknownMfa &&
        error instanceof AuthenticationError &&
        error.errorCode === 'invalid_grant'
      ) {
        throw new MFARequiredError(`${error.message}. ${MFA_HINT}`, {
          status: error.status,
          errorCode: error.errorCode,
          body: error.body,
          headers: error.headers,
        });
      }
      throw error;
    }

    if (response.access_token) return toTokens(response.access_token, response);
    if (response.mfaEnabled) {
      throw code === undefined
        ? new MFARequiredError(MFA_HINT, { status: 200, body: response })
        : new AuthenticationError('The two-factor code was rejected', {
            status: 200,
            body: response,
          });
    }
    throw new AuthenticationError('The login returned no access token', {
      status: 200,
      body: response,
    });
  }

  async #mfaEnabled(username: string): Promise<boolean | undefined> {
    const { http, authUrl, headers } = this.#config;
    try {
      const result = await http.send(
        authUrl + MFA_CHECK_PATH,
        {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username.toLowerCase() }),
        },
        {},
        readJson,
      );
      return (result as { mfaEnabled?: unknown } | undefined)?.mfaEnabled === true;
    } catch (error) {
      if (error instanceof APIError) return undefined;
      throw error;
    }
  }

  async #token(form: Record<string, string>): Promise<TokenResponse> {
    const { http, authUrl, tokenPath, basic, headers } = this.#config;
    const response = await http.send(
      authUrl + tokenPath,
      {
        method: 'POST',
        headers: { ...headers, Authorization: `Basic ${basic}` },
        body: new URLSearchParams(form),
      },
      {},
      readJson,
    );
    return (response ?? {}) as TokenResponse;
  }
}

function toTokens(accessToken: string, response: TokenResponse, refreshToken?: string): Tokens {
  const lifetime = response.expires_in;
  return {
    accessToken,
    refreshToken: response.refresh_token ?? refreshToken,
    expiresAt:
      jwtExpiry(accessToken) ?? (lifetime === undefined ? undefined : Date.now() + lifetime * 1000),
  };
}

function expiresSoon(tokens: Tokens): boolean {
  return tokens.expiresAt !== undefined && tokens.expiresAt - Date.now() < REFRESH_MARGIN;
}
