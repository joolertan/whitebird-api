import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  APIError,
  AuthenticationError,
  BadRequestError,
  ConflictError,
  MFARequiredError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  ServerError,
  TransportError,
  VerificationRequiredError,
  WhitebirdError,
} from '../src/index.js';
import { client, MockApi } from './helpers.js';

const PUBLIC = 'GET /public';
const call = (api: MockApi, options = {}) =>
  client(api, { maxRetries: 0, baseUrls: { brokerage: 'https://api.test' }, ...options }).request(
    'brokerage',
    'GET',
    '/public',
    { auth: false },
  );

afterEach(() => {
  vi.useRealTimers();
});

describe('error responses', () => {
  const cases: [number, object, new (...args: never[]) => APIError, string][] = [
    [
      400,
      { code: 1, status: 'VALIDATION', message: 'Bad amount' },
      BadRequestError,
      'BadRequestError',
    ],
    [
      400,
      { status: 'SMS_VERIFICATION_REQUIRED' },
      VerificationRequiredError,
      'VerificationRequiredError',
    ],
    [401, {}, AuthenticationError, 'AuthenticationError'],
    [
      403,
      { status: 'Operation denied, requires mfa checking', message: 'Forbidden' },
      MFARequiredError,
      'MFARequiredError',
    ],
    [
      403,
      { message: 'Operation denied, requires mfa checking' },
      MFARequiredError,
      'MFARequiredError',
    ],
    [403, { message: 'Forbidden' }, PermissionDeniedError, 'PermissionDeniedError'],
    [404, { message: 'Not found' }, NotFoundError, 'NotFoundError'],
    [409, { message: 'Already exists' }, ConflictError, 'ConflictError'],
    [429, {}, RateLimitError, 'RateLimitError'],
    [500, {}, ServerError, 'ServerError'],
    [418, {}, APIError, 'APIError'],
  ];

  it.each(cases)('maps %i %j to %o', async (status, body, ErrorClass, name) => {
    const api = new MockApi({ [PUBLIC]: () => Response.json(body, { status }) });

    const error = await call(api).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ErrorClass);
    expect(error).toBeInstanceOf(APIError);
    expect(error).toBeInstanceOf(WhitebirdError);
    expect(error).toMatchObject({ name, status });
  });

  it('keeps the details of service errors', async () => {
    const fields = [{ field: 'amount', message: 'must be positive' }];
    const body = {
      code: 4001,
      status: 'INVALID_AMOUNT',
      subStatus: 'BELOW_MINIMUM',
      message: 'Invalid amount',
      fields,
    };
    const api = new MockApi({ [PUBLIC]: () => Response.json(body, { status: 400 }) });

    const error = await call(api).catch((e: unknown) => e);

    expect(error).toMatchObject({
      message: 'Invalid amount',
      status: 400,
      code: 4001,
      errorCode: 'INVALID_AMOUNT',
      subStatus: 'BELOW_MINIMUM',
      fields,
      body,
    });
  });

  it('tells SMS and liveness verifications apart', async () => {
    for (const [status, kind] of [
      ['SMS_VERIFICATION_REQUIRED', 'sms'],
      ['LIVENESS_VERIFICATION_REQUIRED', 'liveness'],
    ]) {
      const api = new MockApi({ [PUBLIC]: () => Response.json({ status }, { status: 400 }) });
      await expect(call(api)).rejects.toMatchObject({ kind });
    }
  });

  it('reads retCode and retMsg of error envelopes', async () => {
    const api = new MockApi({
      [PUBLIC]: () => Response.json({ retCode: 10001, retMsg: 'Invalid symbol' }, { status: 400 }),
    });
    await expect(call(api)).rejects.toMatchObject({ code: 10001, message: 'Invalid symbol' });
  });

  it('keeps the start of a text body as the message', async () => {
    const page = `<html>\n  <body>Bad gateway: upstream ${'x'.repeat(300)}</body></html>`;
    const api = new MockApi({ [PUBLIC]: () => new Response(page, { status: 502 }) });

    const error = (await call(api).catch((e: unknown) => e)) as Error;

    expect(error.message).toMatch(/^<html> <body>Bad gateway: upstream x+…$/);
    expect(error.message.length).toBeLessThanOrEqual(201);
  });

  it('reads Retry-After of rate limit errors', async () => {
    const api = new MockApi({
      [PUBLIC]: () => new Response('slow down', { status: 429, headers: { 'Retry-After': '7' } }),
    });
    await expect(call(api)).rejects.toMatchObject({ retryAfter: 7, body: 'slow down' });
  });

  it('wraps network failures in TransportError', async () => {
    const api = new MockApi({
      [PUBLIC]: () => {
        throw new TypeError('fetch failed', { cause: new Error('connect ECONNREFUSED') });
      },
    });

    const error = await call(api).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(TransportError);
    expect((error as Error).message).toContain('ECONNREFUSED');
  });
});

describe('retries', () => {
  const unavailable = () => new Response(null, { status: 503, headers: { 'Retry-After': '0' } });
  const ok = () => Response.json({ ok: true });

  it('retries idempotent requests on retryable statuses', async () => {
    const api = new MockApi({ [PUBLIC]: [unavailable, unavailable, ok] });
    await expect(call(api, { maxRetries: 2 })).resolves.toEqual({ ok: true });
    expect(api.calls(PUBLIC)).toHaveLength(3);
  });

  it('stops after maxRetries', async () => {
    const api = new MockApi({ [PUBLIC]: [unavailable, unavailable, ok] });
    await expect(call(api, { maxRetries: 1 })).rejects.toBeInstanceOf(ServerError);
    expect(api.calls(PUBLIC)).toHaveLength(2);
  });

  it('does not retry other methods on server errors unless marked idempotent', async () => {
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const api = new MockApi({ [`${method} /public`]: [unavailable, ok, unavailable, ok] });
      const whitebird = client(api, { baseUrls: { brokerage: 'https://api.test' } });
      const send = (idempotent?: boolean) =>
        whitebird.request('brokerage', method, '/public', { auth: false, idempotent });

      await expect(send()).rejects.toBeInstanceOf(ServerError);
      await expect(send(true)).resolves.toEqual({ ok: true });
    }
  });

  it('retries other methods on 429 and on connections that failed before sending', async () => {
    const limited = () => new Response(null, { status: 429, headers: { 'Retry-After': '0' } });
    const refused = () => {
      throw new TypeError('fetch failed', {
        cause: Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' }),
      });
    };
    const reset = () => {
      throw new TypeError('fetch failed', {
        cause: Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' }),
      });
    };
    const api = new MockApi({ 'POST /public': [limited, ok, refused, ok, reset, ok] });
    const whitebird = client(api, { baseUrls: { brokerage: 'https://api.test' } });
    const post = () => whitebird.request('brokerage', 'POST', '/public', { auth: false });
    vi.useFakeTimers();
    const settle = async (result: Promise<unknown>) => {
      const outcome = result.then(
        (value) => ({ value }),
        (error: unknown) => ({ error }),
      );
      await vi.advanceTimersByTimeAsync(1000);
      return outcome;
    };

    expect(await settle(post())).toEqual({ value: { ok: true } });
    expect(await settle(post())).toEqual({ value: { ok: true } });
    const unsure = await settle(post());
    expect((unsure as { error: unknown }).error).toBeInstanceOf(TransportError);
    expect(api.calls('POST /public')).toHaveLength(5);
  });

  it('does not retry client errors', async () => {
    const api = new MockApi({ [PUBLIC]: [() => new Response(null, { status: 400 }), ok] });
    await expect(call(api, { maxRetries: 2 })).rejects.toBeInstanceOf(BadRequestError);
  });

  it('retries transport errors', async () => {
    const api = new MockApi({
      [PUBLIC]: [
        () => {
          throw new TypeError('fetch failed');
        },
        ok,
      ],
    });
    vi.useFakeTimers();
    const result = call(api, { maxRetries: 1 });
    await vi.advanceTimersByTimeAsync(500);
    await expect(result).resolves.toEqual({ ok: true });
  });

  it('waits as long as Retry-After asks', async () => {
    const limited = () => new Response(null, { status: 429, headers: { 'Retry-After': '2' } });
    const api = new MockApi({ [PUBLIC]: [limited, ok] });
    vi.useFakeTimers();

    const result = call(api, { maxRetries: 1 });
    await vi.advanceTimersByTimeAsync(1900);
    expect(api.calls(PUBLIC)).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(100);
    await expect(result).resolves.toEqual({ ok: true });
  });

  it('does not wait for a Retry-After longer than a minute', async () => {
    const limited = () => new Response(null, { status: 429, headers: { 'Retry-After': '120' } });
    const api = new MockApi({ [PUBLIC]: [limited, ok] });
    await expect(call(api, { maxRetries: 2 })).rejects.toMatchObject({ retryAfter: 120 });
  });
});

describe('timeouts and cancellation', () => {
  const hang = (request: Request) =>
    new Promise<Response>((_, reject) => {
      request.signal.addEventListener('abort', () => reject(request.signal.reason));
    });

  it('times out with TransportError', async () => {
    const api = new MockApi({ [PUBLIC]: hang });
    const error = await call(api, { timeout: 20 }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(TransportError);
    expect((error as Error).message).toMatch(/timed out after 20 ms/);
  });

  it('rejects with the abort reason when the caller aborts', async () => {
    const api = new MockApi({ [PUBLIC]: hang });
    const whitebird = client(api, { baseUrls: { brokerage: 'https://api.test' } });
    const controller = new AbortController();

    const result = whitebird.request('brokerage', 'GET', '/public', {
      auth: false,
      signal: controller.signal,
    });
    controller.abort();

    await expect(result).rejects.toMatchObject({ name: 'AbortError' });
    expect(api.calls(PUBLIC)).toHaveLength(1);
  });
});
