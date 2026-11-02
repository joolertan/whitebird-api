import { Whitebird, type WhitebirdOptions } from '../src/index.js';

type Handler = (request: Request) => Response | Promise<Response>;

/** A fake API: routes are keyed by `METHOD /path`; a list of handlers answers calls in order. */
export class MockApi {
  readonly requests: Request[] = [];
  readonly #routes = new Map<string, Handler | Handler[]>();

  constructor(routes: Record<string, Handler | Handler[]> = {}) {
    this.route('POST /oauth/token', () => Response.json(tokenResponse()));
    // An account with 2FA whose first password grant already returns tokens.
    this.route('POST /api/v1/client/mfa', () => Response.json({ mfaEnabled: true }));
    for (const [route, handler] of Object.entries(routes)) this.route(route, handler);
  }

  route(route: string, handler: Handler | Handler[]): this {
    this.#routes.set(route, handler);
    return this;
  }

  /** Requests received on a route. */
  calls(route: string): Request[] {
    return this.requests.filter((request) => key(request) === route);
  }

  readonly fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(input, init);
    this.requests.push(request.clone());
    const handlers = this.#routes.get(key(request));
    const handler = Array.isArray(handlers) ? handlers.shift() : handlers;
    if (!handler) return Response.json({ message: `unexpected ${key(request)}` }, { status: 501 });
    const response = await handler(request);
    if (!response.body) return response;
    // Like a real fetch, aborting the request errors the body that is still being read.
    const body = response.body.pipeThrough(new TransformStream(), { signal: request.signal });
    return new Response(body, response);
  };
}

export function client(api: MockApi, options: WhitebirdOptions = {}): Whitebird {
  return new Whitebird({
    username: 'Trader@Example.com',
    password: 'secret',
    fetch: api.fetch,
    ...options,
  });
}

/** An unsigned JWT that expires in `seconds`. */
export function jwt(seconds: number, subject = 'trader'): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const exp = Math.floor(Date.now() / 1000) + seconds;
  return `${encode({ alg: 'none' })}.${encode({ sub: subject, exp })}.signature`;
}

export function tokenResponse(accessToken = jwt(3600), refreshToken = 'refresh-1') {
  return {
    access_token: accessToken,
    refresh_token: refreshToken,
    token_type: 'bearer',
    expires_in: 3600,
  };
}

export function bearer(request: Request): string | undefined {
  return request.headers.get('authorization')?.replace(/^Bearer /, '');
}

export async function form(request: Request): Promise<Record<string, string>> {
  return Object.fromEntries(new URLSearchParams(await request.text()));
}

function key(request: Request): string {
  return `${request.method} ${new URL(request.url).pathname}`;
}
