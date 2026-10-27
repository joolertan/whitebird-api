import { type CaptchaSolver, solveCaptcha } from './captcha.js';
import {
  APIError,
  type APIErrorDetails,
  AuthenticationError,
  CaptchaRequiredError,
  MFARequiredError,
} from './errors.js';
import { type HttpClient, readJson } from './http.js';
import { jwtExpiry, type TokenStore, type Tokens } from './tokens.js';

/**
 * Two-factor code for logging in: a code, or a function returning a fresh one for every login,
 * such as `totp(secret)` from `whitebird-api/otp`.
 */
export type CodeProvider = string | (() => string | Promise<string>);

/** Returns the current code of a provider. */
export async function resolveCode(otp: CodeProvider | undefined): Promise<string | undefined> {
  return typeof otp === 'function' ? otp() : otp;
}

export interface SessionConfig {
  http: HttpClient;
  /** Base URL of the authorization server. */
  authUrl: string;
  tokenPath: string;
  mfaStatusPath: string;
  captchaPath: string;
  /** Value of the `Authorization: Basic` header of the OAuth client. */
  basic: string;
  headers: Record<string, string>;
  store: TokenStore;
  username?: string | undefined;
  password?: string | undefined;
  otp?: CodeProvider | undefined;
  captcha?: CaptchaSolver | undefined;
}

interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  mfaEnabled?: boolean;
}

const REFRESH_MARGIN = 30_000;
const CAPTCHA_ATTEMPTS = 3;
const MFA_HINT =
  'Two-factor authentication is enabled for this account: pass `otp` (a code, a function ' +
  "returning one, or totp(secret) from 'whitebird-api/otp')";
const CAPTCHA_HINT = 'Logging in needs a solved captcha: pass a `captcha` solver';
const NO_MFA_CAPTCHA =
  'The account has no two-factor authentication, so Whitebird asks for a captcha at sign-in: ' +
  'configure a captcha solver (the captcha option) or enable two-factor authentication';

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
    const { username, password, otp, captcha } = this.#config;
    if (!username || !password) {
      throw new AuthenticationError(
        'Not logged in: pass `username` and `password`, or a `tokenStore` holding a session',
      );
    }
    // Like the web app: accounts with 2FA are asked for a code, the others for a captcha.
    const mfaEnabled = await this.#mfaEnabled(username);
    if (mfaEnabled === false && !captcha) throw new CaptchaRequiredError(NO_MFA_CAPTCHA);
    const code = mfaEnabled === false ? undefined : await resolveCode(otp);
    if (mfaEnabled && code === undefined) throw new MFARequiredError(MFA_HINT);
    const form = {
      grant_type: 'password',
      username: username.toLowerCase(),
      password,
      ...(code !== undefined && { code }),
    };

    // With an unknown status the captcha is solved only once the server asks for it.
    const solver = code === undefined ? captcha : undefined;
    let solve = mfaEnabled === false;
    for (let attempt = 1; ; attempt++) {
      const answer = solver && solve ? await this.#solveCaptcha(solver) : undefined;
      try {
        return await this.#passwordToken(form, answer, mfaEnabled);
      } catch (error) {
        const rejected = error instanceof APIError ? error : undefined;
        if (!rejected?.errorCode?.toUpperCase().includes('CAPTCHA')) throw error;
        if (!solver) {
          const message = captcha ? rejected.message : `${rejected.message}. ${CAPTCHA_HINT}`;
          throw new CaptchaRequiredError(message, details(rejected));
        }
        if (rejected.errorCode !== 'INVALID_CAPTCHA' || attempt === CAPTCHA_ATTEMPTS) {
          throw new CaptchaRequiredError(rejected.message, details(rejected));
        }
        solve = true;
      }
    }
  }

  async #passwordToken(
    form: Record<string, string>,
    captcha: string | undefined,
    mfaEnabled: boolean | undefined,
  ): Promise<Tokens> {
    let response: TokenResponse;
    try {
      response = await this.#token(form, captcha === undefined ? {} : { captcha });
    } catch (error) {
      // Without the MFA status an invalid_grant may just mean that a code was expected.
      const unknownMfa = form.code === undefined && mfaEnabled === undefined;
      if (
        unknownMfa &&
        error instanceof AuthenticationError &&
        error.errorCode === 'invalid_grant'
      ) {
        throw new MFARequiredError(`${error.message}. ${MFA_HINT}`, details(error));
      }
      throw error;
    }

    if (response.access_token) return toTokens(response.access_token, response);
    if (response.mfaEnabled) {
      throw form.code === undefined
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

  #solveCaptcha(solver: CaptchaSolver): Promise<string> {
    const { http, authUrl, captchaPath, headers } = this.#config;
    return solveCaptcha(http, authUrl + captchaPath, headers, solver);
  }

  async #mfaEnabled(username: string): Promise<boolean | undefined> {
    const { http, authUrl, mfaStatusPath, headers } = this.#config;
    try {
      const result = await http.send(
        authUrl + mfaStatusPath,
        {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username.toLowerCase() }),
        },
        {},
        readJson,
      );
      const enabled = (result as { mfaEnabled?: unknown } | undefined)?.mfaEnabled;
      return typeof enabled === 'boolean' ? enabled : undefined;
    } catch (error) {
      if (error instanceof APIError) return undefined;
      throw error;
    }
  }

  async #token(
    form: Record<string, string>,
    extraHeaders: Record<string, string> = {},
  ): Promise<TokenResponse> {
    const { http, authUrl, tokenPath, basic, headers } = this.#config;
    const response = await http.send(
      authUrl + tokenPath,
      {
        method: 'POST',
        headers: { ...headers, ...extraHeaders, Authorization: `Basic ${basic}` },
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

/** Copies what the server said, to rethrow it as another error class with a better message. */
function details(error: APIError): APIErrorDetails {
  const { status, code, errorCode, subStatus, fields, body, headers } = error;
  return { status, code, errorCode, subStatus, fields, body, headers };
}

function expiresSoon(tokens: Tokens): boolean {
  return tokens.expiresAt !== undefined && tokens.expiresAt - Date.now() < REFRESH_MARGIN;
}
