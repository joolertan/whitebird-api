import { describe, expect, it, vi } from 'vitest';
import {
  AuthenticationError,
  type Captcha,
  CaptchaRequiredError,
  MemoryTokenStore,
  MFARequiredError,
} from '../src/index.js';
import { bearer, client, form, jwt, MockApi, tokenResponse } from './helpers.js';

const ME = 'GET /api/v1/brokerage/subaccount/me';
const TOKEN = 'POST /oauth/token';
const MFA_CHECK = 'POST /api/v1/client/mfa';
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
const withMfa = () => Response.json({ mfaEnabled: true });
const withoutMfa = () => Response.json({ mfaEnabled: false });
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
    expect(await api.calls(MFA_CHECK)[0]?.json()).toEqual({ username: 'trader@example.com' });
    expect(bearer(api.calls(ME)[0] as Request)).toBe(access);
  });

  it('sends the code to accounts with 2FA', async () => {
    const api = new MockApi({ [MFA_CHECK]: withMfa, [ME]: subaccount });
    await client(api, { otp: '123456' }).brokerage.subaccounts.me();

    expect(api.calls(MFA_CHECK)).toHaveLength(1);
    expect(await form(api.calls(TOKEN)[0] as Request)).toMatchObject({ code: '123456' });
    expect(api.calls(TOKEN)[0]?.headers.has('captcha')).toBe(false);
  });

  it('still sends the code when the 2FA status check fails', async () => {
    const api = new MockApi({ [MFA_CHECK]: () => new Response(null, { status: 500 }) });

    await client(api, { otp: '123456', maxRetries: 0 }).login();

    expect(api.calls(MFA_CHECK)).toHaveLength(1);
    expect(await form(api.calls(TOKEN)[0] as Request)).toMatchObject({ code: '123456' });
  });

  it('asks the code provider for a fresh code on every login', async () => {
    const otp = vi.fn().mockResolvedValueOnce('111111').mockResolvedValueOnce('222222');
    const api = new MockApi();
    const whitebird = client(api, { otp });

    await whitebird.login();
    await whitebird.login();

    const codes = await Promise.all(api.calls(TOKEN).map(async (r) => (await form(r)).code));
    expect(codes).toEqual(['111111', '222222']);
  });

  it('raises MFARequiredError before logging in when 2FA is on and no code is configured', async () => {
    const api = new MockApi({ [MFA_CHECK]: () => Response.json({ mfaEnabled: true }) });

    const error = await client(api)
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(MFARequiredError);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as Error).message).toMatch(/otp/);
    expect(api.calls(TOKEN)).toHaveLength(0);
  });

  it('raises MFARequiredError when the token endpoint asks for a code', async () => {
    const api = new MockApi({
      [MFA_CHECK]: () => new Response(null, { status: 404 }),
      [TOKEN]: () => Response.json({ mfaEnabled: true }),
    });
    await expect(client(api).login()).rejects.toBeInstanceOf(MFARequiredError);
  });

  it('treats invalid_grant as a missing code when the MFA check is unavailable', async () => {
    const api = new MockApi({
      [MFA_CHECK]: () => new Response(null, { status: 500 }),
      [TOKEN]: () =>
        Response.json(
          { error: 'invalid_grant', error_description: 'Bad credentials' },
          { status: 400 },
        ),
    });
    await expect(client(api, { maxRetries: 0 }).login()).rejects.toBeInstanceOf(MFARequiredError);
  });

  it('reports wrong credentials as AuthenticationError', async () => {
    const api = new MockApi({
      [MFA_CHECK]: withoutMfa,
      [CAPTCHA]: captchaImages,
      [TOKEN]: () =>
        Response.json(
          { error: 'invalid_grant', error_description: 'Bad credentials' },
          { status: 400 },
        ),
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

  it('reports a rejected code as AuthenticationError', async () => {
    const invalidGrant = new MockApi({
      [TOKEN]: () =>
        Response.json(
          { error: 'invalid_grant', error_description: 'Invalid code' },
          { status: 400 },
        ),
    });
    const askedAgain = new MockApi({ [TOKEN]: () => Response.json({ mfaEnabled: true }) });

    for (const api of [invalidGrant, askedAgain]) {
      const error = await client(api, { otp: '000000' })
        .login()
        .catch((e: unknown) => e);
      expect(error).toBeInstanceOf(AuthenticationError);
      expect(error).not.toBeInstanceOf(MFARequiredError);
    }
  });

  it('needs credentials or a stored session', async () => {
    const api = new MockApi({ [ME]: subaccount });
    const anonymous = client(api, { username: undefined, password: undefined });
    await expect(anonymous.brokerage.subaccounts.me()).rejects.toThrow(/username/);
  });
});

describe('captcha', () => {
  it('is solved before logging in when the account has no 2FA', async () => {
    const solver = vi.fn((_captcha: Captcha) => 120.4);
    const api = new MockApi({ [MFA_CHECK]: withoutMfa, [CAPTCHA]: captchaImages });

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
    expect(captchaAnswer(api.calls(TOKEN)[0])).toEqual({ sliderPosition: 120, requestId });
  });

  it('is solved again when rejected', async () => {
    const api = new MockApi({
      [MFA_CHECK]: withoutMfa,
      [CAPTCHA]: captchaImages,
      [TOKEN]: [invalidCaptcha, () => Response.json(tokenResponse())],
    });

    await client(api, { captcha: async () => 10 }).login();

    const ids = api.calls(CAPTCHA).map((r) => new URL(r.url).searchParams.get('requestId'));
    const answers = api.calls(TOKEN).map(captchaAnswer);
    expect(answers).toEqual(ids.map((requestId) => ({ sliderPosition: 10, requestId })));
    expect(new Set(ids).size).toBe(2);
  });

  it('gives up after three attempts', async () => {
    const api = new MockApi({
      [MFA_CHECK]: withoutMfa,
      [CAPTCHA]: captchaImages,
      [TOKEN]: invalidCaptcha,
    });

    const error = await client(api, { captcha: () => 10 })
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(CaptchaRequiredError);
    expect(error).toMatchObject({ status: 400, errorCode: 'INVALID_CAPTCHA' });
    expect(api.calls(TOKEN)).toHaveLength(3);
    expect(api.calls(CAPTCHA)).toHaveLength(3);
  });

  it('raises CaptchaRequiredError when a captcha is needed and no solver is configured', async () => {
    const api = new MockApi({ [TOKEN]: invalidCaptcha });

    const error = await client(api)
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(CaptchaRequiredError);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as Error).message).toMatch(/`captcha` solver/);
    expect(api.calls(TOKEN)[0]?.headers.has('captcha')).toBe(false);
    expect(api.calls(TOKEN)).toHaveLength(1);
  });

  it('is required before sending the password when the account has no 2FA', async () => {
    const otp = vi.fn(() => '123456');
    const api = new MockApi({ [MFA_CHECK]: withoutMfa });

    const error = await client(api, { otp })
      .login()
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(CaptchaRequiredError);
    expect((error as Error).message).toMatch(/captcha solver/);
    expect(otp).not.toHaveBeenCalled();
    expect(api.calls(TOKEN)).toHaveLength(0);
  });

  it('replaces the code for accounts without 2FA', async () => {
    const otp = vi.fn(() => '123456');
    const api = new MockApi({ [MFA_CHECK]: withoutMfa, [CAPTCHA]: captchaImages });

    await client(api, { otp, captcha: () => 10 }).login();

    expect(otp).not.toHaveBeenCalled();
    expect(await form(api.calls(TOKEN)[0] as Request)).not.toHaveProperty('code');
    expect(captchaAnswer(api.calls(TOKEN)[0])).toMatchObject({ sliderPosition: 10 });
  });

  it('is not solved for accounts with 2FA', async () => {
    const solver = vi.fn(() => 10);
    const api = new MockApi({ [MFA_CHECK]: () => Response.json({ mfaEnabled: true }) });

    await client(api, { otp: '123456', captcha: solver }).login();

    expect(solver).not.toHaveBeenCalled();
    expect(api.calls(TOKEN)[0]?.headers.has('captcha')).toBe(false);
    expect(await form(api.calls(TOKEN)[0] as Request)).toMatchObject({ code: '123456' });
  });
});

describe('refresh', () => {
  it('refreshes proactively when the token expires within 30 seconds', async () => {
    const fresh = jwt(3600);
    const api = new MockApi({
      [TOKEN]: () => Response.json(tokenResponse(fresh, 'refresh-2')),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(10), refreshToken: 'refresh-1' });
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

  it('fails when the refresh token is rejected and no password is known', async () => {
    const api = new MockApi({
      [TOKEN]: () => Response.json({ error: 'invalid_grant' }, { status: 400 }),
      [ME]: subaccount,
    });
    const store = new MemoryTokenStore({ accessToken: jwt(0), refreshToken: 'revoked' });
    const whitebird = client(api, { tokenStore: store, password: undefined });

    await expect(whitebird.brokerage.subaccounts.me()).rejects.toBeInstanceOf(AuthenticationError);
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

  it('saves tokens after login and clears them on logout', async () => {
    const access = jwt(3600);
    const api = new MockApi({ [TOKEN]: () => Response.json(tokenResponse(access)) });
    const store = new MemoryTokenStore();
    const whitebird = client(api, { tokenStore: store });

    await whitebird.login();
    expect(store.load()).toEqual({
      accessToken: access,
      refreshToken: 'refresh-1',
      expiresAt: expect.any(Number),
    });

    await whitebird.logout();
    expect(store.load()).toBeUndefined();
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

  it('runs before operations that need a token when otp is configured', async () => {
    const api = new MockApi({
      [MFA_TOKEN]: () => Response.json({ mfaToken: 'mfa-1' }),
      [WITHDRAWAL]: () => Response.json({ id: 'w-1' }),
    });

    await expect(withdraw(client(api, { otp: '123456' }))).resolves.toEqual({ id: 'w-1' });

    expect(await api.calls(MFA_TOKEN)[0]?.json()).toEqual({
      operation: 'WITHDRAWAL',
      mfaCode: '123456',
    });
    expect(api.calls(WITHDRAWAL)[0]?.headers.get('x-mfa-token')).toBe('mfa-1');
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
    expect(api.calls(MFA_TOKEN)).toHaveLength(2);
    expect(api.calls(WITHDRAWAL)).toHaveLength(2);
  });
});
