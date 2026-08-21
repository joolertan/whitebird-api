import { existsSync } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { webcrack } from 'webcrack';
import type { BuildMeta, BuildPaths } from './cache.ts';
import { inWorker } from './isolate.ts';
import { log } from './log.ts';

/** webcrack evaluates obfuscator string decoders in a sandbox; bundle code is never executed here. */
async function refuseToEvaluate(): Promise<never> {
  throw new Error('wbscan does not evaluate bundle code');
}

/** Worker entry: unminifies one chunk. Rolldown ESM chunks are not webpack bundles, so no unpacking. */
export async function deobfuscateFile(input: string, output: string): Promise<number> {
  const code = await readFile(input, 'utf8');
  const result = await webcrack(code, {
    unpack: false,
    deobfuscate: true,
    unminify: true,
    jsx: true,
    mangle: false,
    sandbox: refuseToEvaluate,
  });
  await writeFile(`${output}.tmp`, result.code);
  await rename(`${output}.tmp`, output);
  return result.code.length;
}

export async function deobfuscateBuild(paths: BuildPaths, meta: BuildMeta, force = false): Promise<void> {
  await mkdir(paths.deobfuscated, { recursive: true });
  for (const chunk of meta.chunks) {
    const output = join(paths.deobfuscated, chunk);
    if (!force && existsSync(output)) continue;
    const started = Date.now();
    await inWorker(new URL(import.meta.url), 'deobfuscateFile', join(paths.raw, chunk), output);
    log(`deobfuscated ${chunk} in ${((Date.now() - started) / 1000).toFixed(1)}s`);
  }
}
