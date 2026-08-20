import { Session, type SessionConfig } from './auth.js';
import {
  APIError,
  AuthenticationError,
  MFARequiredError,
  PermissionDeniedError,
  WhitebirdError,
} from './errors.js';
import { HttpClient, type RequestOptions, readJson } from './http.js';
import { type Page, Paginator } from './pagination.js';
import { type ServerSentEvent, type StreamOptions, subscribe } from './sse.js';
import { VERSION } from './version.js';

/** Description of an API call, as built by the generated resource methods. */
export interface ApiRequest {
  /** Service key from `environments.yaml`, resolved to a base URL. */
  service: string;
  method: string;
  /** Path from the host root, path parameters already substituted. */
  path: string;
  /** Query parameters; arrays repeat the parameter, `undefined` and `null` are skipped. */
  query?: object | undefined;
  /** Header parameters of the operation. */
  headers?: object | undefined;
  body?: unknown;
  /** How `body` is encoded. @defaultValue `'json'` */
  bodyType?: 'json' | 'form' | 'multipart' | undefined;
  /** How the response is decoded. @defaultValue `'json'` */
  responseType?: 'json' | 'text' | 'binary' | 'empty' | undefined;
  /** Whether to send the bearer token. @defaultValue `true` */
  auth?: boolean | undefined;
  /** Response wrapper to unwrap. */
  envelope?: 'retcode' | undefined;
}

/** An {@link ApiRequest} of a paginated endpoint. */
export interface PageRequest extends ApiRequest {
  /** Where the `page` and `size` parameters go. @defaultValue `'query'` */
  pageIn?: 'query' | 'body';
}

export interface CoreConfig extends Omit<SessionConfig, 'http' | 'authUrl' | 'headers'> {
  fetch: typeof fetch;
  timeout: number;
  maxRetries: number;
  /** Base URL of every service. */
  baseUrls: Record<string, string | undefined>;
  authService: string;
}

const USER_AGENT = `whitebird-api-node/${VERSION}`;

/** Executes API calls: URLs, headers, authentication, encoding and decoding. */
export class Core {
  /** Operation MFA token, sent as `x-mfa-token` with every request while set. */
  mfaToken: string | undefined;
  readonly session: Session;
  readonly #http: HttpClient;
  readonly #baseUrls: Record<string, string | undefined>;

  constructor({ fetch, timeout, maxRetries, baseUrls, authService, ...auth }: CoreConfig) {
    this.#http = new HttpClient({ fetch, timeout, maxRetries });
    this.#baseUrls = baseUrls;
    this.session = new Session({
      ...auth,
      http: this.#http,
      authUrl: this.#baseUrl(authService),
      headers: { Accept: 'application/json', 'User-Agent': USER_AGENT },
    });
  }

  /** Performs a call and returns its decoded response. */
  call<T>(request: ApiRequest, options: RequestOptions = {}): Promise<T> {
    const raw = request.responseType === 'binary' || request.responseType === 'text';
    const accept = raw ? '*/*' : 'application/json';
    return this.#authorized(request, isExpiredToken, (token) =>
      this.#http.send(
        this.#url(request),
        this.#init(request, options, token, accept),
        options,
        (response) => decode(response, request) as Promise<T>,
      ),
    );
  }

  /** Starts a paginated call; pages are fetched as the result is awaited or iterated. */
  paginate<T>(
    { pageIn = 'query', ...request }: PageRequest,
    options?: RequestOptions,
  ): Paginator<T> {
    const located = (pageIn === 'body' ? request.body : request.query) as object | undefined;
    const params: Record<string, unknown> = { ...located };
    const start = typeof params.page === 'number' ? params.page : 0;
    return new Paginator<T>((page) => {
      const values = { ...params, page };
      const pageRequest =
        pageIn === 'body' ? { ...request, body: values } : { ...request, query: values };
      return this.call<Page<T>>(pageRequest, options);
    }, start);
  }

  /** Subscribes to a server-sent event stream; connects when iteration starts. */
  stream<T>(request: ApiRequest, options: StreamOptions = {}): AsyncIterable<ServerSentEvent<T>> {
    const { lastEventId, signal, ...rest } = options;
    const connect = (lastId: string | undefined) => {
      const headers = lastId ? { ...rest.headers, 'Last-Event-ID': lastId } : rest.headers;
      const connectOptions = { ...rest, signal, headers, maxRetries: 0 };
      return this.#authorized(request, isRejectedToken, (token) =>
        this.#http.send(
          this.#url(request),
          this.#init(request, connectOptions, token, 'text/event-stream'),
          connectOptions,
          async (response) => response,
        ),
      );
    };
    return { [Symbol.asyncIterator]: () => subscribe<T>(connect, { signal, lastEventId }) };
  }

  /** Sends with a bearer token, and once more with a renewed token if `rejected` says so. */
  async #authorized<T>(
    request: ApiRequest,
    rejected: (error: unknown) => boolean,
    send: (token: string | undefined) => Promise<T>,
  ): Promise<T> {
    if (request.auth === false) return send(undefined);
    const token = await this.session.accessToken();
    try {
      return await send(token);
    } catch (error) {
      if (!rejected(error)) throw error;
      return send(await this.session.renew(token));
    }
  }

  #baseUrl(service: string): string {
    const url = this.#baseUrls[service];
    if (!url) throw new WhitebirdError(`No base URL for service "${service}": set it in baseUrls`);
    return url.replace(/\/+$/, '');
  }

  #url(request: ApiRequest): string {
    const query = new URLSearchParams();
    for (const [name, value] of entries(request.query)) query.append(name, String(value));
    const search = query.toString();
    return this.#baseUrl(request.service) + request.path + (search ? `?${search}` : '');
  }

  #init(
    request: ApiRequest,
    options: RequestOptions,
    token: string | undefined,
    accept: string,
  ): RequestInit & { method: string } {
    const headers = new Headers({ Accept: accept, 'User-Agent': USER_AGENT });
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (this.mfaToken) headers.set('x-mfa-token', this.mfaToken);
    for (const [name, value] of entries(request.headers)) headers.set(name, String(value));
    const body = encodeBody(request.body, request.bodyType ?? 'json');
    if (typeof body === 'string') headers.set('Content-Type', 'application/json');
    for (const [name, value] of Object.entries(options.headers ?? {})) headers.set(name, value);
    return { method: request.method, headers, body };
  }
}

/** Flattens an object into name/value pairs, repeating arrays and skipping missing values. */
function* entries(object: object | undefined): Generator<[string, unknown]> {
  for (const [name, value] of Object.entries(object ?? {})) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null) yield [name, item];
    }
  }
}

function encodeBody(body: unknown, type: 'json' | 'form' | 'multipart'): BodyInit | undefined {
  if (body === undefined) return undefined;
  if (body instanceof Blob || body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
    return body as BodyInit;
  }
  if (type === 'json') return JSON.stringify(body);
  if (type === 'form') {
    const form = new URLSearchParams();
    for (const [name, value] of entries(body as object)) form.append(name, String(value));
    return form;
  }
  const form = new FormData();
  for (const [name, value] of entries(body as object)) {
    if (value instanceof Blob) form.append(name, value);
    else if (typeof value === 'object') {
      form.append(name, new Blob([JSON.stringify(value)], { type: 'application/json' }));
    } else form.append(name, String(value));
  }
  return form;
}

async function decode(response: Response, request: ApiRequest): Promise<unknown> {
  switch (request.responseType) {
    case 'empty':
      await response.body?.cancel();
      return undefined;
    case 'text':
      return response.text();
    case 'binary':
      return new Uint8Array(await response.arrayBuffer());
    default: {
      const data = await readJson(response);
      return request.envelope === 'retcode' ? unwrapRetCode(data, response) : data;
    }
  }
}

function unwrapRetCode(data: unknown, response: Response): unknown {
  const { retCode, retMsg, result } = (data ?? {}) as Record<string, unknown>;
  if (retCode === 0) return result;
  const details = { status: response.status, body: data, headers: response.headers };
  if (typeof retCode !== 'number') {
    throw new APIError('The response is not a {retCode, retMsg, result} envelope', details);
  }
  const message = typeof retMsg === 'string' && retMsg ? retMsg : `retCode ${retCode}`;
  throw new APIError(message, { ...details, code: retCode });
}

function isExpiredToken(error: unknown): boolean {
  return (
    error instanceof AuthenticationError &&
    !(error instanceof MFARequiredError) &&
    error.status === 401
  );
}

function isRejectedToken(error: unknown): boolean {
  return isExpiredToken(error) || error instanceof PermissionDeniedError;
}
