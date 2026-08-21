import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
export const DEFAULT_CACHE_DIR = join(REPO_ROOT, '.cache', 'wbscan');

export interface BuildMeta {
  site: string;
  build: string;
  entry: string;
  fetchedAt: string;
  chunks: string[];
}

export interface BuildPaths {
  root: string;
  raw: string;
  deobfuscated: string;
  meta: string;
  inventory: string;
}

export function buildPaths(cacheDir: string, build: string): BuildPaths {
  const root = join(cacheDir, build);
  return {
    root,
    raw: join(root, 'raw'),
    deobfuscated: join(root, 'deobfuscated'),
    meta: join(root, 'meta.json'),
    inventory: join(root, 'inventory.json'),
  };
}

export function openapiDir(cacheDir: string): string {
  return join(cacheDir, 'openapi');
}

const latestFile = (cacheDir: string) => join(cacheDir, 'latest');

export async function setLatest(cacheDir: string, build: string): Promise<void> {
  await mkdir(cacheDir, { recursive: true });
  await writeFile(latestFile(cacheDir), `${build}\n`);
}

export async function writeJson(path: string, value: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

export async function readJson<T>(path: string): Promise<T> {
  return JSON.parse(await readFile(path, 'utf8')) as T;
}

/** The requested build, or the most recently fetched one. */
export async function resolveBuild(cacheDir: string, build?: string): Promise<{ paths: BuildPaths; meta: BuildMeta }> {
  const id = build ?? (existsSync(latestFile(cacheDir)) ? (await readFile(latestFile(cacheDir), 'utf8')).trim() : undefined);
  if (!id) throw new Error(`no build in ${cacheDir}; run "fetch" first`);
  const paths = buildPaths(cacheDir, id);
  if (!existsSync(paths.meta)) throw new Error(`build ${id} is not fetched (missing ${paths.meta})`);
  return { paths, meta: await readJson<BuildMeta>(paths.meta) };
}
