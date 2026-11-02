import {
  APIError,
  errorFromResponse,
  parseRetryAfter,
  TransportError,
  WhitebirdError,
} from './errors.js';

/** Options accepted by every API method. */
export interface RequestOptions {
  /**
   * Cancels the request, including a sign-in it waits for; the returned promise rejects with the
   * signal's reason.
   */
  signal?: AbortSignal | undefined;
  /** Timeout in milliseconds for this request, overriding the client's `timeout`. */
  timeout?: number | undefined;
  /** Extra headers sent with this request. */
  headers?: Record<string, string> | undefined;
  /** Retries for this request, overriding the client's `maxRetries`. */
  maxRetries?: number | undefined;
  /**
   * Whether the request may be sent again after any transient failure. Defaults to `true` for
   * GET, HEAD and OPTIONS. Other requests are retried only when they certainly did not reach the
   * server: the connection could not be established, or the answer was 429. Set it to `true` to
   * retry a request on every transient failure, or to `false` to never retry it.
   */
  idempotent?: boolean | undefined;
}

export interface HttpConfig {
  fetch: typeof fetch;
  timeout: number;
  maxRetries: number;
}

const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const RETRY_STATUSES = new Set([429, 502, 503, 504]);
const MAX_RETRY_AFTER = 60_000;
// Errors of connections that were never established, so the request was not sent.
const CONNECT_ERRORS = new Set([
  'ECONNREFUSED',
  'ENOTFOUND',
  'EAI_AGAIN',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ENETDOWN',
  'UND_ERR_CONNECT_TIMEOUT',
]);
/** Transport errors of requests that never reached the server. */
const UNSENT = new WeakSet<TransportError>();

/** Sends requests with timeouts and retries, turning error responses into {@link APIError}s. */
export class HttpClient {
  readonly #config: HttpConfig;

  constructor(config: HttpConfig) {
    this.#config = config;
  }

  /**
   * Sends a request and hands a successful response to `read`, which runs under the same
   * timeout. Idempotent requests are retried on transport errors, 429, 502, 503 and 504; other
   * requests only when the connection failed before sending, and on 429.
   */
  async send<T>(
    url: string,
    init: RequestInit & { method: string },
    options: RequestOptions,
    read: (response: Response) => Promise<T>,
  ): Promise<T> {
    const idempotent = options.idempotent ?? IDEMPOTENT_METHODS.has(init.method);
    // A stream body cannot be sent twice.
    const replayable = !(init.body instanceof ReadableStream);
    const maxRetries = replayable ? (options.maxRetries ?? this.#config.maxRetries) : 0;
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.#attempt(url, init, options, read);
      } catch (error) {
        const delay = attempt < maxRetries ? retryDelay(error, attempt, idempotent) : undefined;
        if (delay === undefined) throw error;
        await sleep(delay, options.signal);
      }
    }
  }

  async #attempt<T>(
    url: string,
    init: RequestInit,
    options: RequestOptions,
    read: (response: Response) => Promise<T>,
  ): Promise<T> {
    const deadline = new Deadline(options.signal, options.timeout ?? this.#config.timeout);
    const fetch = this.#config.fetch;
    try {
      const response = await fetch(url, { ...init, signal: deadline.signal });
      if (!response.ok) {
        const body = parseBody(await response.text());
        throw errorFromResponse(response.status, body, response.headers);
      }
      return await read(response);
    } catch (error) {
      throw deadline.explain(error);
    } finally {
      deadline.clear();
    }
  }
}

/** Parses a JSON response body; an empty body reads as `undefined`. Numbers are not exact. */
export async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === '') return undefined;
  try {
    return JSON.parse(text);
  } catch {
    throw new APIError('The response is not valid JSON', {
      status: response.status,
      body: text,
      headers: response.headers,
    });
  }
}

/** Resolves after `ms` milliseconds, or rejects with the signal's reason when it aborts. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/** Exponential backoff with jitter: about 0.5 s, 1 s, 2 s, ... capped at 8 s. */
export function backoff(attempt: number): number {
  return Math.min(500 * 2 ** Math.min(attempt, 10), 8000) * (1 - Math.random() * 0.25);
}

function retryDelay(error: unknown, attempt: number, idempotent: boolean): number | undefined {
  if (error instanceof TransportError) {
    return idempotent || UNSENT.has(error) ? backoff(attempt) : undefined;
  }
  if (!(error instanceof APIError)) return undefined;
  const status = error.status ?? 0;
  if (!(status === 429 || (idempotent && RETRY_STATUSES.has(status)))) return undefined;
  const retryAfter = error.headers && parseRetryAfter(error.headers);
  if (retryAfter === undefined) return backoff(attempt);
  // Waiting minutes inside a call is worse than letting the caller decide.
  return retryAfter * 1000 <= MAX_RETRY_AFTER ? retryAfter * 1000 : undefined;
}

/** Whether a fetch failure happened while connecting, before anything was sent. */
function connectFailed(error: unknown, depth = 0): boolean {
  if (!(error instanceof Error) || depth > 5) return false;
  const code = (error as { code?: unknown }).code;
  if (typeof code === 'string' && CONNECT_ERRORS.has(code)) return true;
  const nested = error instanceof AggregateError ? error.errors : [];
  return [error.cause, ...nested].some((inner) => connectFailed(inner, depth + 1));
}

function parseBody(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text || undefined;
  }
}

/** Aborts a request after a timeout, and tells a timeout apart from the caller aborting. */
class Deadline {
  readonly signal: AbortSignal;
  readonly #parent: AbortSignal | undefined;
  readonly #ms: number;
  readonly #timer: ReturnType<typeof setTimeout> | undefined;
  #expired = false;

  constructor(parent: AbortSignal | undefined, ms: number) {
    const controller = new AbortController();
    this.#parent = parent;
    this.#ms = ms;
    // setTimeout fires immediately for delays above 2^31 - 1 ms, so those mean "no timeout".
    if (ms > 0 && ms < 2 ** 31) {
      this.#timer = setTimeout(() => {
        this.#expired = true;
        controller.abort();
      }, ms);
    }
    this.signal = parent ? AbortSignal.any([parent, controller.signal]) : controller.signal;
  }

  clear(): void {
    clearTimeout(this.#timer);
  }

  explain(error: unknown): unknown {
    if (error instanceof WhitebirdError) return error;
    if (this.#parent?.aborted) return this.#parent.reason;
    if (this.#expired) {
      return new TransportError(`The request timed out after ${this.#ms} ms`, { cause: error });
    }
    const failure = new TransportError(`The request failed: ${describe(error)}`, { cause: error });
    if (connectFailed(error)) UNSENT.add(failure);
    return failure;
  }
}

function describe(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  // undici reports every network failure as "fetch failed" and puts the reason in `cause`.
  return error.cause instanceof Error ? `${error.message} (${error.cause.message})` : error.message;
}
