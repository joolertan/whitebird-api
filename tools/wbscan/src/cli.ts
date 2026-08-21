import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { DEFAULT_CACHE_DIR, REPO_ROOT, openapiDir, readJson, resolveBuild } from './cache.ts';
import { deobfuscateBuild } from './deobfuscate.ts';
import { diffInventory, loadPublished, loadSpec, renderMarkdown } from './diff.ts';
import type { ExtractSummary } from './extract/build.ts';
import type { Inventory } from './extract/inventory.ts';
import { fetchBuild } from './fetch.ts';
import { inWorker } from './isolate.ts';
import { log } from './log.ts';
import { downloadApiDocs } from './openapi.ts';

const USAGE = `Usage: node src/cli.ts <command> [options]

Commands:
  fetch         download index.html and all JS chunks of the live site
  deobfuscate   run webcrack on every chunk of a build
  extract       build inventory.json from the deobfuscated chunks
  scan          fetch + deobfuscate + extract
  openapi       download public /v3/api-docs of the inventory's services
  diff          compare the inventory with spec/services/*.yaml

Options:
  --cache-dir <dir>    cache root (default: <repo>/.cache/wbscan)
  --build <id>         build id (entry chunk hash); default: the latest fetched
  --site <url>         site to scan (default: https://whitebird.io)
  --force              re-download / re-run steps that are cached
  --out <file>         extract/scan: also write inventory.json here
  --service <name>     openapi: only this service (repeatable)
  --spec <dir>         diff: spec dir (default: <repo>/spec)
  --inventory <file>   diff: inventory to compare (default: the build's)
  --openapi-dir <dir>  diff: published specs (default: <cache>/openapi)
  --json               diff: print the report as JSON instead of Markdown
  --check              diff: exit with code 1 when drift is found
`;

const { values: opts, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    'cache-dir': { type: 'string', default: DEFAULT_CACHE_DIR },
    build: { type: 'string' },
    site: { type: 'string', default: 'https://whitebird.io' },
    force: { type: 'boolean', default: false },
    out: { type: 'string' },
    service: { type: 'string', multiple: true, default: [] },
    spec: { type: 'string', default: resolve(REPO_ROOT, 'spec') },
    inventory: { type: 'string' },
    'openapi-dir': { type: 'string' },
    json: { type: 'boolean', default: false },
    check: { type: 'boolean', default: false },
    help: { type: 'boolean', short: 'h', default: false },
  },
});

const cacheDir = resolve(opts['cache-dir']);

async function fetchCommand(): Promise<string> {
  const meta = await fetchBuild({ cacheDir, site: opts.site, force: opts.force });
  return meta.build;
}

async function deobfuscateCommand(build?: string): Promise<void> {
  const { paths, meta } = await resolveBuild(cacheDir, build);
  await deobfuscateBuild(paths, meta, opts.force);
}

async function extractCommand(build?: string): Promise<void> {
  const { paths, meta } = await resolveBuild(cacheDir, build);
  const out = opts.out ? resolve(opts.out) : undefined;
  const summary = await inWorker<ExtractSummary>(new URL('./extract/build.ts', import.meta.url), 'extractBuild', paths, meta, out);
  for (const [service, count] of Object.entries(summary.services)) log(`  ${service.padEnd(32)} ${count}`);
  log(`${summary.endpoints} endpoints, ${summary.unresolved} unresolved -> ${paths.inventory}`);
}

async function loadInventory(): Promise<Inventory> {
  if (opts.inventory) return readJson<Inventory>(resolve(opts.inventory));
  const { paths } = await resolveBuild(cacheDir, opts.build);
  if (!existsSync(paths.inventory)) throw new Error(`no inventory at ${paths.inventory}; run "extract" first`);
  return readJson<Inventory>(paths.inventory);
}

async function openapiCommand(): Promise<void> {
  const published = await downloadApiDocs(cacheDir, await loadInventory(), opts.service);
  log(`${published.length} published spec(s) in ${openapiDir(cacheDir)}`);
}

async function diffCommand(): Promise<number> {
  const inventory = await loadInventory();
  const spec = await loadSpec(resolve(opts.spec));
  const published = await loadPublished(resolve(opts['openapi-dir'] ?? openapiDir(cacheDir)));
  const report = diffInventory(inventory, spec, published);
  process.stdout.write(opts.json ? `${JSON.stringify(report, null, 2)}\n` : renderMarkdown(report));
  return opts.check && report.drift ? 1 : 0;
}

async function main(): Promise<number> {
  const [command] = positionals;
  if (opts.help || !command) {
    process.stdout.write(USAGE);
    return command || opts.help ? 0 : 2;
  }
  switch (command) {
    case 'fetch':
      await fetchCommand();
      return 0;
    case 'deobfuscate':
      await deobfuscateCommand(opts.build);
      return 0;
    case 'extract':
      await extractCommand(opts.build);
      return 0;
    case 'scan': {
      const build = await fetchCommand();
      await deobfuscateCommand(build);
      await extractCommand(build);
      return 0;
    }
    case 'openapi':
      await openapiCommand();
      return 0;
    case 'diff':
      return diffCommand();
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

try {
  process.exitCode = await main();
} catch (error) {
  log(`error: ${(error as Error).message}`);
  process.exitCode = 2;
}
