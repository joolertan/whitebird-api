import { describe, expect, it, vi } from 'vitest';
import {
  AuthenticationError,
  BadRequestError,
  type Captcha,
  CaptchaRequiredError,
  MemoryTokenStore,
  MFARequiredError,
  ServerError,
} from '../src/index.js';
import { bearer, client, form, jwt, MockApi, tokenResponse } from './helpers.js';

const ME = 'GET /api/v1/brokerage/subaccount/me';
const TOKEN = 'POST /oauth/token';
const CAPTCHA = 'GET /api/v1/captcha/generate';
const MFA_TOKEN = 'POST /api/v2/auth/mfa/merchant/client/token';
const WITHDRAWAL = 'POST /api/v2/exchange/client/balance/crypto/withdrawal';
const subaccount = () => Response.json({ uuid: 'sub-1' });
const captchaImages = () =>
  Response.json({
    backgroundImage: btoa('\x01\x02\x03'),
    puzzlePiece: `data:image/png;base64,${btoa('\x04')}`,
    positionY: 42,
  });
/** The answer of the token endpoint when Whitebird wants a captcha with the grant. */
const captchaRequired = () =>
  Response.json({ status: 'REQUIRED_CAPTCHA', message: 'Captcha required' }, { status: 400 });
/** The answer of the token endpoint to a password grant without the code of a 2FA account. */
const askForCode = () => Response.json({ mfaEnabled: true });
const invalidCaptcha = () =>
  Response.json({ status: 'INVALID_CAPTCHA', message: 'Invalid captcha' }, { status: 400 });
const mfaDenied = () =>
  Response.json({ message: 'Operation denied, requires mfa checking' }, { status: 403 });
const withdraw = (whitebird: ReturnType<typeof client>) =>
  whitebird.balance.withdrawals.createCrypto({ accountType: 'WALLET', calculationId: 'calc-1' });

function captchaAnswer(request: Request | undefined): unknown {
  const header = request?.headers.get('captcha');
  return header ? JSON.parse(atob(header)) : undefined;
}

describe('login', () => {
  it('logs in lazily with the password grant and sends the bearer token', async () => {
    const access = jwt(3600);
    const api = new MockApi({
      [TOKEN]: () => Response.json(tokenResponse(access)),
      [ME]: subaccount,
    });

    await expect(client(api).brokerage.subaccounts.me()).resolves.toEqual({ uuid: 'sub-1' });

    const [login] = api.calls(TOKEN);
    expect(login?.url).toBe('https://authorization-server.whitebird.io/oauth/token');
    expect(login?.headers.get('authorization')).toBe('Basic YXV0aENsaWVudElkOlhZN2ttem9OemwxMDA=');
    expect(await form(login as Request)).toEqual({
      grant_type: 'password',
      username: 'trader@example.com',
      password: 'secret',
    });
    expect(bearer(api.calls(ME)[0] as Request)).toBe(access);
  });

  it('sends the code in a second grant when the account asks for it', async () => {
    const otp = vi.fn(() => ' 123456 ');
    const api = new MockApi({ [TOKEN]: [askForCode, () => Response.json(tokenResponse())] });

    await client(api, { otp }).login();

    const [first, second] = await Promise.all(api.calls(TOKEN).map(form));
    expect(first).not.toHaveProperty('code');
    expect(second).toEqual({
      grant_type: 'password',
      username: 'trader@example.com',
      password: 'secret',
      code: '123456',
    });
    expect(otp).toHaveBeenCalledTimes(1);
    expect(api.calls(TOKEN).some((r) => r.headers.has('captcha'))).toBe(false);
  });

  it('does not ask for a code when the first grant returns tokens', async () => {
    const otp = vi.fn(() => '123456');
    const api = new MockApi();

    await client(api, { otp }).login();

    expect(otp).not.toHaveBeenCalled();
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('asks the code provider for a fresh code on every login', async () => {
    const otp = vi.fn().mockResolvedValueOnce('111111').mockResolvedValueOnce('222222');
    const tokens = () => Response.json(tokenResponse());
    const api = new MockApi({ [TOKEN]: [askForCode, tokens, askForCode, tokens] });
    const whitebird = client(api, { otp });

    await whitebird.login();
    await whitebird.login();

    const codes = await Promise.all(api.calls(TOKEN).map(async (r) => (await form(r)).code));
    expect(codes).toEqual([undefined, '111111', undefined, '222222']);
  });

  it('uses the code passed to login()', async () => {
    const otp = vi.fn(() => '111111');
    const api = new MockApi({ [TOKEN]: [askForCode, () => Response.json(tokenResponse())] });

    await client(api, { otp }).login('999999');

    expect((await form(api.calls(TOKEN)[1] as Request)).code).toBe('999999');
    expect(otp).not.toHaveBeenCalled();
  });

  it('raises MFARequiredError when a code is asked for and none is configured', async () => {
    const api = new MockApi({ [TOKEN]: askForCode });

    const error = await client(api)
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(MFARequiredError);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as Error).message).toMatch(/otp/);
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('reports a rejected code as AuthenticationError', async () => {
    const invalidCode = () =>
      Response.json({ status: 'INVALID_CODE', message: 'Invalid code' }, { status: 400 });
    for (const rejection of [invalidCode, askForCode]) {
      const api = new MockApi({ [TOKEN]: [askForCode, rejection] });
      const error = await client(api, { otp: '000000' })
        .login()
        .catch((e: unknown) => e);
      expect(error).toBeInstanceOf(AuthenticationError);
      expect(error).not.toBeInstanceOf(MFARequiredError);
    }
  });

  it('reports wrong credentials as AuthenticationError', async () => {
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [
        captchaRequired,
        () =>
          Response.json(
            { error: 'invalid_grant', error_description: 'Bad credentials' },
            { status: 400 },
          ),
      ],
    });

    const error = await client(api, { captcha: () => 10 })
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(AuthenticationError);
    expect(error).not.toBeInstanceOf(MFARequiredError);
    expect(error).toMatchObject({
      message: 'Bad credentials',
      status: 400,
      errorCode: 'invalid_grant',
    });
  });

  it('keeps the class of blocked and rate-limited logins', async () => {
    const api = new MockApi({
      [TOKEN]: () => Response.json({ status: 'USER_BLOCKED' }, { status: 400 }),
    });
    const error = await client(api)
      .login()
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BadRequestError);
    expect(error).not.toBeInstanceOf(AuthenticationError);
  });

  it('reports an unknown username', async () => {
    const api = new MockApi({
      [TOKEN]: () =>
        Response.json({ status: 'NOT_FOUND', message: 'User not found' }, { status: 400 }),
    });
    const error = await client(api, { otp: '123456', captcha: () => 10 })
      .login()
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect(error).not.toBeInstanceOf(MFARequiredError);
    expect((error as Error).message).toMatch(/no Whitebird account/);
    expect(error).toMatchObject({ status: 400, errorCode: 'NOT_FOUND' });
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('needs credentials or a stored session, before any request', async () => {
    const captcha = vi.fn(() => 10);
    const api = new MockApi({ [ME]: subaccount });
    const anonymous = client(api, { username: undefined, password: undefined, captcha });
    await expect(anonymous.brokerage.subaccounts.me()).rejects.toThrow(/username/);
    expect(api.requests).toHaveLength(0);
    expect(captcha).not.toHaveBeenCalled();
  });

  it('sends Accept-Language with sign-in and API requests', async () => {
    const api = new MockApi({ [ME]: subaccount });
    await client(api, { language: 'en' }).brokerage.subaccounts.me();
    expect(api.requests.map((r) => r.headers.get('accept-language'))).toEqual(['en', 'en']);
  });

  it('stops waiting for a login when the signal aborts, and starts over next time', async () => {
    let answer: (code: string) => void = () => {};
    const otp = vi
      .fn<() => Promise<string>>()
      .mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
      .mockResolvedValueOnce('222222');
    const api = new MockApi({
      [TOKEN]: [askForCode, askForCode, () => Response.json(tokenResponse())],
      [ME]: subaccount,
    });
    const whitebird = client(api, { otp });
    const controller = new AbortController();

    const stuck = whitebird.brokerage.subaccounts.me({ signal: controller.signal });
    await vi.waitFor(() => expect(otp).toHaveBeenCalledTimes(1));
    controller.abort();
    await expect(stuck).rejects.toMatchObject({ name: 'AbortError' });

    await expect(whitebird.brokerage.subaccounts.me()).resolves.toEqual({ uuid: 'sub-1' });
    expect((await form(api.calls(TOKEN)[2] as Request)).code).toBe('222222');
    answer('111111');
  });
});

describe('captcha', () => {
  it('is not solved until the token endpoint asks for one', async () => {
    const solver = vi.fn(() => 10);
    const api = new MockApi({ [CAPTCHA]: captchaImages });

    await client(api, { captcha: solver }).login();

    expect(solver).not.toHaveBeenCalled();
    expect(api.calls(CAPTCHA)).toHaveLength(0);
    expect(api.calls(TOKEN)[0]?.headers.has('captcha')).toBe(false);
  });

  it('is solved when the token endpoint asks for one', async () => {
    const solver = vi.fn((_captcha: Captcha) => 120.4);
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [captchaRequired, () => Response.json(tokenResponse())],
    });

    await client(api, { captcha: solver }).login();

    const [generate] = api.calls(CAPTCHA);
    const requestId = new URL(generate?.url ?? '').searchParams.get('requestId');
    expect(generate?.url).toMatch(/^https:\/\/authorization-server\.whitebird\.io\//);
    expect(requestId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(solver).toHaveBeenCalledWith({
      requestId,
      backgroundImage: new Uint8Array([1, 2, 3]),
      puzzlePiece: new Uint8Array([4]),
      positionY: 42,
    });
    const [first, second] = api.calls(TOKEN);
    expect(first?.headers.has('captcha')).toBe(false);
    // Like the web app, which sends the value of its range input.
    expect(captchaAnswer(second)).toEqual({ sliderPosition: '120', requestId });
    expect(await form(second as Request)).toEqual(await form(first as Request));
  });

  it('is solved again when rejected', async () => {
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [captchaRequired, invalidCaptcha, () => Response.json(tokenResponse())],
    });

    await client(api, { captcha: async () => 10 }).login();

    const ids = api.calls(CAPTCHA).map((r) => new URL(r.url).searchParams.get('requestId'));
    const answers = api.calls(TOKEN).slice(1).map(captchaAnswer);
    expect(answers).toEqual(ids.map((requestId) => ({ sliderPosition: '10', requestId })));
    expect(new Set(ids).size).toBe(2);
  });

  it('gives up after three attempts', async () => {
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [captchaRequired, invalidCaptcha, invalidCaptcha, invalidCaptcha],
    });

    const error = await client(api, { captcha: () => 10 })
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(CaptchaRequiredError);
    expect(error).toMatchObject({ status: 400, errorCode: 'INVALID_CAPTCHA' });
    expect(api.calls(TOKEN)).toHaveLength(4);
    expect(api.calls(CAPTCHA)).toHaveLength(3);
  });

  it('needs a solver when the token endpoint asks for one', async () => {
    const otp = vi.fn(() => '123456');
    const api = new MockApi({ [TOKEN]: captchaRequired });

    const error = await client(api, { otp })
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(CaptchaRequiredError);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as Error).message).toMatch(/captcha solver/);
    expect(error).toMatchObject({ status: 400, errorCode: 'REQUIRED_CAPTCHA' });
    expect(otp).not.toHaveBeenCalled();
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('is solved again for the code step once it was asked for', async () => {
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [captchaRequired, askForCode, () => Response.json(tokenResponse())],
    });

    await client(api, { otp: '123456', captcha: () => 10 }).login();

    const [first, second, third] = api.calls(TOKEN);
    expect(first?.headers.has('captcha')).toBe(false);
    expect(captchaAnswer(second)).toMatchObject({ sliderPosition: '10' });
    expect(await form(second as Request)).not.toHaveProperty('code');
    expect(await form(third as Request)).toMatchObject({ code: '123456' });
    const ids = [second, third].map((r) => (captchaAnswer(r) as { requestId: string }).requestId);
    expect(new Set(ids).size).toBe(2);
    expect(api.calls(CAPTCHA)).toHaveLength(2);
  });

  it('is not solved for the code step when the password went through without one', async () => {
    const solver = vi.fn(() => 10);
    const api = new MockApi({ [TOKEN]: [askForCode, () => Response.json(tokenResponse())] });

    await client(api, { otp: '123456', captcha: solver }).login();

    expect(solver).not.toHaveBeenCalled();
    expect(api.calls(TOKEN).some((r) => r.headers.has('captcha'))).toBe(false);
  });

  it('is asked for anew by every login', async () => {
    const api = new MockApi({
      [CAPTCHA]: captchaImages,
      [TOKEN]: [
        captchaRequired,
        () => Response.json(tokenResponse()),
        () => Response.json(tokenResponse()),
      ],
    });
    const whitebird = client(api, { captcha: () => 10 });

    await whitebird.login();
    await whitebird.login();

    expect(api.calls(TOKEN).map((r) => r.headers.has('captcha'))).toEqual([false, true, false]);
  });
});

describe('refresh', () => {
  it('refreshes proactively when the token expires within a minute', async () => {
    const fresh = jwt(3600);
    const api = new MockApi({
      [TOKEN]: () => Response.json(tokenResponse(fresh, 'refresh-2')),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(50), refreshToken: 'refresh-1' });
    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    expect(await form(api.calls(TOKEN)[0] as Request)).toEqual({
      grant_type: 'refresh_token',
      refresh_token: 'refresh-1',
    });
    expect(bearer(api.calls(ME)[0] as Request)).toBe(fresh);
    expect(store.load()).toMatchObject({ accessToken: fresh, refreshToken: 'refresh-2' });
  });

  it('refreshes once after a 401 and retries the request', async () => {
    const stale = jwt(3600, 'stale');
    const fresh = jwt(3600, 'fresh');
    const api = new MockApi({
      [TOKEN]: () => Response.json(tokenResponse(fresh)),
      [ME]: [() => new Response(null, { status: 401 }), subaccount],
    });
    const store = new MemoryTokenStore({ accessToken: stale, refreshToken: 'refresh-1' });

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    expect(api.calls(ME).map(bearer)).toEqual([stale, fresh]);
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('gives up when the renewed token is rejected too', async () => {
    const api = new MockApi({ [ME]: () => new Response(null, { status: 401 }) });
    const whitebird = client(api);
    await expect(whitebird.brokerage.subaccounts.me()).rejects.toBeInstanceOf(AuthenticationError);
    expect(api.calls(ME)).toHaveLength(2);
  });

  it('shares one login between concurrent requests', async () => {
    const api = new MockApi({ [ME]: subaccount });
    const whitebird = client(api);

    await Promise.all(Array.from({ length: 5 }, () => whitebird.brokerage.subaccounts.me()));

    expect(api.calls(TOKEN)).toHaveLength(1);
    expect(api.calls(ME)).toHaveLength(5);
  });

  it('shares one refresh between concurrent 401s', async () => {
    const stale = jwt(3600, 'stale');
    const api = new MockApi({
      [TOKEN]: () => Response.json(tokenResponse(jwt(3600, 'fresh'))),
      [ME]: (request) =>
        bearer(request) === stale ? new Response(null, { status: 401 }) : subaccount(),
    });
    const store = new MemoryTokenStore({ accessToken: stale, refreshToken: 'refresh-1' });
    const whitebird = client(api, { tokenStore: store });

    await Promise.all(Array.from({ length: 5 }, () => whitebird.brokerage.subaccounts.me()));

    expect(api.calls(TOKEN)).toHaveLength(1);
    expect(api.calls(ME)).toHaveLength(10);
  });

  it('logs in again when the refresh token is rejected', async () => {
    const api = new MockApi({
      [TOKEN]: [
        () => Response.json({ error: 'invalid_grant' }, { status: 400 }),
        () => Response.json(tokenResponse()),
      ],
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: 'revoked' });

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    const grants = await Promise.all(api.calls(TOKEN).map(async (r) => (await form(r)).grant_type));
    expect(grants).toEqual(['refresh_token', 'password']);
  });

  it('fails and forgets the session when the refresh token is rejected and no password is known', async () => {
    const api = new MockApi({
      [TOKEN]: () => Response.json({ error: 'invalid_grant' }, { status: 400 }),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: 'revoked' });
    const whitebird = client(api, { tokenStore: store, password: undefined });

    await expect(whitebird.brokerage.subaccounts.me()).rejects.toBeInstanceOf(AuthenticationError);
    expect(store.load()).toBeUndefined();
  });

  it('does not log in again when the refresh fails for another reason', async () => {
    const api = new MockApi({
      [TOKEN]: () => new Response(null, { status: 503 }),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: 'refresh-1' });

    const error = await client(api, { tokenStore: store, maxRetries: 0 })
      .brokerage.subaccounts.me()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ServerError);
    expect(api.calls(TOKEN)).toHaveLength(1);
    expect(store.load()).toMatchObject({ refreshToken: 'refresh-1' });
  });

  it('keeps the refresh token when the refresh returns none', async () => {
    const fresh = jwt(3600);
    const api = new MockApi({
      [TOKEN]: () => Response.json({ access_token: fresh, expires_in: 3600 }),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: 'refresh-1' });

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    expect(store.load()).toMatchObject({ accessToken: fresh, refreshToken: 'refresh-1' });
  });

  it('skips an expired refresh token and logs in with the password', async () => {
    const api = new MockApi({ [ME]: subaccount });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: jwt(-10) });

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    const grants = await Promise.all(api.calls(TOKEN).map(async (r) => (await form(r)).grant_type));
    expect(grants).toEqual(['password']);
  });

  it('uses a token about to expire when it cannot be renewed', async () => {
    const access = jwt(30);
    const api = new MockApi({ [ME]: subaccount });
    const store = new MemoryTokenStore({ accessToken: access });

    await client(api, { tokenStore: store, password: undefined }).brokerage.subaccounts.me();

    expect(api.calls(TOKEN)).toHaveLength(0);
    expect(bearer(api.calls(ME)[0] as Request)).toBe(access);
  });

  it('adopts a session another process renewed instead of refreshing', async () => {
    const renewed = jwt(3600, 'renewed');
    const api = new MockApi({ [ME]: subaccount });
    const store = new MemoryTokenStore({ accessToken: jwt(10), refreshToken: 'refresh-1' });
    const whitebird = client(api, { tokenStore: store });
    const original = store.load.bind(store);
    let loads = 0;
    store.load = () => (++loads === 1 ? original() : { accessToken: renewed });

    await whitebird.brokerage.subaccounts.me();

    expect(api.calls(TOKEN)).toHaveLength(0);
    expect(bearer(api.calls(ME)[0] as Request)).toBe(renewed);
  });
});

describe('token store', () => {
  it('resumes a saved session without logging in', async () => {
    const access = jwt(3600);
    const api = new MockApi({ [ME]: subaccount });
    const store = new MemoryTokenStore({ accessToken: access, refreshToken: 'refresh-1' });

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    expect(api.calls(TOKEN)).toHaveLength(0);
    expect(bearer(api.calls(ME)[0] as Request)).toBe(access);
  });

  it('saves tokens after login', async () => {
    const access = jwt(3600);
    const api = new MockApi({ [TOKEN]: () => Response.json(tokenResponse(access)) });
    const store = new MemoryTokenStore();

    await client(api, { tokenStore: store }).login();

    expect(store.load()).toEqual({
      accessToken: access,
      refreshToken: 'refresh-1',
      expiresAt: expect.any(Number),
    });
  });

  it('ignores saved data that is not a session', async () => {
    for (const saved of [{ accessToken: 42 }, 'garbage', { refreshToken: 'r' }, null]) {
      const api = new MockApi({ [ME]: subaccount });
      const store = { load: () => saved as never, save: () => {}, clear: () => {} };
      await client(api, { tokenStore: store }).brokerage.subaccounts.me();
      expect(api.calls(TOKEN)).toHaveLength(1);
    }
  });

  it('loads again after a failed load', async () => {
    const access = jwt(3600);
    const load = vi
      .fn()
      .mockRejectedValueOnce(new Error('disk not ready'))
      .mockResolvedValue({ accessToken: access });
    const api = new MockApi({ [ME]: subaccount });
    const whitebird = client(api, { tokenStore: { load, save: () => {}, clear: () => {} } });

    await expect(whitebird.brokerage.subaccounts.me()).rejects.toThrow('disk not ready');
    await expect(whitebird.brokerage.subaccounts.me()).resolves.toEqual({ uuid: 'sub-1' });
    expect(bearer(api.calls(ME)[0] as Request)).toBe(access);
    expect(api.calls(TOKEN)).toHaveLength(0);
  });

  it('does not fail the call when saving fails', async () => {
    const warnings = (globalThis as unknown as { process: { emitWarning(text: string): void } })
      .process;
    const warning = vi.spyOn(warnings, 'emitWarning').mockImplementation(() => {});
    const api = new MockApi({ [ME]: subaccount });
    const store = {
      load: () => undefined,
      save: () => Promise.reject(new Error('read-only')),
      clear: () => {},
    };

    await expect(client(api, { tokenStore: store }).brokerage.subaccounts.me()).resolves.toEqual({
      uuid: 'sub-1',
    });
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('read-only'));
    warning.mockRestore();
  });

  it('accepts asynchronous stores', async () => {
    const saved: unknown[] = [];
    const api = new MockApi({ [ME]: subaccount });
    const store = {
      load: async () => undefined,
      save: async (tokens: unknown) => {
        saved.push(tokens);
      },
      clear: async () => {},
    };

    await client(api, { tokenStore: store }).brokerage.subaccounts.me();

    expect(saved).toHaveLength(1);
  });
});

describe('logout', () => {
  const LOGOUT = 'POST /api/v1/user/logout';

  it('revokes the session on the server and forgets it', async () => {
    const access = jwt(3600);
    const api = new MockApi({ [LOGOUT]: () => new Response(null), [ME]: subaccount });
    const store = new MemoryTokenStore({ accessToken: access, refreshToken: 'refresh-1' });
    const whitebird = client(api, { tokenStore: store });
    whitebird.mfaToken = 'mfa-1';

    await whitebird.logout();

    expect(bearer(api.calls(LOGOUT)[0] as Request)).toBe(access);
    expect(store.load()).toBeUndefined();
    expect(whitebird.mfaToken).toBeUndefined();
    await whitebird.brokerage.subaccounts.me();
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('forgets the session even when the server fails, and does not log in to log out', async () => {
    const failing = new MockApi({ [LOGOUT]: () => new Response(null, { status: 500 }) });
    const store = new MemoryTokenStore({ accessToken: jwt(3600) });
    await client(failing, { tokenStore: store }).logout();
    expect(store.load()).toBeUndefined();

    const anonymous = new MockApi();
    await client(anonymous).logout();
    expect(anonymous.requests).toHaveLength(0);
  });

  it('drops the result of a login in progress', async () => {
    let release: () => void = () => {};
    const api = new MockApi({
      [TOKEN]: () =>
        new Promise<Response>((resolve) => {
          release = () => resolve(Response.json(tokenResponse()));
        }),
    });
    const store = new MemoryTokenStore();
    const whitebird = client(api, { tokenStore: store });

    const login = whitebird.login();
    await vi.waitFor(() => expect(api.calls(TOKEN)).toHaveLength(1));
    await whitebird.logout();
    release();
    await login;

    expect(store.load()).toBeUndefined();
  });

  it('is what user.logout() does too, whatever the answer', async () => {
    const api = new MockApi({ [LOGOUT]: () => new Response(null, { status: 500 }) });
    const store = new MemoryTokenStore({ accessToken: jwt(3600) });
    const whitebird = client(api, { tokenStore: store, maxRetries: 0 });

    await expect(whitebird.user.logout()).rejects.toBeInstanceOf(ServerError);
    expect(store.load()).toBeUndefined();
  });
});

describe('password change', () => {
  const CHANGE = 'POST /api/v1/user/password/change-and-reauthorize';

  it('keeps the new tokens and the new password', async () => {
    const renewed = jwt(3600, 'renewed');
    const api = new MockApi({
      [CHANGE]: () => Response.json({ access_token: renewed, refresh_token: 'refresh-9' }),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(3600), refreshToken: 'refresh-1' });
    const whitebird = client(api, { tokenStore: store });

    await whitebird.user.password.change({ oldPassword: 'secret', newPassword: 'Secret-2' });
    await whitebird.brokerage.subaccounts.me();

    expect(store.load()).toMatchObject({ accessToken: renewed, refreshToken: 'refresh-9' });
    expect(bearer(api.calls(ME)[0] as Request)).toBe(renewed);

    store.clear();
    await whitebird.logout();
    await whitebird.login();
    expect((await form(api.calls(TOKEN)[0] as Request)).password).toBe('Secret-2');
  });

  it('is ignored when the session ended while it was under way', async () => {
    const store = new MemoryTokenStore({ accessToken: jwt(3600), refreshToken: 'refresh-1' });
    let whitebird: ReturnType<typeof client> | undefined;
    const api = new MockApi({
      [CHANGE]: async () => {
        await whitebird?.logout(); // a logout elsewhere meanwhile
        return Response.json({ access_token: jwt(3600, 'renewed') });
      },
      'POST /api/v1/user/logout': () => new Response(null, { status: 200 }),
    });
    whitebird = client(api, { tokenStore: store });

    await whitebird.user.password.change({ oldPassword: 'secret', newPassword: 'Secret-2' });

    expect(store.load()).toBeUndefined();
    await whitebird.login();
    expect((await form(api.calls(TOKEN)[0] as Request)).password).toBe('secret');
  });

  it('keeps the refresh token when the reply has none', async () => {
    const renewed = jwt(3600, 'renewed');
    const api = new MockApi({ [CHANGE]: () => Response.json({ access_token: renewed }) });
    const store = new MemoryTokenStore({ accessToken: jwt(3600), refreshToken: 'refresh-1' });
    const whitebird = client(api, { tokenStore: store });

    await whitebird.user.password.change({ oldPassword: 'secret', newPassword: 'Secret-2' });

    expect(store.load()).toMatchObject({ accessToken: renewed, refreshToken: 'refresh-1' });
  });
});

describe('operation MFA token', () => {
  it('is sent with every request while set', async () => {
    const api = new MockApi({ [ME]: subaccount });
    const whitebird = client(api);

    whitebird.mfaToken = 'mfa-1';
    await whitebird.brokerage.subaccounts.me();
    whitebird.mfaToken = undefined;
    await whitebird.brokerage.subaccounts.me();

    expect(api.calls(ME).map((r) => r.headers.get('x-mfa-token'))).toEqual(['mfa-1', null]);
  });

  it('is required by sensitive operations', async () => {
    const api = new MockApi({ [WITHDRAWAL]: mfaDenied });

    await expect(withdraw(client(api))).rejects.toBeInstanceOf(MFARequiredError);
    expect(api.calls(MFA_TOKEN)).toHaveLength(0);
  });
});

describe('confirmOperation', () => {
  it('exchanges a code for a token and keeps it', async () => {
    const api = new MockApi({ [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }) });
    const whitebird = client(api);

    await expect(whitebird.confirmOperation('WITHDRAWAL', '654321')).resolves.toBe('mfa-1');

    const [request] = api.calls(MFA_TOKEN);
    expect(request?.url).toBe('https://api.whitebird.io/api/v2/auth/mfa/merchant/client/token');
    expect(await request?.json()).toEqual({ operation: 'WITHDRAWAL', mfaCode: '654321' });
    expect(bearer(request as Request)).toBeTruthy();
    expect(whitebird.mfaToken).toBe('mfa-1');
  });

  it.each([
    ['a JSON string', () => Response.json('mfa-2')],
    [
      'a plain string',
      () => new Response('mfa-2\n', { headers: { 'content-type': 'text/plain' } }),
    ],
    ['the header', () => Response.json({}, { headers: { 'x-mfa-token': 'mfa-2' } })],
    [
      'the header of an empty response',
      () => new Response(null, { headers: { 'x-mfa-token': 'mfa-2' } }),
    ],
  ])('reads the token from %s', async (_, response) => {
    const api = new MockApi({ [MFA_TOKEN]: response });
    await expect(client(api).confirmOperation('WITHDRAWAL', '1')).resolves.toBe('mfa-2');
  });

  it('fails when the response has no token', async () => {
    const api = new MockApi({ [MFA_TOKEN]: () => Response.json({ ok: true }) });
    const whitebird = client(api);

    await expect(whitebird.confirmOperation('WITHDRAWAL', '1')).rejects.toThrow(
      /no operation MFA token/,
    );
    expect(whitebird.mfaToken).toBeUndefined();
  });

  it('takes the code from otp', async () => {
    const api = new MockApi({ [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }) });

    await client(api, { otp: () => '777777' }).confirmOperation('WITHDRAWAL');

    expect(await api.calls(MFA_TOKEN)[0]?.json()).toMatchObject({ mfaCode: '777777' });
  });

  it('needs a code or otp', async () => {
    const api = new MockApi();

    await expect(client(api).confirmOperation('WITHDRAWAL')).rejects.toBeInstanceOf(
      MFARequiredError,
    );
    expect(api.requests).toHaveLength(0);
  });

  it('is obtained with otp when an operation is refused for want of one', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: ' mfa-1 ' }),
      [WITHDRAWAL]: [mfaDenied, () => Response.json({ id: 'w-1' })],
    });

    await expect(withdraw(client(api, { otp: '123456' }))).resolves.toEqual({ id: 'w-1' });

    expect(await api.calls(MFA_TOKEN)[0]?.json()).toEqual({
      operation: 'WITHDRAWAL',
      mfaCode: '123456',
    });
    const sent = api.calls(WITHDRAWAL).map((r) => r.headers.get('x-mfa-token'));
    expect(sent).toEqual([null, 'mfa-1']);
  });

  it('is not requested before the API asks for it', async () => {
    const api = new MockApi({ [WITHDRAWAL]: () => Response.json({ id: 'w-1' }) });
    await withdraw(client(api, { otp: '123456' }));
    expect(api.calls(MFA_TOKEN)).toHaveLength(0);
  });

  it('recognizes the refusal in the status field', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }),
      [WITHDRAWAL]: [
        () =>
          Response.json(
            { status: 'Operation denied, requires mfa checking', message: 'Forbidden' },
            { status: 403 },
          ),
        () => Response.json({ id: 'w-1' }),
      ],
    });
    await expect(withdraw(client(api, { otp: '123456' }))).resolves.toEqual({ id: 'w-1' });
  });

  it('is obtained once for operations refused at the same time', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }),
      [WITHDRAWAL]: (request) =>
        request.headers.get('x-mfa-token') === 'mfa-1' ? Response.json({ id: 'w-1' }) : mfaDenied(),
    });
    const whitebird = client(api, { otp: '123456' });

    await Promise.all([withdraw(whitebird), withdraw(whitebird), withdraw(whitebird)]);

    expect(api.calls(MFA_TOKEN)).toHaveLength(1);
    expect(api.calls(WITHDRAWAL)).toHaveLength(6);
  });

  it('runs again once when the token is refused', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-new' }),
      [WITHDRAWAL]: [mfaDenied, () => Response.json({ id: 'w-1' })],
    });
    const whitebird = client(api, { otp: '123456' });
    whitebird.mfaToken = 'mfa-old';

    await expect(withdraw(whitebird)).resolves.toEqual({ id: 'w-1' });

    const sent = api.calls(WITHDRAWAL).map((r) => r.headers.get('x-mfa-token'));
    expect(sent).toEqual(['mfa-old', 'mfa-new']);
  });

  it('gives up when the new token is refused too', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }),
      [WITHDRAWAL]: mfaDenied,
    });

    await expect(withdraw(client(api, { otp: '123456' }))).rejects.toBeInstanceOf(MFARequiredError);
    expect(api.calls(MFA_TOKEN)).toHaveLength(1);
    expect(api.calls(WITHDRAWAL)).toHaveLength(2);
  });
});
