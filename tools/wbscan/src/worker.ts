import { parentPort, workerData } from 'node:worker_threads';

const { module, fn, args } = workerData as { module: string; fn: string; args: unknown[] };
const mod = (await import(module)) as Record<string, (...a: unknown[]) => unknown>;
parentPort!.postMessage(await mod[fn]!(...args));
