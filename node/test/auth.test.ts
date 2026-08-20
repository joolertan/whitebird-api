import { describe, expect, it, vi } from 'vitest';
import { AuthenticationError, MemoryTokenStore, MFARequiredError } from '../src/index.js';
import { bearer, client, form, jwt, MockApi, tokenResponse } from './helpers.js';

const ME = 'GET /api/v1/brokerage/subaccount/me';
const TOKEN = 'POST /oauth/token';
const MFA_CHECK = 'POST /api/v1/client/mfa';
const subaccount = () => Response.json({ uuid: 'sub-1' });

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

  it('sends a static code and skips the MFA check', async () => {
    const api = new MockApi({ [ME]: subaccount });
    await client(api, { otp: '123456' }).brokerage.subaccounts.me();

    expect(await form(api.calls(TOKEN)[0] as Request)).toMatchObject({ code: '123456' });
    expect(api.calls(MFA_CHECK)).toHaveLength(0);
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
      [TOKEN]: () =>
        Response.json(
          { error: 'invalid_grant', error_description: 'Bad credentials' },
          { status: 400 },
        ),
    });

    const error = await client(api)
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
    const api = new MockApi({
      [ME]: () =>
        Response.json({ message: 'Operation denied, requires mfa checking' }, { status: 403 }),
    });
    await expect(client(api).brokerage.subaccounts.me()).rejects.toBeInstanceOf(MFARequiredError);
  });
});
