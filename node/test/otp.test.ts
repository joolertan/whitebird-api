import { describe, expect, it } from 'vitest';
import { generateTotp, type TotpAlgorithm, totp } from '../src/otp.js';
import { client, form, MockApi } from './helpers.js';

const encoder = new TextEncoder();
const SEEDS: Record<TotpAlgorithm, Uint8Array> = {
  'SHA-1': encoder.encode('12345678901234567890'),
  'SHA-256': encoder.encode('12345678901234567890123456789012'),
  'SHA-512': encoder.encode('1234567890123456789012345678901234567890123456789012345678901234'),
};

// RFC 6238, appendix B.
const VECTORS: [number, string, TotpAlgorithm][] = [
  [59, '94287082', 'SHA-1'],
  [59, '46119246', 'SHA-256'],
  [59, '90693936', 'SHA-512'],
  [1111111109, '07081804', 'SHA-1'],
  [1111111109, '68084774', 'SHA-256'],
  [1111111109, '25091201', 'SHA-512'],
  [1111111111, '14050471', 'SHA-1'],
  [1111111111, '67062674', 'SHA-256'],
  [1111111111, '99943326', 'SHA-512'],
  [1234567890, '89005924', 'SHA-1'],
  [1234567890, '91819424', 'SHA-256'],
  [1234567890, '93441116', 'SHA-512'],
  [2000000000, '69279037', 'SHA-1'],
  [2000000000, '90698825', 'SHA-256'],
  [2000000000, '38618901', 'SHA-512'],
  [20000000000, '65353130', 'SHA-1'],
  [20000000000, '77737706', 'SHA-256'],
  [20000000000, '47863826', 'SHA-512'],
];

describe('TOTP', () => {
  it.each(VECTORS)('at %i s is %s with %s', async (seconds, code, algorithm) => {
    const options = { time: seconds * 1000, digits: 8, algorithm };
    await expect(generateTotp(SEEDS[algorithm], options)).resolves.toBe(code);
  });

  it('decodes base32 secrets regardless of case, spaces and padding', async () => {
    const time = 59_000;
    const expected = await generateTotp(SEEDS['SHA-1'], { time });
    for (const secret of [
      'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
      'gezd gnbv gy3t qojq gezd gnbv gy3t qojq',
      'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ====',
    ]) {
      await expect(generateTotp(secret, { time })).resolves.toBe(expected);
    }
    expect(expected).toBe('287082');
  });

  it('rejects invalid secrets', () => {
    expect(() => totp('not base32!')).toThrow(TypeError);
    expect(() => totp('   ')).toThrow(TypeError);
  });

  it('provides fresh codes for login', async () => {
    const api = new MockApi();
    const provider = totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ');

    await client(api, { otp: provider }).login();

    const { code } = await form(api.calls('POST /oauth/token')[0] as Request);
    expect(code).toMatch(/^\d{6}$/);
  });
});
