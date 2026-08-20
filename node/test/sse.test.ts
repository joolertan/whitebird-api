import { describe, expect, it } from 'vitest';
import type { Core } from '../src/core.js';
import { AuthenticationError, MemoryTokenStore, type ServerSentEvent } from '../src/index.js';
import { EventParser, lines } from '../src/sse.js';
import { bearer, client, jwt, MockApi, tokenResponse } from './helpers.js';

const CONNECT = 'GET /api/v1/sse/connect';
const encoder = new TextEncoder();

function stream(chunks: string[], onCancel?: () => void): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      if (!onCancel) controller.close();
    },
    cancel: onCancel,
  });
}

const events = (body: string, onCancel?: () => void) =>
  new Response(stream([body], onCancel), { headers: { 'Content-Type': 'text/event-stream' } });

function subscribe(api: MockApi, options: { signal?: AbortSignal; lastEventId?: string } = {}) {
  const whitebird = client(api);
  const core = (whitebird as unknown as { _client: Core })._client;
  return core.stream<{ orderId?: string }>(
    { service: 'notification', method: 'GET', path: '/api/v1/sse/connect' },
    options,
  );
}

async function take<T>(iterable: AsyncIterable<T>, count: number): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) {
    items.push(item);
    if (items.length === count) break;
  }
  return items;
}

describe('event stream parsing', () => {
  async function parse(chunks: string[]) {
    const parser = new EventParser();
    const parsed = [];
    for await (const line of lines(stream(chunks))) {
      const event = parser.push(line);
      if (event) parsed.push(event);
    }
    return { parsed, parser };
  }

  it('follows the text/event-stream rules', async () => {
    const { parsed, parser } = await parse([
      '﻿: comment\n',
      'event: order-updates\nid: 1\ndata: {"a":',
      '1}\n\n',
      'data: first\r\ndata:second\r',
      '\n\r\n',
      'retry: 2500\nid\n\n',
      'event: ignored\n\n',
      'data: unterminated',
    ]);

    expect(parsed).toEqual([
      { event: 'order-updates', data: '{"a":1}', id: '1' },
      { event: 'message', data: 'first\nsecond', id: '1' },
    ]);
    expect(parser.retry).toBe(2500);
    expect(parser.lastEventId).toBe('');
  });
});

describe('streams', () => {
  it('connect with the bearer token and yield parsed events', async () => {
    const api = new MockApi({
      [CONNECT]: () =>
        events(
          'event: sse-status\ndata: connected\n\nevent: order-updates\ndata: {"orderId":"o-1"}\n\n',
        ),
    });

    const received = await take(subscribe(api), 2);

    expect(received).toEqual([
      { event: 'sse-status', data: 'connected', id: undefined },
      { event: 'order-updates', data: { orderId: 'o-1' }, id: undefined },
    ]);
    const [request] = api.calls(CONNECT);
    expect(request?.url).toBe('https://notification-service.whitebird.io/api/v1/sse/connect');
    expect(request?.headers.get('accept')).toBe('text/event-stream');
    expect(bearer(request as Request)).toBeTruthy();
  });

  it('reconnect with Last-Event-ID after the connection ends', async () => {
    const api = new MockApi({
      [CONNECT]: [
        () => events('retry: 0\nid: 7\ndata: {"orderId":"o-1"}\n\n'),
        () => new Response(null, { status: 503 }),
        () => events('id: 8\ndata: {"orderId":"o-2"}\n\n'),
      ],
    });

    const received = await take(subscribe(api, { lastEventId: '6' }), 2);

    expect(received.map((event) => event.id)).toEqual(['7', '8']);
    expect(api.calls(CONNECT).map((r) => r.headers.get('last-event-id'))).toEqual(['6', '7', '7']);
  });

  it('end when the signal aborts', async () => {
    const controller = new AbortController();
    const api = new MockApi({ [CONNECT]: () => events('data: 1\n\n', () => {}) });
    const received: ServerSentEvent[] = [];

    for await (const event of subscribe(api, { signal: controller.signal })) {
      received.push(event);
      controller.abort();
    }

    expect(received).toHaveLength(1);
  });

  it('close the connection when the loop breaks', async () => {
    let cancelled = false;
    const api = new MockApi({
      [CONNECT]: () =>
        events('data: 1\n\n', () => {
          cancelled = true;
        }),
    });

    await take(subscribe(api), 1);

    expect(cancelled).toBe(true);
    expect(api.calls(CONNECT)).toHaveLength(1);
  });

  it('refresh the token once on 401 or 403, then stop', async () => {
    const stale = jwt(3600, 'stale');
    const fresh = jwt(3600, 'fresh');
    const store = () => new MemoryTokenStore({ accessToken: stale, refreshToken: 'refresh-1' });
    const refreshed = new MockApi({
      'POST /oauth/token': () => Response.json(tokenResponse(fresh)),
      [CONNECT]: (request) =>
        bearer(request) === fresh ? events('data: 1\n\n') : new Response(null, { status: 403 }),
    });
    const rejected = new MockApi({ [CONNECT]: () => new Response(null, { status: 401 }) });

    const core = (api: MockApi) =>
      (client(api, { tokenStore: store() }) as unknown as { _client: Core })._client;
    const request = { service: 'notification', method: 'GET', path: '/api/v1/sse/connect' };

    await expect(take(core(refreshed).stream(request), 1)).resolves.toHaveLength(1);
    await expect(take(core(rejected).stream(request), 1)).rejects.toBeInstanceOf(
      AuthenticationError,
    );
    expect(rejected.calls(CONNECT)).toHaveLength(2);
  });
});
