import {
  type CodeProvider,
  resolveCode,
  Session,
  type SessionConfig,
  type TokenResponse,
} from './auth.js';
import {
  APIError,
  AuthenticationError,
  MFARequiredError,
  PermissionDeniedError,
  WhitebirdError,
} from './errors.js';
import { HttpClient, type RequestOptions } from './http.js';
import { formatScalar, JsonCodec, type Shape, type Shapes } from './json.js';
import { type Page, Paginator } from './pagination.js';
import { type ServerSentEvent, type StreamOptions, subscribe } from './sse.js';
import { VERSION } from './version.js';

/** Description of an API call, as built by the generated resource methods. */
export interface ApiRequest {
  /** Service key from `environments.yaml`, resolved to a base URL. */
  service: string;
  method: string;
  /** Path from the host root; `{name}` placeholders are filled in from `pathParams`. */
  path: string;
  /** Values of the `{name}` placeholders in `path`, each encoded as one path segment. */
  pathParams?: Record<string, string | number> | undefined;
  /** Query parameters; arrays repeat the parameter, `undefined` and `null` are skipped. */
  query?: object | undefined;
  /** Header parameters of the operation. */
  headers?: object | undefined;
  /**
   * Request body: encoded as `bodyType` says, except `FormData`, `URLSearchParams`, `Blob`,
   * `ArrayBuffer`, typed arrays and `ReadableStream`, which are sent as they are.
   */
  body?: unknown;
  /** How `body` is encoded. @defaultValue `'json'` */
  bodyType?: 'json' | 'form' | 'multipart' | undefined;
  /** Media types of the multipart parts that declare one. */
  partTypes?: Record<string, string> | undefined;
  /**
   * How the response is decoded: `json` parses JSON, `auto` parses JSON and returns the text
   * when it is not JSON, `string` returns a JSON string or else the text, `text` and `binary`
   * return the body as it is and `empty` discards it.
   * @defaultValue `'json'`
   */
  responseType?: 'json' | 'auto' | 'string' | 'text' | 'binary' | 'empty' | undefined;
  /** Whether to send the bearer token. @defaultValue `true` */
  auth?: boolean | undefined;
  /** Response wrapper to unwrap. */
  envelope?: 'retcode' | undefined;
  /** Operation that needs an operation MFA token, such as `WITHDRAWAL`. */
  mfa?: string | undefined;
  /** Values sent for query parameters, headers and body fields the caller left `undefined`. */
  defaults?:
    | { query?: object | undefined; headers?: object | undefined; body?: object | undefined }
    | undefined;
  /**
   * Where decimals sit in the body, to send them with their exact digits.
   * @internal
   */
  bodyShape?: Shape | undefined;
  /**
   * Where decimals sit in the response, to read them as strings with their exact digits.
   * @internal
   */
  responseShape?: Shape | undefined;
  /**
   * What the call does to the session: `end` forgets it afterwards, whatever the outcome, and
   * `replace` keeps the tokens of the response as the new session.
   */
  session?: 'end' | 'replace' | undefined;
}

/** An {@link ApiRequest} of a paginated endpoint. */
export interface PageRequest extends ApiRequest {
  /** Where the `page` and `size` parameters go. @defaultValue `'query'` */
  pageIn?: 'query' | 'body';
}

/** Method and location of an endpoint without parameters. */
export interface Endpoint {
  service: string;
  method: string;
  path: string;
}

export interface CoreConfig extends Omit<SessionConfig, 'http' | 'authUrl' | 'headers' | 'onEnd'> {
  fetch: typeof fetch;
  timeout: number;
  maxRetries: number;
  /** Base URL of every service. */
  baseUrls: Record<string, string | undefined>;
  authService: string;
  /** Endpoint that exchanges a two-factor code for an operation MFA token. */
  operationMfa: { service: string; tokenPath: string };
  /** Endpoint that ends the session on the server. */
  logout?: Endpoint | undefined;
  /** Value of the `Accept-Language` header. */
  language?: string | undefined;
  /** Where decimals sit in the models. */
  shapes?: Shapes | undefined;
}

const USER_AGENT = `whitebird-api-node/${VERSION}`;

/** Executes API calls: URLs, headers, authentication, encoding and decoding. */
export class Core {
  /** Operation MFA token, sent as `x-mfa-token` with every request while set. */
  mfaToken: string | undefined;
  readonly session: Session;
  readonly #http: HttpClient;
  readonly #json: JsonCodec;
  readonly #headers: Record<string, string>;
  readonly #baseUrls: Record<string, string | undefined>;
  readonly #operationMfa: CoreConfig['operationMfa'];
  readonly #logout: Endpoint | undefined;
  readonly #otp: CodeProvider | undefined;
  /** Confirmations in progress, by operation, shared by the calls refused at the same time. */
  readonly #confirming = new Map<string, Promise<string>>();

  constructor(config: CoreConfig) {
    const {
      fetch,
      timeout,
      maxRetries,
      baseUrls,
      authService,
      operationMfa,
      logout,
      language,
      shapes,
      ...auth
    } = config;
    this.#http = new HttpClient({ fetch, timeout, maxRetries });
    this.#json = new JsonCodec(shapes);
    this.#headers = { 'User-Agent': USER_AGENT, ...(language && { 'Accept-Language': language }) };
    this.#baseUrls = baseUrls;
    this.#operationMfa = operationMfa;
    this.#logout = logout;
    this.#otp = auth.otp;
    this.session = new Session({
      ...auth,
      http: this.#http,
      authUrl: this.#baseUrl(authService),
      headers: { Accept: 'application/json', ...this.#headers },
      onEnd: () => {
        this.mfaToken = undefined;
        this.#confirming.clear();
      },
    });
  }

  /**
   * Performs a call and returns its decoded response. When an operation is refused for want of
   * an operation MFA token and an OTP provider is configured, the client gets a token and sends
   * the operation once more.
   */
  async call<T>(request: ApiRequest, options: RequestOptions = {}): Promise<T> {
    const prepared = withDefaults(request);
    if (prepared.session === 'end') {
      try {
        return await this.#confirmed<T>(prepared, options);
      } finally {
        await this.endSession();
      }
    }
    const generation = this.session.generation;
    const result = await this.#confirmed<T>(prepared, options);
    if (prepared.session === 'replace') await this.#replaceSession(result, prepared, generation);
    return result;
  }

  /** Exchanges a two-factor code for an operation MFA token, keeps it and returns it. */
  async confirmOperation(operation: string, code?: string): Promise<string> {
    const mfaCode = code === undefined ? await resolveCode(this.#otp) : code.trim();
    if (!mfaCode) {
      throw new MFARequiredError(
        `Confirming ${operation} needs a two-factor code: pass one, or configure \`otp\``,
      );
    }
    const { service, tokenPath } = this.#operationMfa;
    const request: ApiRequest = {
      service,
      method: 'POST',
      path: tokenPath,
      body: { operation, mfaCode },
    };
    const generation = this.session.generation;
    const token = await this.#authorized(request, {}, isExpiredToken, (accessToken) =>
      this.#http.send(
        this.#url(request),
        this.#init(request, {}, accessToken, 'application/json'),
        {},
        readMfaToken,
      ),
    );
    // A logout meanwhile drops the token.
    if (this.session.generation === generation) this.mfaToken = token;
    return token;
  }

  /** Revokes the session on the server, ignoring failures, then forgets it. */
  async logout(options: RequestOptions = {}): Promise<void> {
    try {
      const token = await this.session.currentToken().catch(() => undefined);
      if (token && this.#logout) {
        const request: ApiRequest = { ...this.#logout, responseType: 'empty' };
        const sendOptions = { ...options, maxRetries: 0 };
        await this.#http
          .send(
            this.#url(request),
            this.#init(request, sendOptions, token, 'application/json'),
            sendOptions,
            (response) => decodeEmpty(response),
          )
          .catch(() => {});
      }
    } finally {
      await this.endSession();
    }
  }

  /** Forgets the session, its operation MFA token and the saved tokens. */
  endSession(): Promise<void> {
    return this.session.end();
  }

  /** Starts a paginated call; pages are fetched as the result is awaited or iterated. */
  paginate<T>(
    { pageIn = 'query', ...request }: PageRequest,
    options?: RequestOptions,
  ): Paginator<T> {
    const prepared = withDefaults(request);
    const located = (pageIn === 'body' ? prepared.body : prepared.query) as object | undefined;
    const params: Record<string, unknown> = { ...located };
    const start = typeof params.page === 'number' ? params.page : 0;
    return new Paginator<T>((page) => {
      const values = { ...params, page };
      const pageRequest =
        pageIn === 'body' ? { ...prepared, body: values } : { ...prepared, query: values };
      return this.call<Page<T>>(pageRequest, options);
    }, start);
  }

  /** Subscribes to a server-sent event stream; connects when iteration starts. */
  stream<T>(request: ApiRequest, options: StreamOptions = {}): AsyncIterable<ServerSentEvent<T>> {
    const { lastEventId, signal, ...rest } = options;
    const prepared = withDefaults(request);
    const connect = (lastId: string | undefined) => {
      const headers = lastId ? { ...rest.headers, 'Last-Event-ID': lastId } : rest.headers;
      const connectOptions = { ...rest, signal, headers, maxRetries: 0 };
      return this.#authorized(prepared, connectOptions, isRejectedToken, (token) =>
        this.#http.send(
          this.#url(prepared),
          this.#init(prepared, connectOptions, token, 'text/event-stream'),
          connectOptions,
          async (response) => response,
        ),
      );
    };
    const decode = (data: string): T => this.#parseLenient(data, prepared.responseShape) as T;
    return {
      [Symbol.asyncIterator]: () => subscribe<T>(connect, decode, { signal, lastEventId }),
    };
  }

  /** Sends a call, getting an operation MFA token and sending it again if it was refused. */
  async #confirmed<T>(request: ApiRequest, options: RequestOptions): Promise<T> {
    let sentMfaToken: string | undefined;
    const raw = request.responseType === 'binary' || request.responseType === 'text';
    const accept = raw ? '*/*' : 'application/json';
    // Before signing in: an invalid path parameter needs no session to fail.
    const url = this.#url(request);
    const send = () =>
      this.#authorized(request, options, isExpiredToken, (token) => {
        sentMfaToken = this.mfaToken;
        return this.#http.send(
          url,
          this.#init(request, options, token, accept),
          options,
          (response) => this.#decode(response, request) as Promise<T>,
        );
      });
    if (request.mfa === undefined || this.#otp === undefined) return send();
    try {
      return await send();
    } catch (error) {
      if (!(error instanceof MFARequiredError)) throw error;
      await this.#reconfirm(request.mfa, sentMfaToken);
      return send();
    }
  }

  /** Gets a new operation MFA token once for all the calls that the old one failed. */
  async #reconfirm(operation: string, refused: string | undefined): Promise<void> {
    // Another call may have got a new token already.
    if (this.mfaToken !== undefined && this.mfaToken !== refused) return;
    let pending = this.#confirming.get(operation);
    if (!pending) {
      const started = this.confirmOperation(operation).finally(() => {
        if (this.#confirming.get(operation) === started) this.#confirming.delete(operation);
      });
      pending = started;
      this.#confirming.set(operation, started);
    }
    await pending;
  }

  async #replaceSession(result: unknown, request: ApiRequest, generation: number): Promise<void> {
    const response = (typeof result === 'object' && result !== null ? result : {}) as {
      access_token?: unknown;
    };
    if (typeof response.access_token !== 'string' || !response.access_token) return;
    // A new password replaces the old one for later logins.
    const newPassword = (request.body as { newPassword?: unknown } | undefined)?.newPassword;
    await this.session.replace(
      response as TokenResponse,
      generation,
      typeof newPassword === 'string' ? newPassword : undefined,
    );
  }

  /** Sends with a bearer token, and once more with a renewed token if `rejected` says so. */
  async #authorized<T>(
    request: ApiRequest,
    options: RequestOptions,
    rejected: (error: unknown) => boolean,
    send: (token: string | undefined) => Promise<T>,
  ): Promise<T> {
    if (request.auth === false) return send(undefined);
    const token = await this.session.accessToken(options.signal);
    try {
      return await send(token);
    } catch (error) {
      if (!rejected(error)) throw error;
      return send(await this.session.renew(token, options.signal));
    }
  }

  #baseUrl(service: string): string {
    const url = this.#baseUrls[service];
    if (!url) throw new WhitebirdError(`No base URL for service "${service}": set it in baseUrls`);
    return url.replace(/\/+$/, '');
  }

  #url(request: ApiRequest): string {
    const query = new URLSearchParams();
    for (const [name, value] of entries(request.query)) query.append(name, formatScalar(value));
    const search = query.toString();
    const path = fillPath(request.path, request.pathParams);
    return this.#baseUrl(request.service) + path + (search ? `?${search}` : '');
  }

  #init(
    request: ApiRequest,
    options: RequestOptions,
    token: string | undefined,
    accept: string,
  ): RequestInit & { method: string } {
    const headers = new Headers({ Accept: accept, ...this.#headers });
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (this.mfaToken) headers.set('x-mfa-token', this.mfaToken);
    for (const [name, value] of entries(request.headers)) headers.set(name, formatScalar(value));
    const body = this.#encodeBody(request);
    if (typeof body === 'string') headers.set('Content-Type', 'application/json');
    for (const [name, value] of Object.entries(options.headers ?? {})) headers.set(name, value);
    const init: RequestInit & { method: string; duplex?: 'half' } = {
      method: request.method,
      headers,
      body: body ?? null,
    };
    // Node.js sends stream bodies only in half-duplex mode.
    if (body instanceof ReadableStream) init.duplex = 'half';
    return init;
  }

  #encodeBody(request: ApiRequest): BodyInit | undefined {
    const { body } = request;
    if (body === undefined) return undefined;
    if (isBodyInit(body)) return body;
    switch (request.bodyType ?? 'json') {
      case 'json':
        return this.#json.stringify(body, request.bodyShape);
      case 'form': {
        const form = new URLSearchParams();
        for (const [name, value] of entries(body as object)) form.append(name, formatScalar(value));
        return form;
      }
      case 'multipart':
        return this.#multipart(body as object, request);
    }
  }

  #multipart(body: object, request: ApiRequest): FormData {
    const form = new FormData();
    for (const [name, value] of Object.entries(body)) {
      if (value === undefined || value === null) continue;
      const files = Array.isArray(value) && value.every(isFile);
      const type = request.partTypes?.[name];
      const shape = this.#json.field(request.bodyShape, name);
      for (const item of files ? value : [value]) this.#appendPart(form, name, item, type, shape);
    }
    return form;
  }

  /**
   * Adds a multipart part: files as they are, objects as JSON (as a text field when the part is
   * declared `text/plain`), scalars as text.
   */
  #appendPart(
    form: FormData,
    name: string,
    value: unknown,
    type: string | undefined,
    shape: Shape | undefined,
  ): void {
    if (value instanceof Blob) {
      form.append(name, value);
    } else if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
      form.append(name, toBlob(value));
    } else if (typeof value === 'object' && value !== null && !(value instanceof Date)) {
      const json = this.#json.stringify(value, shape);
      if (type?.startsWith('text/plain')) form.append(name, json);
      else form.append(name, new Blob([json], { type: type ?? 'application/json' }));
    } else {
      const text = formatScalar(value);
      if (type && !type.startsWith('text/plain')) form.append(name, new Blob([text], { type }));
      else form.append(name, text);
    }
  }

  async #decode(response: Response, request: ApiRequest): Promise<unknown> {
    const type = request.responseType ?? 'json';
    switch (type) {
      case 'empty':
        return decodeEmpty(response);
      case 'text':
        return response.text();
      case 'binary':
        return new Uint8Array(await response.arrayBuffer());
    }
    const text = await response.text();
    if (text === '') return type === 'string' ? '' : undefined;
    let data: unknown;
    try {
      data = this.#json.read(text);
    } catch {
      if (type === 'json') {
        throw new APIError('The response is not valid JSON', {
          status: response.status,
          body: text,
          headers: response.headers,
        });
      }
      return text;
    }
    if (type === 'string') return typeof data === 'string' ? data : text;
    const shape = request.responseShape;
    if (request.envelope !== 'retcode') return this.#json.decode(data, shape);
    const wrapped = typeof data === 'object' && data !== null && 'result' in data;
    const envelopeShape = wrapped && shape !== undefined ? { result: shape } : shape;
    return unwrapRetCode(this.#json.decode(data, envelopeShape), response);
  }

  /** Parses JSON with exact decimals, or returns the text when it is not JSON. */
  #parseLenient(text: string, shape: Shape | undefined): unknown {
    try {
      return this.#json.parse(text, shape);
    } catch {
      return text;
    }
  }
}

/** Fills in the defaults of the parameters and body fields that are `undefined`. */
function withDefaults<R extends ApiRequest>(request: R): R {
  const { defaults } = request;
  if (!defaults) return request;
  const fill = (values: unknown, fallback: object | undefined) => {
    if (!fallback || (values !== undefined && (typeof values !== 'object' || values === null))) {
      return values;
    }
    if (isBodyInit(values)) return values;
    const filled: Record<string, unknown> = { ...(values as object) };
    for (const [name, value] of Object.entries(fallback)) {
      if (filled[name] === undefined) filled[name] = value;
    }
    return filled;
  };
  return {
    ...request,
    query: fill(request.query, defaults.query) as object | undefined,
    headers: fill(request.headers, defaults.headers) as object | undefined,
    body: fill(request.body, defaults.body),
  };
}

/** Fills in the placeholders of a path, each value encoded as one path segment. */
function fillPath(path: string, params: Record<string, string | number> | undefined): string {
  if (!params) return path;
  return path.replace(/\{([^}]+)\}/g, (placeholder, name: string) => {
    if (!Object.hasOwn(params, name)) return placeholder;
    const value = String(params[name]);
    // Empty values and dot segments would change the path.
    if (value === '' || value === '.' || value === '..') {
      throw new WhitebirdError(`Invalid value ${JSON.stringify(value)} for path parameter ${name}`);
    }
    return encodeURIComponent(value);
  });
}

/** Flattens an object into name/value pairs, repeating arrays and skipping missing values. */
function* entries(object: object | undefined): Generator<[string, unknown]> {
  for (const [name, value] of Object.entries(object ?? {})) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null) yield [name, item];
    }
  }
}

/** Bodies that fetch sends as they are. */
function isBodyInit(body: unknown): body is BodyInit {
  return (
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    ArrayBuffer.isView(body) ||
    body instanceof FormData ||
    body instanceof URLSearchParams ||
    body instanceof ReadableStream
  );
}

function isFile(value: unknown): boolean {
  return value instanceof Blob || value instanceof ArrayBuffer || ArrayBuffer.isView(value);
}

/** Wraps bytes, such as a `Buffer`, in a file part. */
function toBlob(value: ArrayBuffer | ArrayBufferView): Blob {
  const bytes =
    value instanceof ArrayBuffer
      ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  return new Blob([new Uint8Array(bytes)], { type: 'application/octet-stream' });
}

async function decodeEmpty(response: Response): Promise<undefined> {
  await response.body?.cancel();
  return undefined;
}

/** Reads the token from `{mfaToken}`, a bare string body or the `x-mfa-token` header. */
async function readMfaToken(response: Response): Promise<string> {
  const text = (await response.text()).trim();
  let data: unknown = text;
  try {
    data = JSON.parse(text);
  } catch {
    // A plain-text token.
  }
  const value = typeof data === 'string' ? data : (data as { mfaToken?: unknown } | null)?.mfaToken;
  const token =
    (typeof value === 'string' && value.trim()) || response.headers.get('x-mfa-token')?.trim();
  if (token) return token;
  throw new APIError('The response has no operation MFA token', {
    status: response.status,
    body: data,
    headers: response.headers,
  });
}

/**
 * Unwraps a `{retCode, retMsg, result}` envelope: `retCode` 0 or 200, or none at all, is a
 * success whose payload is `result` (or the whole body without one).
 */
function unwrapRetCode(data: unknown, response: Response): unknown {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return data;
  const body = data as Record<string, unknown>;
  const raw = body.retCode ?? body.code;
  const code = typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : raw;
  if (code === undefined || code === null || code === 0 || code === 200) {
    return 'result' in body ? body.result : data;
  }
  const message = [body.retMsg, body.message].find((m) => typeof m === 'string' && m !== '');
  throw new APIError((message as string | undefined) ?? `retCode ${String(raw)}`, {
    status: response.status,
    code: typeof code === 'number' && Number.isInteger(code) ? code : undefined,
    body: data,
    headers: response.headers,
  });
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
