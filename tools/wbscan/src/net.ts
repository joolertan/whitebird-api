import { setTimeout as sleep } from 'node:timers/promises';

export const USER_AGENT =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36';

const TIMEOUT_MS = 60_000;
const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

export class HttpError extends Error {
  readonly status: number;

  constructor(url: string, status: number) {
    super(`GET ${url}: HTTP ${status}`);
    this.status = status;
  }
}

export interface GetOptions {
  accept?: string;
  retries?: number;
}

/** GET with a browser-like User-Agent, retrying network errors and transient statuses with backoff. */
export async function getText(url: string, { accept = '*/*', retries = 3 }: GetOptions = {}): Promise<string> {
  for (let attempt = 0; ; attempt++) {
    let error: unknown;
    let retryable = true;
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': USER_AGENT, Accept: accept, 'Accept-Language': 'ru,en;q=0.8' },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (res.ok) return await res.text();
      error = new HttpError(url, res.status);
      retryable = RETRYABLE.has(res.status);
    } catch (e) {
      error = e;
    }
    if (!retryable || attempt >= retries) throw error;
    await sleep(1000 * 2 ** attempt);
  }
}

/** Pause between sequential requests to stay polite. */
export function pause(ms = 300): Promise<void> {
  return sleep(ms);
}
