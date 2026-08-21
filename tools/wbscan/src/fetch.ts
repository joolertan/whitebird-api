import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { buildId, chunkRefs, fileName, scriptsFromHtml } from './assets.ts';
import { buildPaths, readJson, setLatest, writeJson, type BuildMeta } from './cache.ts';
import { log } from './log.ts';
import { HttpError, getText, pause } from './net.ts';

export interface FetchOptions {
  cacheDir: string;
  site: string;
  force?: boolean;
}

/** Downloads index.html and every JS chunk reachable from it into `<cache>/<build>/raw/`. */
export async function fetchBuild({ cacheDir, site, force = false }: FetchOptions): Promise<BuildMeta> {
  const pageUrl = new URL('/', site).href;
  const html = await getText(pageUrl, { accept: 'text/html' });
  const { entry, preloads } = scriptsFromHtml(html, pageUrl);
  const build = buildId(entry);
  const paths = buildPaths(cacheDir, build);

  if (!force && existsSync(paths.meta)) {
    log(`build ${build} already cached at ${paths.root}`);
    await setLatest(cacheDir, build);
    return readJson<BuildMeta>(paths.meta);
  }

  await mkdir(paths.raw, { recursive: true });
  await writeFile(join(paths.raw, 'index.html'), html);
  const queue = [entry, ...preloads];
  const seen = new Set(queue);
  const chunks: string[] = [];
  while (queue.length) {
    const url = queue.shift()!;
    await pause();
    let code: string;
    try {
      code = await getText(url, { accept: '*/*' });
    } catch (error) {
      // Discovery is regex-based, so a stray "./x.js" string in vendor code may not exist.
      if (error instanceof HttpError && error.status === 404 && url !== entry) {
        log(`skip ${fileName(url)}: 404`);
        continue;
      }
      throw error;
    }
    await writeFile(join(paths.raw, fileName(url)), code);
    chunks.push(fileName(url));
    log(`fetched ${fileName(url)} (${(code.length / 1024).toFixed(0)} KiB)`);
    for (const ref of chunkRefs(code, url)) {
      if (!seen.has(ref)) {
        seen.add(ref);
        queue.push(ref);
      }
    }
  }

  const meta: BuildMeta = {
    site: pageUrl,
    build,
    entry: fileName(entry),
    fetchedAt: new Date().toISOString(),
    chunks: chunks.sort(),
  };
  await writeJson(paths.meta, meta);
  await setLatest(cacheDir, build);
  log(`build ${build}: ${chunks.length} chunks in ${paths.raw}`);
  return meta;
}
