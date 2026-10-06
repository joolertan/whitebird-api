import { readFile, rm, writeFile } from 'node:fs/promises';
import { type TokenStore, Whitebird } from 'whitebird-api';
import { totp } from 'whitebird-api/otp';

/** Keeps the session in a JSON file, so that only the first run logs in. */
export function fileTokenStore(path: URL): TokenStore {
  return {
    load: () => readFile(path, 'utf8').then(JSON.parse, () => undefined),
    save: (tokens) => writeFile(path, JSON.stringify(tokens), { mode: 0o600 }),
    clear: () => rm(path, { force: true }),
  };
}

/**
 * Client of the account in WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD. Two-factor codes are
 * computed from WHITEBIRD_TOTP_SECRET, the base32 secret behind the authenticator app QR code.
 */
export function createClient(): Whitebird {
  const { WHITEBIRD_USERNAME, WHITEBIRD_PASSWORD, WHITEBIRD_TOTP_SECRET } = process.env;
  return new Whitebird({
    username: WHITEBIRD_USERNAME,
    password: WHITEBIRD_PASSWORD,
    otp: WHITEBIRD_TOTP_SECRET ? totp(WHITEBIRD_TOTP_SECRET) : undefined,
    tokenStore: fileTokenStore(new URL('.session.json', import.meta.url)),
  });
}
