/** OAuth tokens of a logged-in session. */
export interface Tokens {
  accessToken: string;
  refreshToken?: string;
  /** Expiry of the access token in epoch milliseconds, when known. */
  expiresAt?: number;
}

/**
 * Keeps the session between runs so the client does not log in on every start. Implement it
 * over a file, a database or a secrets manager; the default keeps tokens in memory.
 *
 * @example
 * ```ts
 * import { readFile, writeFile, rm } from 'node:fs/promises';
 *
 * const fileStore: TokenStore = {
 *   load: () => readFile('session.json', 'utf8').then(JSON.parse, () => undefined),
 *   save: (tokens) => writeFile('session.json', JSON.stringify(tokens), { mode: 0o600 }),
 *   clear: () => rm('session.json', { force: true }),
 * };
 * ```
 */
export interface TokenStore {
  /** Returns the saved tokens, if any. Called once, before the first authenticated request. */
  load(): Tokens | undefined | Promise<Tokens | undefined>;
  /** Saves tokens after every login and refresh. */
  save(tokens: Tokens): unknown;
  /** Forgets the saved tokens. */
  clear(): unknown;
}

/** Keeps tokens in memory for the lifetime of the process. */
export class MemoryTokenStore implements TokenStore {
  #tokens: Tokens | undefined;

  /** @param tokens - Tokens to start with, for example from a previous session. */
  constructor(tokens?: Tokens) {
    this.#tokens = tokens;
  }

  load(): Tokens | undefined {
    return this.#tokens;
  }

  save(tokens: Tokens): void {
    this.#tokens = tokens;
  }

  clear(): void {
    this.#tokens = undefined;
  }
}

/** Reads the `exp` claim of a JWT as epoch milliseconds, without verifying the token. */
export function jwtExpiry(token: string): number | undefined {
  const payload = token.split('.')[1];
  if (!payload) return undefined;
  try {
    const base64 = payload.replace(/-/g, '+').replace(/_/g, '/');
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const { exp } = JSON.parse(new TextDecoder().decode(bytes));
    return typeof exp === 'number' ? exp * 1000 : undefined;
  } catch {
    return undefined;
  }
}
