import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { writeJson, type BuildMeta, type BuildPaths } from '../cache.ts';
import { extractInventory } from './index.ts';

export interface ExtractSummary {
  endpoints: number;
  unresolved: number;
  services: Record<string, number>;
}

/** Worker entry: reads the deobfuscated chunks of a build and writes `inventory.json`. */
export async function extractBuild(paths: BuildPaths, meta: BuildMeta, extraOut?: string): Promise<ExtractSummary> {
  const sources = await Promise.all(
    meta.chunks.map(async (name) => ({ name, code: await readFile(join(paths.deobfuscated, name), 'utf8') })),
  );
  const inventory = extractInventory(sources, { entry: meta.entry, fetchedAt: meta.fetchedAt, chunks: meta.chunks });
  await writeJson(paths.inventory, inventory);
  if (extraOut) await writeJson(extraOut, inventory);

  const services: Record<string, number> = {};
  for (const e of inventory.endpoints) services[e.service] = (services[e.service] ?? 0) + 1;
  return { endpoints: inventory.endpoints.length, unresolved: inventory.unresolved.length, services };
}
