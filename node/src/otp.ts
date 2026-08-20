/**
 * Time-based one-time passwords (RFC 6238) for logging in to accounts with two-factor
 * authentication, computed with WebCrypto.
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
 * ```
 *
 * @packageDocumentation
 */

/** HMAC hash function of a TOTP generator. */
export type TotpAlgorithm = 'SHA-1' | 'SHA-256' | 'SHA-512';

/** Parameters of a TOTP generator; the defaults are those of common authenticator apps. */
export interface TotpOptions {
  /** Number of digits. @defaultValue 6 */
  digits?: number;
  /** Time step in seconds. @defaultValue 30 */
  period?: number;
  /** @defaultValue `'SHA-1'` */
  algorithm?: TotpAlgorithm;
}

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

/**
 * Returns a code provider for the client's `otp` option that computes a fresh code from the
 * authenticator secret for every login.
 *
 * @param secret - Base32 secret shown when two-factor authentication was enabled (the `secret`
 * parameter of the `otpauth://` QR code). Case, spaces and padding are ignored.
 * @throws TypeError if the secret is not valid base32.
 */
export function totp(secret: string, options: TotpOptions = {}): () => Promise<string> {
  const key = decodeBase32(secret);
  return () => generateTotp(key, options);
}

/**
 * Computes the TOTP code for a moment in time.
 *
 * @param secret - Base32 secret, or the raw key bytes.
 * @param options - Generator parameters and `time` in epoch milliseconds (now by default).
 * @returns The code, zero-padded to `digits`.
 */
export async function generateTotp(
  secret: string | Uint8Array,
  options: TotpOptions & { time?: number } = {},
): Promise<string> {
  const { digits = 6, period = 30, algorithm = 'SHA-1', time = Date.now() } = options;
  const key = typeof secret === 'string' ? decodeBase32(secret) : new Uint8Array(secret);
  const counter = Math.floor(time / 1000 / period);
  const message = new DataView(new ArrayBuffer(8));
  message.setUint32(0, Math.floor(counter / 2 ** 32));
  message.setUint32(4, counter >>> 0);

  const hmacKey = await crypto.subtle.importKey(
    'raw',
    key,
    { name: 'HMAC', hash: algorithm },
    false,
    ['sign'],
  );
  const mac = new DataView(await crypto.subtle.sign('HMAC', hmacKey, message));
  // Dynamic truncation, RFC 4226 section 5.3.
  const offset = mac.getUint8(mac.byteLength - 1) & 0x0f;
  const code = (mac.getUint32(offset) & 0x7fffffff) % 10 ** digits;
  return String(code).padStart(digits, '0');
}

function decodeBase32(secret: string): Uint8Array<ArrayBuffer> {
  const text = secret.replace(/[\s=-]/g, '').toUpperCase();
  if (text === '') throw new TypeError('The TOTP secret is empty');
  const bytes = new Uint8Array(Math.floor((text.length * 5) / 8));
  let buffer = 0;
  let bits = 0;
  let index = 0;
  for (const char of text) {
    const value = BASE32.indexOf(char);
    if (value === -1)
      throw new TypeError(`The TOTP secret has an invalid base32 character "${char}"`);
    buffer = (buffer << 5) | value;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes[index++] = buffer >>> bits;
      buffer &= (1 << bits) - 1;
    }
  }
  return bytes;
}
