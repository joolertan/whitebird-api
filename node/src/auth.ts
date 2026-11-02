import { type CaptchaSolver, solveCaptcha } from './captcha.js';
import {
  APIError,
  type APIErrorDetails,
  AuthenticationError,
  BadRequestError,
  CaptchaRequiredError,
  MFARequiredError,
  VerificationRequiredError,
} from './errors.js';
import { type HttpClient, readJson } from './http.js';
import { jwtExpiry, parseTokens, type TokenStore, type Tokens } from './tokens.js';

/**
 * Two-factor code: a code, or a function returning a fresh one whenever a code is needed, such
 * as `totp(secret)` from `whitebird-api/otp` or a prompt that asks a person.
 */
export type CodeProvider = string | (() => string | Promise<string>);

/** Returns the current code of a provider, trimmed; `undefined` when there is none. */
export async function resolveCode(otp: CodeProvider | undefined): Promise<string | undefined> {
  const code = typeof otp === 'function' ? await otp() : otp;
  const trimmed = typeof code === 'string' ? code.trim() : undefined;
  return trimmed || undefined;
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
  /** Called whenever the session is forgotten, to drop what belongs to it. */
  onEnd?: (() => void) | undefined;
}

/** OAuth token response; accounts with 2FA get `{mfaEnabled: true}` until the code is sent. */
export interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  mfaEnabled?: boolean;
}

/** A login or refresh in progress, shared by the calls that wait for it. */
interface Attempt {
  promise: Promise<Tokens>;
  /** Order in which attempts started; a result never replaces that of a later attempt. */
  sequence: number;
  waiters: number;
}

// The web application renews tokens that expire within a minute (app-pages, `PN = 60000`).
const REFRESH_MARGIN = 60_000;
const CAPTCHA_ATTEMPTS = 3;
// Codes of rejected logins that say more than "wrong credentials".
const LOGIN_ERRORS = new Set(['USER_BLOCKED', 'MANY_REQUESTS']);
const NOT_LOGGED_IN =
  'Not logged in: pass `username` and `password`, or a `tokenStore` holding a session';
const MFA_HINT =
  'Two-factor authentication is enabled for this account: pass `otp` (a code, a function ' +
  "returning one, or totp(secret) from 'whitebird-api/otp'), or call login(code)";
const NO_MFA_CAPTCHA =
  'The account has no two-factor authentication, so Whitebird asks for a captcha at sign-in: ' +
  'configure a captcha solver (the captcha option) or enable two-factor authentication';
const EXPIRED = 'The session expired and cannot be renewed without `username` and `password`';

/**
 * Owns the OAuth tokens: logs in lazily like the web application, refreshes shortly before expiry
 * or after a 401, and makes concurrent callers share a single login or refresh.
 */
export class Session {
  readonly #config: SessionConfig;
  #password: string | undefined;
  #tokens: Tokens | undefined;
  #restored = false;
  #restoring: Promise<void> | undefined;
  #attempt: Attempt | undefined;
  #attempts = 0;
  /** Sequence of the attempt whose tokens are held. */
  #adopted = 0;
  /** Bumped when the session ends or is replaced, so that late results are dropped. */
  #generation = 0;

  constructor(config: SessionConfig) {
    this.#config = config;
    this.#password = config.password;
  }

  /** Changes whenever the session ends or is replaced. */
  get generation(): number {
    return this.#generation;
  }

  /** Returns the access token held now, without logging in or refreshing. */
  async currentToken(): Promise<string | undefined> {
    await this.#restore();
    return this.#tokens?.accessToken;
  }

  /**
   * Returns an access token valid for at least another minute, logging in or refreshing when
   * needed. A token about to expire is still returned when there is no way to get a new one.
   */
  async accessToken(signal?: AbortSignal): Promise<string> {
    await this.#restore();
    const tokens = this.#tokens;
    if (tokens && this.#usable(tokens)) return tokens.accessToken;
    return (await this.#renew(() => this.#refreshOrLogin(tokens?.accessToken), signal)).accessToken;
  }

  /** Returns a replacement for an access token the server rejected. */
  async renew(rejected: string, signal?: AbortSignal): Promise<string> {
    const tokens = this.#tokens;
    // Another request may have replaced the token already.
    if (!this.#attempt && tokens && tokens.accessToken !== rejected) return tokens.accessToken;
    return (await this.#renew(() => this.#refreshOrLogin(rejected), signal)).accessToken;
  }

  /** Starts a new session with the password, using `code` if the account asks for one. */
  async login(code?: string, signal?: AbortSignal): Promise<void> {
    const otp = code === undefined ? this.#config.otp : code;
    await this.#renew(() => this.#passwordLogin(otp), signal, true);
  }

  /** Forgets the session, also in the token store; logins in progress no longer count. */
  async end(): Promise<void> {
    this.#generation += 1;
    this.#attempt = undefined;
    this.#tokens = undefined;
    this.#restored = true;
    this.#config.onEnd?.();
    await this.#config.store.clear();
  }

  /** Replaces the session with tokens the API issued, such as after a password change. */
  /**
   * Stores the tokens a call such as a password change answered with, unless the session ended
   * or was replaced since `generation`. A reply without refresh token keeps the current one.
   */
  async replace(response: TokenResponse, generation: number, password?: string): Promise<void> {
    if (generation !== this.#generation) return;
    const tokens = toTokens(response, this.#tokens?.refreshToken);
    this.#generation += 1;
    this.#attempt = undefined;
    this.#adopted = this.#attempts;
    this.#tokens = tokens;
    this.#restored = true;
    if (password) this.#password = password;
    await this.#save(tokens);
  }

  async #restore(): Promise<void> {
    if (this.#restored) return;
    const generation = this.#generation;
    this.#restoring ??= this.#load()
      .then((saved) => {
        this.#restored = true;
        // A login that finished while the store was loading wins over the saved session.
        if (saved && !this.#tokens && this.#generation === generation) this.#tokens = saved;
      })
      .finally(() => {
        // A failed load is tried again by the next call.
        this.#restoring = undefined;
      });
    await this.#restoring;
  }

  async #load(): Promise<Tokens | undefined> {
    return parseTokens(await this.#config.store.load());
  }

  async #save(tokens: Tokens): Promise<void> {
    try {
      await this.#config.store.save(tokens);
    } catch (error) {
      warn('The token store failed to save the session', error);
    }
  }

  /** Clears the tokens of a session that cannot be renewed, without ending a login in progress. */
  async #forget(): Promise<void> {
    this.#tokens = undefined;
    this.#config.onEnd?.();
    try {
      await this.#config.store.clear();
    } catch (error) {
      warn('The token store failed to clear the session', error);
    }
  }

  #usable(tokens: Tokens): boolean {
    if (!expiresWithin(tokens, REFRESH_MARGIN)) return true;
    return !expiresWithin(tokens, 0) && !this.#canRenew(tokens);
  }

  #canRenew(tokens: Tokens | undefined): boolean {
    return liveRefreshToken(tokens) !== undefined || this.#hasCredentials();
  }

  #hasCredentials(): boolean {
    return Boolean(this.#config.username && this.#password);
  }

  /**
   * Shares one login or refresh between callers. A caller whose signal aborts stops waiting; when
   * nobody waits any more, the attempt is abandoned so that a stuck one, such as a prompt nobody
   * answers, does not hold up later calls.
   */
  async #renew(
    obtain: () => Promise<Tokens>,
    signal: AbortSignal | undefined,
    fresh = false,
  ): Promise<Tokens> {
    signal?.throwIfAborted();
    let attempt = this.#attempt;
    if (!attempt || fresh) {
      attempt = this.#start(obtain);
      this.#attempt = attempt;
    }
    attempt.waiters += 1;
    try {
      return await abortable(attempt.promise, signal);
    } finally {
      attempt.waiters -= 1;
      if (attempt.waiters === 0 && signal?.aborted && this.#attempt === attempt) {
        this.#attempt = undefined;
      }
    }
  }

  #start(obtain: () => Promise<Tokens>): Attempt {
    const generation = this.#generation;
    const sequence = ++this.#attempts;
    const promise = obtain()
      .then(async (tokens) => {
        // An abandoned attempt that ends late still counts, unless a newer one ended first.
        if (this.#generation === generation && sequence > this.#adopted) {
          this.#adopted = sequence;
          this.#tokens = tokens;
          this.#restored = true;
          await this.#save(tokens);
        }
        return tokens;
      })
      .finally(() => {
        if (this.#attempt?.sequence === sequence) this.#attempt = undefined;
      });
    // An abandoned attempt may still fail later, when nobody listens.
    promise.catch(() => {});
    return { promise, sequence, waiters: 0 };
  }

  async #refreshOrLogin(seen: string | undefined): Promise<Tokens> {
    // Another process sharing the token store may have renewed the session already.
    const saved = await this.#load().catch(() => undefined);
    if (saved && saved.accessToken !== seen && !expiresWithin(saved, REFRESH_MARGIN)) return saved;

    const tokens = this.#tokens ?? saved;
    const refreshToken = liveRefreshToken(tokens);
    if (refreshToken) {
      try {
        return await this.#refresh(refreshToken);
      } catch (error) {
        // Only a rejected refresh token ends the session; outages and rate limits do not.
        if (!(error instanceof APIError) || (error.status !== 400 && error.status !== 401)) {
          throw error;
        }
        await this.#forget();
        if (!this.#hasCredentials()) throw new AuthenticationError(EXPIRED, details(error));
      }
    } else if (tokens && !this.#hasCredentials()) {
      await this.#forget();
      throw new AuthenticationError(EXPIRED);
    }
    return this.#passwordLogin(this.#config.otp);
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
    return toTokens(response, refreshToken);
  }

  /**
   * Logs in like the web application: asks whether the account has 2FA, sends the password
   * (with a solved captcha when it has not), and sends it again with a code if asked for one.
   */
  async #passwordLogin(otp: CodeProvider | undefined): Promise<Tokens> {
    const { username } = this.#config;
    const password = this.#password;
    if (!username || !password) throw new AuthenticationError(NOT_LOGGED_IN);

    const twoFactor = await this.#twoFactorEnabled(username);
    const form = { grant_type: 'password', username: username.toLowerCase(), password };
    const response = twoFactor ? await this.#grant(form) : await this.#grantWithCaptcha(form);
    if (response.access_token) return toTokens(response);
    if (!response.mfaEnabled) {
      throw new AuthenticationError('The login returned no access token', {
        status: 200,
        body: response,
      });
    }

    const code = await resolveCode(otp);
    if (code === undefined) throw new MFARequiredError(MFA_HINT, { status: 200, body: response });
    const confirmed = await this.#grant({ ...form, code });
    if (confirmed.access_token) return toTokens(confirmed);
    throw new AuthenticationError('The two-factor code was rejected', {
      status: 200,
      body: confirmed,
    });
  }

  /** Sends the password with a solved captcha, solving a new one when it is rejected. */
  async #grantWithCaptcha(form: Record<string, string>): Promise<TokenResponse> {
    const solver = this.#config.captcha;
    if (!solver) throw new CaptchaRequiredError(NO_MFA_CAPTCHA);
    for (let attempt = 1; ; attempt++) {
      const captcha = await this.#solveCaptcha(solver);
      try {
        return await this.#grant(form, { captcha });
      } catch (error) {
        if (!(error instanceof APIError) || !isCaptchaError(error)) throw error;
        if (error.errorCode !== 'INVALID_CAPTCHA' || attempt === CAPTCHA_ATTEMPTS) {
          throw new CaptchaRequiredError(error.message, details(error));
        }
      }
    }
  }

  /** Sends a password grant; a rejected one is an {@link AuthenticationError}. */
  async #grant(
    form: Record<string, string>,
    headers: Record<string, string> = {},
  ): Promise<TokenResponse> {
    try {
      return await this.#token(form, headers);
    } catch (error) {
      const rejected =
        error instanceof BadRequestError &&
        !(error instanceof VerificationRequiredError) &&
        !isCaptchaError(error) &&
        !LOGIN_ERRORS.has(error.errorCode ?? '');
      throw rejected ? new AuthenticationError(error.message, details(error)) : error;
    }
  }

  #solveCaptcha(solver: CaptchaSolver): Promise<string> {
    const { http, authUrl, captchaPath, headers } = this.#config;
    return solveCaptcha(http, authUrl + captchaPath, headers, solver);
  }

  /** Asks whether the account has 2FA; the password is never sent when this fails. */
  async #twoFactorEnabled(username: string): Promise<boolean> {
    const { http, authUrl, mfaStatusPath, headers } = this.#config;
    let result: unknown;
    try {
      result = await http.send(
        authUrl + mfaStatusPath,
        {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          // As typed, like the web application; only the token request lowercases it.
          body: JSON.stringify({ username }),
        },
        {},
        readJson,
      );
    } catch (error) {
      if (error instanceof APIError && (error.status === 404 || error.errorCode === 'NOT_FOUND')) {
        throw new AuthenticationError('There is no Whitebird account with this username', {
          ...details(error),
        });
      }
      throw error;
    }
    const body = (typeof result === 'object' && result !== null ? result : {}) as {
      mfaEnabled?: unknown;
      status?: unknown;
    };
    if (body.status === 'NOT_FOUND') {
      throw new AuthenticationError('There is no Whitebird account with this username', {
        status: 200,
        errorCode: 'NOT_FOUND',
        body: result,
      });
    }
    return Boolean(body.mfaEnabled);
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
    return (typeof response === 'object' && response !== null ? response : {}) as TokenResponse;
  }
}

/** Builds the tokens of a token response; a missing refresh token keeps the previous one. */
export function toTokens(response: TokenResponse, refreshToken?: string): Tokens {
  const accessToken = response.access_token ?? '';
  const lifetime = response.expires_in;
  return {
    accessToken,
    refreshToken: response.refresh_token || refreshToken,
    expiresAt:
      jwtExpiry(accessToken) ??
      (typeof lifetime === 'number' ? Date.now() + lifetime * 1000 : undefined),
  };
}

/** Emits a warning about a failure that does not fail the call. */
export function warn(message: string, error: unknown): void {
  const text = `${message}: ${error instanceof Error ? error.message : String(error)}`;
  const process = (globalThis as { process?: { emitWarning?: (warning: string) => void } }).process;
  if (process?.emitWarning) process.emitWarning(text);
  else console.warn(text);
}

/** Waits for a promise, or rejects with the signal's reason when it aborts first. */
function abortable<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (!signal) return promise;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    if (signal.aborted) return onAbort();
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort));
  });
}

function isCaptchaError(error: APIError): boolean {
  return error.errorCode?.toUpperCase().includes('CAPTCHA') ?? false;
}

/** Copies what the server said, to rethrow it as another error class with a better message. */
function details(error: APIError): APIErrorDetails {
  const { status, code, errorCode, subStatus, fields, body, headers } = error;
  return { status, code, errorCode, subStatus, fields, body, headers };
}

function expiresWithin(tokens: Tokens, margin: number): boolean {
  return tokens.expiresAt !== undefined && tokens.expiresAt - Date.now() < margin;
}

/** The refresh token, unless it is a JWT that has expired. */
function liveRefreshToken(tokens: Tokens | undefined): string | undefined {
  const token = tokens?.refreshToken;
  if (!token) return undefined;
  const expiry = jwtExpiry(token);
  return expiry === undefined || expiry > Date.now() ? token : undefined;
}
