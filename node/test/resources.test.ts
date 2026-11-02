import { describe, expect, it } from 'vitest';
import packageJson from '../package.json' with { type: 'json' };
import { APIError, VERSION, WhitebirdError } from '../src/index.js';
import { JsonCodec } from '../src/json.js';
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
      exchangeRateValue: '100000',
    };

    await expect(client(api).brokerage.otcOrders.create(params)).resolves.toEqual(order);

    const [request] = api.calls('POST /api/v1/brokerage/otc-order');
    expect(request?.url).toBe(`${BROKERAGE}/api/v1/brokerage/otc-order`);
    expect(request?.headers.get('content-type')).toBe('application/json');
    expect(request?.headers.get('accept')).toBe('application/json');
    expect(request?.headers.get('user-agent')).toBe(`whitebird-api-node/${VERSION}`);
    expect(await request?.json()).toEqual({ ...params, exchangeRateValue: 100_000 });
  });

  it('keeps every digit of decimals, both ways', async () => {
    const route = 'POST /api/v1/brokerage/otc-order';
    const api = new MockApi({
      [route]: () =>
        new Response(
          '{"uuid":"otc-1","fromAmount":0.123456789012345678,"toAmount":12345678.123456789,' +
            '"exchangeRateValue":1.10,"extra":{"big":12345678.123456789}}',
          { headers: { 'content-type': 'application/json' } },
        ),
    });

    const order = await client(api).brokerage.otcOrders.create({
      subaccountId: 'sub-1',
      fromAsset: 'USDT',
      fromAmount: '0.30000000000000000001',
      toAsset: 'BTC',
      exchangeRatePair: 'BTC/USDT',
      exchangeRateValue: 0.5,
    });

    expect(order).toMatchObject({
      fromAmount: '0.123456789012345678',
      toAmount: '12345678.123456789',
      exchangeRateValue: '1.10',
      extra: { big: 12345678.12345679 },
    });
    const sent = await api.calls(route)[0]?.text();
    expect(sent).toContain('"fromAmount":0.30000000000000000001');
    expect(sent).toContain('"exchangeRateValue":0.5');
  });

  it('reads decimals in maps as strings', async () => {
    const api = new MockApi({
      'GET /api/v1/brokerage/broker/balances': () =>
        new Response('{"BTC":1,"ETH":0.123456789012345678}'),
    });
    await expect(client(api).brokerage.balances()).resolves.toEqual({
      BTC: '1',
      ETH: '0.123456789012345678',
    });
  });

  it('rejects decimal strings that are not numbers', async () => {
    const api = new MockApi();
    const create = client(api).brokerage.otcOrders.create({
      subaccountId: 'sub-1',
      fromAsset: 'USDT',
      fromAmount: '1,5',
      toAsset: 'BTC',
      exchangeRatePair: 'BTC/USDT',
      exchangeRateValue: 1,
    });
    await expect(create).rejects.toThrow(/fromAmount must be a decimal number/);
  });

  it('sends the spec defaults of parameters the caller leaves unset', async () => {
    const route = 'GET /api/v1/orders/history/page';
    const api = new MockApi({ [route]: () => Response.json({ content: [], last: true }) });

    await client(api).trading.orders.list({ sortDir: 'ASC', sortBy: undefined });

    const query = new URL(api.calls(route)[0]?.url ?? '').searchParams;
    expect(Object.fromEntries(query)).toEqual({
      page: '0',
      size: '30',
      sortBy: 'exchangeTime',
      sortDir: 'ASC',
    });
  });

  it('sends the spec defaults of body fields the caller leaves unset', async () => {
    const route = 'POST /api/v2/exchange/client/balance/crypto/reject';
    const api = new MockApi({ [route]: () => new Response('') });

    await client(api).balance.operations.rejectCrypto({ transactionId: 'tx-1' });

    expect(await api.calls(route)[0]?.json()).toEqual({
      transactionId: 'tx-1',
      operationType: 'crypto',
    });
  });

  it('returns the text of untyped responses that are not JSON', async () => {
    const route = 'POST /api/v2/exchange/client/balance/crypto/reject';
    const api = new MockApi({
      [route]: () => new Response('9f0c-uuid', { headers: { 'content-type': 'application/json' } }),
    });
    await expect(
      client(api).balance.operations.rejectCrypto({ transactionId: 'tx-1' }),
    ).resolves.toBe('9f0c-uuid');
  });

  it('sends bracketed query names as the web app does', async () => {
    const route = 'GET /api/v1/ico/emission/stable-coin/available';
    const api = new MockApi({ [route]: () => Response.json({ AED: true }) });

    await client(api).ico.emissions.stableCoinAvailability({ assets: ['AED', 'RUBS'] });

    const query = new URL(api.calls(route)[0]?.url ?? '').searchParams;
    expect(query.getAll('assets[]')).toEqual(['AED', 'RUBS']);
    expect(query.has('assets')).toBe(false);
  });

  it('rejects path parameters that would change the path', async () => {
    const api = new MockApi();
    for (const value of ['', '.', '..']) {
      await expect(client(api).brokerage.cryptoAddresses.delete(value)).rejects.toBeInstanceOf(
        WhitebirdError,
      );
    }
    expect(api.requests).toHaveLength(0);
  });

  it('sends dates as the web app formats them', async () => {
    const api = new MockApi({ 'POST /dates': () => new Response(null) });
    const at = new Date(Date.UTC(2026, 0, 2, 3, 4, 5, 678));

    await client(api).request('brokerage', 'POST', '/dates', {
      query: { from: at },
      body: { from: at, nested: [{ at }] },
      responseType: 'empty',
    });

    const [request] = api.calls('POST /dates');
    expect(new URL(request?.url ?? '').searchParams.get('from')).toBe('2026-01-02T03:04:05+0000');
    expect(await request?.json()).toEqual({
      from: '2026-01-02T03:04:05+0000',
      nested: [{ at: '2026-01-02T03:04:05+0000' }],
    });
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

  it('sends an empty object as the body of operations without body fields', async () => {
    const api = new MockApi({ 'POST /api/v1/exchange/rate/stats': () => Response.json([]) });

    await client(api).exchange.rates.stats();

    const [request] = api.calls('POST /api/v1/exchange/rate/stats');
    expect(request?.headers.get('content-type')).toBe('application/json');
    expect(await request?.text()).toBe('{}');
  });

  it('sends an object body even when no field is set', async () => {
    const api = new MockApi({ 'POST /api/v2/kyc/liveness/start': () => Response.json({}) });

    await client(api).kyc.liveness.start();

    expect(await api.calls('POST /api/v2/kyc/liveness/start')[0]?.text()).toBe('{}');
  });

  it('sends multipart parts with their declared media type', async () => {
    const route = 'POST /api/v2/qualification';
    const api = new MockApi({ [route]: () => new Response(null) });
    const request = {
      requestedInvestorStatus: 'QUALIFIED_INVESTOR',
      certifiedExpert: true,
    } as const;

    await client(api).kyc.qualification.submit({
      request,
      files: [
        new File(['%PDF'], 'diploma.pdf', { type: 'application/pdf' }),
        new File(['%PDF'], 'contract.pdf', { type: 'application/pdf' }),
      ],
    });

    const data = await api.calls(route)[0]?.formData();
    const part = data?.get('request') as File;
    const file = data?.get('files') as File;
    expect(part.type).toBe('application/json');
    expect(JSON.parse(await part.text())).toEqual(request);
    expect(data?.getAll('files')).toHaveLength(2);
    expect([file.name, file.type, await file.text()]).toEqual([
      'diploma.pdf',
      'application/pdf',
      '%PDF',
    ]);
  });

  it('sends JSON parts declared as text as plain text fields', async () => {
    const route = 'POST /api/v1/support';
    const api = new MockApi({ [route]: () => Response.json({ id: 's-1' }) });

    await client(api).support.send({
      recaptcha: 'token',
      request: { name: 'Me', email: 'me@example.com', topic: 'other', message: 'Hello' },
      files: [new Uint8Array([1, 2, 3])],
    });

    const data = (await api.calls(route)[0]?.formData()) as FormData;
    const request = data.get('request');
    expect(typeof request).toBe('string');
    expect(JSON.parse(request as string)).toMatchObject({ email: 'me@example.com' });
    const file = data.get('files') as File;
    expect(file).toBeInstanceOf(File);
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
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

describe('JsonCodec', () => {
  const codec = new JsonCodec({
    Order: { total: '#', lines: ['array', 'Line'] },
    Line: { price: '#' },
  });

  it('reads decimals at the places of the shape', () => {
    const text = '{"total":1e-7,"lines":[{"price":2.50,"qty":3}],"other":0.1}';
    expect(codec.parse(text, 'Order')).toEqual({
      total: '1e-7',
      lines: [{ price: '2.50', qty: 3 }],
      other: 0.1,
    });
    expect(codec.parse('[1, 2.0]', ['array', '#'])).toEqual(['1', '2.0']);
    expect(codec.parse('{"__proto__":{"total":1}}', 'Order')).toEqual(
      JSON.parse('{"__proto__":{"total":1}}'),
    );
  });

  it('writes decimal strings as numbers with their digits', () => {
    expect(
      codec.stringify({ total: '-0.000000000000000000001', lines: [{ price: 7 }] }, 'Order'),
    ).toBe('{"total":-0.000000000000000000001,"lines":[{"price":7}]}');
    expect(() => codec.stringify({ total: '1.' }, 'Order')).toThrow(TypeError);
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

  it('uses the demo accounting host only for the spot balance', async () => {
    const api = new MockApi({
      'POST /api/v2/accounting/client/account/enhanced': () => Response.json([]),
      'POST /api/v2/auth/mfa/merchant/client/token': () => Response.json({ mfaToken: 'm' }),
    });
    const demo = client(api, { environment: 'demo' });

    await demo.accounting.accounts.getEnhanced({ type: 'USER_BALANCE' });
    await demo.confirmOperation('WITHDRAWAL', '123456');

    const hosts = api.requests.map((r) => `${new URL(r.url).host}${new URL(r.url).pathname}`);
    expect(hosts).toContain('api-demo.whitebird.io/api/v2/accounting/client/account/enhanced');
    expect(hosts).toContain('api.whitebird.io/api/v2/auth/mfa/merchant/client/token');
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

  it('sends FormData, URLSearchParams, streams and bytes as they are', async () => {
    const api = new MockApi({
      'POST /raw': async (request) =>
        new Response(`${request.headers.get('content-type')}|${await request.text()}`),
    });
    const whitebird = client(api);
    const send = (body: unknown, bodyType?: 'json' | 'form' | 'multipart') =>
      whitebird.request<string>('brokerage', 'POST', '/raw', { body, bodyType });

    const form = new FormData();
    form.append('a', '1');
    expect(await send(form, 'multipart')).toMatch(/^multipart\/form-data.*name="a"\r\n\r\n1/s);
    expect(await send(new URLSearchParams({ a: '1' }))).toBe(
      'application/x-www-form-urlencoded;charset=UTF-8|a=1',
    );
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('streamed'));
        controller.close();
      },
    });
    expect(await send(stream)).toBe('null|streamed');
    expect(await send(new TextEncoder().encode('bytes'))).toBe('null|bytes');
  });

  it('sends one part per file, and other objects as JSON parts', async () => {
    const api = new MockApi({ 'POST /upload': () => new Response(null) });

    await client(api).request('brokerage', 'POST', '/upload', {
      body: {
        files: [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')],
        filter: { assets: ['BTC'] },
        tags: ['x', 'y'],
        none: [],
        count: 2,
      },
      bodyType: 'multipart',
      responseType: 'empty',
    });

    const data = (await api.calls('POST /upload')[0]?.formData()) as FormData;
    const files = data.getAll('files') as File[];
    expect(await Promise.all(files.map((f) => f.text()))).toEqual(['a', 'b']);
    expect(files.map((f) => f.name)).toEqual(['a.txt', 'b.txt']);
    for (const [name, json] of [
      ['filter', { assets: ['BTC'] }],
      ['tags', ['x', 'y']],
    ] as const) {
      const part = data.get(name) as File;
      expect(part.type).toBe('application/json');
      expect(JSON.parse(await part.text())).toEqual(json);
    }
    expect(data.has('none')).toBe(false);
    expect(data.get('count')).toBe('2');
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

  it('accepts retCode 200 and a missing retCode as success', async () => {
    for (const body of [
      { retCode: 200, result: [ticker] },
      { retCode: '0', retMsg: 'OK', result: [ticker] },
      { result: [ticker] },
    ]) {
      const api = new MockApi({ 'GET /tickers': () => Response.json(body) });
      await expect(get(api)).resolves.toEqual([ticker]);
    }
    const bare = new MockApi({ 'GET /tickers': () => Response.json({ ok: true }) });
    await expect(get(bare)).resolves.toEqual({ ok: true });
  });

  it('takes the message of a failed envelope from retMsg or message', async () => {
    const api = new MockApi({
      'GET /tickers': () => Response.json({ code: 10002, message: 'Invalid category' }),
    });
    await expect(get(api)).rejects.toMatchObject({ code: 10002, message: 'Invalid category' });
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
