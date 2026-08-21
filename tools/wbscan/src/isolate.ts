import { Worker } from 'node:worker_threads';

/** webcrack on the largest chunk and parsing all chunks at once both need ~3 GB of heap. */
const HEAP_LIMIT_MB = 6144;

/**
 * Calls `module[fn](...args)` in a worker thread with a raised heap limit, so memory-heavy
 * steps neither depend on Node's default limit nor accumulate garbage across chunks.
 */
export function inWorker<T>(module: URL, fn: string, ...args: unknown[]): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const worker = new Worker(new URL('./worker.ts', import.meta.url), {
      workerData: { module: module.href, fn, args },
      resourceLimits: { maxOldGenerationSizeMb: HEAP_LIMIT_MB },
    });
    let settled = false;
    worker.once('message', (value: T) => {
      settled = true;
      resolve(value);
    });
    worker.once('error', (error) => {
      settled = true;
      reject(error);
    });
    worker.once('exit', (code) => {
      if (!settled) reject(new Error(`${fn} worker exited with code ${code}`));
    });
  });
}
