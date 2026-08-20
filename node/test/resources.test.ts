import { describe, expect, it } from 'vitest';
import packageJson from '../package.json' with { type: 'json' };
import { APIError, VERSION, WhitebirdError } from '../src/index.js';
import { client, MockApi } from './helpers.js';

const BROKERAGE = 'https://brokerage-service.whitebird.io';

describe('generated methods', () => {
  it('posts JSON bodies and returns the parsed response', async () => {
    const order = { uuid: 'otc-1', status: 'NEW' };
    const api = new MockApi({ 'POST /api/v1/brokerage/otc-order': () => Response.json(order) });
    const params = {
      subaccountId: 'sub-1',
      fromAsset: 'USDT',
      fromAmount: 100,
      toAsset: 'BTC',
      exchangeRatePair: 'BTC/USDT',
      exchangeRateValue: 100_000,
    };

    await expect(client(api).brokerage.otcOrders.create(params)).resolves.toEqual(order);

    const [request] = api.calls('POST /api/v1/brokerage/otc-order');
    expect(request?.url).toBe(`${BROKERAGE}/api/v1/brokerage/otc-order`);
    expect(request?.headers.get('content-type')).toBe('application/json');
    expect(request?.headers.get('accept')).toBe('application/json');
    expect(request?.headers.get('user-agent')).toBe(`whitebird-api-node/${VERSION}`);
    expect(await request?.json()).toEqual(params);
  });

  it('splits params into query and body', async () => {
    const api = new MockApi({
      'POST /api/v1/brokerage/orders/current/report': () => new Response(new Uint8Array([1, 2])),
    });

    const report = await client(api).brokerage.orders.downloadReport({
      fileType: 'PDF',
      statuses: ['COMPLETED'],
    });

    expect(report).toEqual(new Uint8Array([1, 2]));
    const [request] = api.calls('POST /api/v1/brokerage/orders/current/report');
    expect(new URL(request?.url ?? '').search).toBe('?fileType=PDF');
    expect(await request?.json()).toEqual({ statuses: ['COMPLETED'] });
    expect(request?.headers.get('accept')).toBe('*/*');
  });

  it('encodes path parameters and returns nothing for empty responses', async () => {
    const api = new MockApi({
      'DELETE /api/v1/brokerage/crypto-address/a%2Fb': () => new Response(null, { status: 200 }),
    });
    await expect(client(api).brokerage.cryptoAddresses.delete('a/b')).resolves.toBeUndefined();
  });

  it('repeats array query parameters', async () => {
    const api = new MockApi({
      'GET /api/v1/brokerage/subaccount/page': () => Response.json({ content: [], last: true }),
    });

    await client(api).brokerage.subaccounts.list({ size: 5, sort: ['customId,asc', 'email'] });

    const [request] = api.calls('GET /api/v1/brokerage/subaccount/page');
    expect(new URL(request?.url ?? '').searchParams.getAll('sort')).toEqual([
      'customId,asc',
      'email',
    ]);
  });
});

describe('base URLs', () => {
  const route = { 'GET /ping': () => Response.json('pong') };

  it('uses demo hosts in the demo environment and production hosts elsewhere', async () => {
    const api = new MockApi(route);
    const demo = client(api, { environment: 'demo' });

    await demo.request('trading', 'GET', '/ping', { auth: false });
    await demo.request('brokerage', 'GET', '/ping', { auth: false });

    expect(api.requests.map((r) => new URL(r.url).origin)).toEqual([
      'https://trading-demo-service.whitebird.io',
      BROKERAGE,
    ]);
  });

  it('can be overridden per service, also for services the SDK does not know', async () => {
    const api = new MockApi({ ...route, 'GET /brokerage/ping': () => Response.json('pong') });
    const whitebird = client(api, {
      baseUrls: { brokerage: 'https://proxy.test/brokerage/', reports: 'https://reports.test' },
    });

    await whitebird.request('brokerage', 'GET', '/ping', { auth: false });
    await whitebird.request('reports', 'GET', '/ping', { auth: false });

    expect(api.requests.map((r) => r.url)).toEqual([
      'https://proxy.test/brokerage/ping',
      'https://reports.test/ping',
    ]);
  });

  it('fails clearly for unknown services', async () => {
    const whitebird = client(new MockApi());
    await expect(whitebird.request('nope', 'GET', '/ping', { auth: false })).rejects.toThrow(
      WhitebirdError,
    );
  });
});

describe('request()', () => {
  it('calls any endpoint with authentication', async () => {
    const api = new MockApi({
      'PUT /api/v1/custom': async (request) => Response.json({ echo: await request.json() }),
    });

    const result = await client(api).request<{ echo: unknown }>(
      'brokerage',
      'put',
      '/api/v1/custom',
      {
        query: { a: 1, skipped: undefined },
        body: { b: 2 },
        headers: { 'Accept-Language': 'en' },
      },
    );

    expect(result).toEqual({ echo: { b: 2 } });
    const [request] = api.calls('PUT /api/v1/custom');
    expect(new URL(request?.url ?? '').search).toBe('?a=1');
    expect(request?.headers.get('authorization')).toMatch(/^Bearer /);
    expect(request?.headers.get('accept-language')).toBe('en');
  });

  it('sends form and multipart bodies', async () => {
    const api = new MockApi({
      'POST /form': async (request) => Response.json(Object.fromEntries(await request.formData())),
      'POST /upload': async (request) => {
        const data = await request.formData();
        const file = data.get('file') as File;
        return new Response(`${data.get('note')}:${await file.text()}`);
      },
    });
    const whitebird = client(api);

    const fields = await whitebird.request('brokerage', 'POST', '/form', {
      body: { address: 'x', tags: ['a', 'b'], memo: undefined },
      bodyType: 'form',
    });
    const text = await whitebird.request('brokerage', 'POST', '/upload', {
      body: { file: new Blob(['hello']), note: 'avatar' },
      bodyType: 'multipart',
      responseType: 'text',
    });

    expect(fields).toEqual({ address: 'x', tags: 'b' });
    expect(text).toBe('avatar:hello');
    expect(api.calls('POST /form')[0]?.headers.get('content-type')).toMatch(
      /application\/x-www-form-urlencoded/,
    );
  });
});

describe('retCode envelope', () => {
  const ticker = { symbol: 'BTCUSDT', lastPrice: '100000' };
  const get = (api: MockApi) =>
    client(api).request('market_data', 'GET', '/tickers', { auth: false, envelope: 'retcode' });

  it('unwraps result', async () => {
    const api = new MockApi({
      'GET /tickers': () => Response.json({ retCode: 0, retMsg: 'OK', result: [ticker] }),
    });
    await expect(get(api)).resolves.toEqual([ticker]);
  });

  it('raises APIError for a non-zero retCode on HTTP 200', async () => {
    const api = new MockApi({
      'GET /tickers': () => Response.json({ retCode: 10001, retMsg: 'Invalid symbol', result: {} }),
    });

    const error = await get(api).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(APIError);
    expect(error).toMatchObject({ status: 200, code: 10001, message: 'Invalid symbol' });
  });
});

it('reports the package version', () => {
  expect(VERSION).toBe(packageJson.version);
});
