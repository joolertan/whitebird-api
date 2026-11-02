import { RateLimitError, ServerError, TransportError } from './errors.js';
import { type RequestOptions, sleep } from './http.js';

/** An event received from a server-sent event stream. */
export interface ServerSentEvent<T = unknown> {
  /** Event type, `message` when the server sent none. */
  event: string;
  /** Payload: parsed JSON with decimals as strings, or the raw text when it is not JSON. */
  data: T;
  /** ID of the last event received, sent back as `Last-Event-ID` when reconnecting. */
  id?: string | undefined;
}

/** Options of streaming methods. */
export interface StreamOptions extends RequestOptions {
  /** Resumes the stream after this event ID. */
  lastEventId?: string | undefined;
}

interface RawEvent {
  event: string;
  data: string;
  id: string | undefined;
}

const DEFAULT_RETRY = 1000;
const MAX_RETRY = 30_000;

/**
 * Reads events from connections opened by `connect`, reconnecting with `Last-Event-ID` and an
 * exponential backoff whenever a connection drops or fails with a transient error. Ends when
 * `signal` aborts or the server answers 204 No Content; other errors (such as a rejected token)
 * end it by throwing.
 */
export async function* subscribe<T>(
  connect: (lastEventId: string | undefined) => Promise<Response>,
  decode: (data: string) => T,
  { signal, lastEventId }: { signal?: AbortSignal | undefined; lastEventId?: string | undefined },
): AsyncGenerator<ServerSentEvent<T>, void, undefined> {
  const parser = new EventParser(lastEventId);
  let failures = 0;
  while (!signal?.aborted) {
    let body: ReadableStream<Uint8Array> | null = null;
    try {
      const response = await connect(parser.lastEventId);
      // 204 is how a server tells event stream clients to stop reconnecting.
      if (response.status === 204) {
        await response.body?.cancel();
        return;
      }
      const type = response.headers.get('content-type') ?? '';
      if (!/^text\/event-stream\b/i.test(type)) {
        await response.body?.cancel();
        throw new TransportError(`The stream answered with "${type}", not text/event-stream`);
      }
      body = response.body;
      failures = 0;
    } catch (error) {
      if (signal?.aborted) return;
      if (!isTransient(error)) throw error;
      failures += 1;
    }
    if (body) {
      try {
        for await (const line of lines(body)) {
          const event = parser.push(line);
          if (event) yield { event: event.event, data: decode(event.data), id: event.id };
        }
      } catch {
        // The connection dropped: reconnect below.
        if (signal?.aborted) return;
      }
    }
    parser.reset();
    const delay = Math.min(
      (parser.retry ?? DEFAULT_RETRY) * 2 ** Math.min(failures, 16),
      MAX_RETRY,
    );
    try {
      await sleep(delay, signal);
    } catch {
      return;
    }
  }
}

/** Parses the text/event-stream format line by line, as specified by WHATWG HTML. */
export class EventParser {
  /** ID of the last event dispatched, sent back as `Last-Event-ID` when reconnecting. */
  lastEventId: string | undefined;
  /** Reconnection delay in milliseconds requested by the server. */
  retry: number | undefined;
  #event = '';
  #data: string[] = [];
  /** ID set by the event being received; it counts once the event is complete. */
  #id: string | undefined;

  constructor(lastEventId?: string) {
    this.lastEventId = lastEventId;
    this.#id = lastEventId;
  }

  /** Consumes one line; returns the event completed by a blank line. */
  push(line: string): RawEvent | undefined {
    if (line === '') return this.#dispatch();
    if (line.startsWith(':')) return undefined;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'event':
        this.#event = value;
        break;
      case 'data':
        this.#data.push(value);
        break;
      case 'id':
        if (!value.includes('\0')) this.#id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
    }
    return undefined;
  }

  /** Drops a partially received event. */
  reset(): void {
    this.#event = '';
    this.#data = [];
    this.#id = this.lastEventId;
  }

  #dispatch(): RawEvent | undefined {
    const event = this.#event || 'message';
    const data = this.#data;
    this.lastEventId = this.#id;
    this.#event = '';
    this.#data = [];
    if (data.length === 0) return undefined;
    return { event, data: data.join('\n'), id: this.lastEventId };
  }
}

/** Splits a byte stream into lines ended by CRLF, LF or CR. Cancels the stream when left early. */
export async function* lines(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let pendingCR = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      const text = decoder.decode(value, { stream: true });
      // A CRLF split across chunks must not count as two line breaks.
      const chunk: string = pendingCR && text.startsWith('\n') ? text.slice(1) : text;
      pendingCR = chunk.endsWith('\r');
      const parts = (buffer + chunk).split(/\r\n|\r|\n/);
      buffer = parts.pop() ?? '';
      yield* parts;
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}

function isTransient(error: unknown): boolean {
  return (
    error instanceof TransportError ||
    error instanceof ServerError ||
    error instanceof RateLimitError
  );
}
