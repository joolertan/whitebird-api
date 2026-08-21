import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { openapiDir, writeJson } from './cache.ts';
import { API_DOMAIN } from './extract/hosts.ts';
import type { Inventory } from './extract/inventory.ts';
import { log } from './log.ts';
import { HttpError, getText, pause } from './net.ts';

const DOCS_PATH = '/v3/api-docs';

export interface PublishedSpec {
  service: string;
  url: string;
}

/**
 * Probes each production service host for a public springdoc `/v3/api-docs` and stores the
 * ones that answer with an OpenAPI document as `<cache>/openapi/<service>.json`.
 */
export async function downloadApiDocs(cacheDir: string, inventory: Inventory, only: string[] = []): Promise<PublishedSpec[]> {
  const dir = openapiDir(cacheDir);
  await mkdir(dir, { recursive: true });
  const published: PublishedSpec[] = [];
  const services = Object.entries(inventory.services).filter(
    ([name, hosts]) => (only.length ? only.includes(name) : true) && new URL(hosts.production).hostname.endsWith(API_DOMAIN),
  );
  for (const [service, hosts] of services) {
    const url = `${hosts.production}${DOCS_PATH}`;
    await pause();
    try {
      const doc = JSON.parse(await getText(url, { accept: 'application/json', retries: 1 })) as { openapi?: string };
      if (typeof doc.openapi !== 'string') throw new Error('not an OpenAPI document');
      await writeJson(join(dir, `${service}.json`), doc);
      published.push({ service, url });
      log(`${service}: saved OpenAPI ${doc.openapi}`);
    } catch (error) {
      const reason = error instanceof HttpError ? `HTTP ${error.status}` : (error as Error).message;
      log(`${service}: no public api-docs (${reason})`);
    }
  }
  return published;
}
