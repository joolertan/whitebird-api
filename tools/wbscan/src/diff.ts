import { existsSync } from 'node:fs';
import { readFile, readdir } from 'node:fs/promises';
import { basename, extname, join } from 'node:path';
import YAML from 'yaml';
import { hostOf } from './extract/hosts.ts';
import type { Endpoint, Inventory } from './extract/inventory.ts';
import type { BodyKind } from './extract/requests.ts';
import { pathKey } from './extract/url.ts';

const METHODS = ['get', 'put', 'post', 'delete', 'patch', 'head', 'options'];
const SPEC_EXTENSIONS = new Set(['.yaml', '.yml', '.json']);

export interface SpecBody {
  kind: BodyKind;
  fields: string[];
}

export interface SpecOperation {
  method: string;
  path: string;
  query: string[];
  body: SpecBody | null;
  /** `x-wb-source`: where the operation came from (`openapi` = imported from a published spec). */
  source: string | null;
}

export interface SpecFile {
  file: string;
  /** `x-wb-service`, else derived from the file name. */
  service: string;
  /** Hosts of `servers`; the first one identifies the service. */
  hosts: string[];
  operations: SpecOperation[];
}

export interface OAuthClient {
  tokenUrl?: string;
  basic?: string;
}

/** `spec/environments.yaml`: service hosts per environment and the public OAuth client. */
export interface Environments {
  hosts: string[];
  oauth: OAuthClient | null;
}

/** `spec/excluded.yaml`: what the web app calls but the SDKs deliberately leave out. */
export interface Excluded {
  hosts: Set<string>;
  /** `METHOD host/path/{}` keys. */
  endpoints: Set<string>;
}

export interface Spec {
  services: SpecFile[];
  environments: Environments | null;
  excluded: Excluded;
}

export interface EndpointRef {
  service: string;
  method: string;
  path: string;
}

export interface ShapeChange extends EndpointRef {
  /** Query parameters the frontend sends that the spec lacks. */
  query?: string[];
  /** Top-level body fields the frontend sends that the spec lacks. */
  body?: string[];
  bodyKind?: { spec: string; inventory: string };
}

export interface DiffReport {
  build: Inventory['build'];
  specFiles: number;
  drift: boolean;
  newHosts: string[];
  removedHosts: string[];
  oauth: { field: string; spec: string | null; inventory: string | null }[];
  newEndpoints: EndpointRef[];
  goneEndpoints: (EndpointRef & { file: string })[];
  /** Spec operations without a resolved call that are still accounted for; not drift. */
  unconfirmedEndpoints: (EndpointRef & { file: string; reason: 'mentioned' | 'live' })[];
  changedEndpoints: ShapeChange[];
}

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);

function deref(doc: Json, value: unknown, depth = 0): unknown {
  if (!isObject(value) || typeof value.$ref !== 'string' || depth > 20) return value;
  const target = value.$ref
    .replace(/^#\//, '')
    .split('/')
    .reduce<unknown>((node, key) => (isObject(node) ? node[key.replace(/~1/g, '/').replace(/~0/g, '~')] : undefined), doc);
  return deref(doc, target, depth + 1);
}

function schemaFields(doc: Json, schema: unknown, depth = 0): string[] {
  const s = deref(doc, schema);
  if (!isObject(s) || depth > 10) return [];
  const own = isObject(s.properties) ? Object.keys(s.properties) : [];
  const composed = Array.isArray(s.allOf) ? s.allOf.flatMap((part) => schemaFields(doc, part, depth + 1)) : [];
  return [...own, ...composed];
}

function bodyKindOf(contentType: string): BodyKind {
  if (contentType.includes('multipart')) return 'multipart';
  if (contentType.includes('urlencoded')) return 'form-urlencoded';
  return 'json';
}

function specBody(doc: Json, requestBody: unknown): SpecBody | null {
  const body = deref(doc, requestBody);
  if (!isObject(body) || !isObject(body.content)) return null;
  const [contentType, media] = Object.entries(body.content)[0] ?? [];
  if (!contentType) return null;
  const fields = isObject(media) ? schemaFields(doc, media.schema) : [];
  return { kind: bodyKindOf(contentType), fields: [...new Set(fields)].sort() };
}

export function parseSpec(file: string, text: string): SpecFile {
  const doc = YAML.parse(text) as Json;
  const servers = Array.isArray(doc.servers) ? doc.servers.filter(isObject) : [];
  const hosts = servers.map((s) => (typeof s.url === 'string' ? hostOf(s.url) : undefined)).filter((h): h is string => !!h);
  const service = typeof doc['x-wb-service'] === 'string' ? doc['x-wb-service'] : basename(file, extname(file));

  const operations: SpecOperation[] = [];
  for (const [path, rawItem] of Object.entries(isObject(doc.paths) ? doc.paths : {})) {
    const item = deref(doc, rawItem);
    if (!isObject(item)) continue;
    const shared = Array.isArray(item.parameters) ? item.parameters : [];
    for (const method of METHODS) {
      const op = item[method];
      if (!isObject(op)) continue;
      const params = [...shared, ...(Array.isArray(op.parameters) ? op.parameters : [])].map((p) => deref(doc, p));
      const query = params.filter(isObject).filter((p) => p.in === 'query' && typeof p.name === 'string').map((p) => p.name as string);
      operations.push({
        method: method.toUpperCase(),
        path,
        query: [...new Set(query)].sort(),
        body: specBody(doc, op.requestBody),
        source: typeof op['x-wb-source'] === 'string' ? op['x-wb-source'] : null,
      });
    }
  }
  return { file, service, hosts, operations };
}

export function parseEnvironments(text: string): Environments {
  const doc = YAML.parse(text) as Json;
  const services = isObject(doc.services) ? doc.services : {};
  const hosts = new Set<string>();
  for (const entry of Object.values(services)) {
    if (!isObject(entry)) continue;
    for (const [key, value] of Object.entries(entry)) {
      const host = key !== 'title' && typeof value === 'string' ? hostOf(value) : undefined;
      if (host) hosts.add(host);
    }
  }
  let oauth: OAuthClient | null = null;
  if (isObject(doc.oauth)) {
    oauth = {};
    const { basic, service, token_path: tokenPath } = doc.oauth;
    if (typeof basic === 'string') oauth.basic = basic;
    const origin = typeof service === 'string' && isObject(services[service]) ? services[service].production : undefined;
    if (typeof origin === 'string' && typeof tokenPath === 'string') oauth.tokenUrl = `${origin.replace(/\/+$/, '')}${tokenPath}`;
  }
  return { hosts: [...hosts].sort(), oauth };
}

async function specFiles(dir: string): Promise<SpecFile[]> {
  if (!existsSync(dir)) return [];
  const files = (await readdir(dir)).filter((f) => SPEC_EXTENSIONS.has(extname(f))).sort();
  return Promise.all(files.map(async (f) => parseSpec(join(dir, f), await readFile(join(dir, f), 'utf8'))));
}

/** Reads `<dir>/environments.yaml` and `<dir>/services/*.yaml`, or spec files directly in `<dir>`. */
export async function loadSpec(dir: string): Promise<Spec> {
  const envFile = join(dir, 'environments.yaml');
  const excludedFile = join(dir, 'excluded.yaml');
  const servicesDir = existsSync(join(dir, 'services')) ? join(dir, 'services') : dir;
  return {
    services: await specFiles(servicesDir),
    environments: existsSync(envFile) ? parseEnvironments(await readFile(envFile, 'utf8')) : null,
    excluded: existsSync(excludedFile) ? parseExcluded(await readFile(excludedFile, 'utf8')) : { hosts: new Set(), endpoints: new Set() },
  };
}

/** `hosts` and `endpoints` (`METHOD https://host/path`) are maps from the entry to the reason. */
function parseExcluded(text: string): Excluded {
  const doc: unknown = YAML.parse(text);
  const keys = (v: unknown) => (isObject(v) ? Object.keys(v) : []);
  const endpoints = keys(isObject(doc) ? doc.endpoints : undefined).map((entry) => {
    const [method, url] = entry.split(/\s+/);
    const parsed = url ? URL.parse(url) : null;
    if (!method || !parsed) throw new Error(`excluded.yaml: expected "METHOD https://host/path", got "${entry}"`);
    return endpointKey(method.toUpperCase(), parsed.host, decodeURI(parsed.pathname));
  });
  return { hosts: new Set(keys(isObject(doc) ? doc.hosts : undefined)), endpoints: new Set(endpoints) };
}

function endpointKey(method: string, host: string, path: string): string {
  return `${method} ${host}${pathKey(path)}`;
}

/** Operation keys (`METHOD /path/{}`) of the specs services publish, by inventory service (`<service>.json`). */
export async function loadPublished(dir: string): Promise<Map<string, Set<string>>> {
  const specs = await specFiles(dir);
  return new Map(
    specs.map((spec) => [basename(spec.file, extname(spec.file)), new Set(spec.operations.map((op) => `${op.method} ${pathKey(op.path)}`))]),
  );
}

/** Same path, where a parameter on either side matches any single segment (`/add/{}` ~ `/add/organization`). */
function pathsMatch(a: string, b: string): boolean {
  if (a === b) return true;
  const x = a.split('/');
  const y = b.split('/');
  return x.length === y.length && x.every((seg, i) => seg === y[i] || seg === '{}' || y[i] === '{}');
}

function missingFrom(spec: string[], inventory: string[]): string[] | undefined {
  const missing = inventory.filter((k) => !spec.includes(k));
  return missing.length ? missing : undefined;
}

// The extractor cannot see every field the app sends, so only inventory fields missing from the
// spec count as drift; spec-only fields are expected blind spots.
function shapeChange(op: SpecOperation, e: Endpoint): ShapeChange | undefined {
  const change: ShapeChange = { service: e.service, method: e.method, path: e.path };
  const query = missingFrom(op.query, e.query);
  if (query) change.query = query;
  const specKind = op.body?.kind ?? 'none';
  if (e.body.fields.length && specKind !== e.body.kind) {
    change.bodyKind = { spec: specKind, inventory: e.body.kind };
  }
  const body = op.body ? missingFrom(op.body.fields, e.body.fields) : undefined;
  if (body) change.body = body;
  return change.query || change.body || change.bodyKind ? change : undefined;
}

const ref = (e: EndpointRef): EndpointRef => ({ service: e.service, method: e.method, path: e.path });

function hostsOf(inventory: Inventory, service: string): string[] {
  const urls = inventory.services[service];
  return [urls?.production, urls?.demo].flatMap((url) => (url ? [hostOf(url)!] : []));
}

function serviceHosts(inventory: Inventory): Map<string, string> {
  const byHost = new Map<string, string>();
  for (const [name, { production, demo }] of Object.entries(inventory.services)) {
    for (const url of [production, demo]) if (url) byHost.set(hostOf(url)!, name);
  }
  return byHost;
}

/**
 * Compares the inventory with the spec. Spec files are tied to inventory services by host, and
 * operations match on (method, path with parameters reduced to `{}`). A spec operation without a
 * match is "gone", except one marked `x-wb-source: openapi` that the service's published
 * api-docs still contain: the backend documents it even though the frontend does not call it.
 * It is "unconfirmed" instead when its path still appears as a literal in the code (the URL is
 * built in a way the extractor cannot follow) or when it is marked `x-wb-source: live`
 * (verified against the server, not necessarily called by the frontend).
 * Request shapes are only compared for operations not imported from a published spec.
 */
export function diffInventory(inventory: Inventory, spec: Spec, published = new Map<string, Set<string>>()): DiffReport {
  const byService = new Map<string, Endpoint[]>();
  for (const e of inventory.endpoints) byService.set(e.service, [...(byService.get(e.service) ?? []), e]);
  const serviceByHost = serviceHosts(inventory);

  const matched = new Set<Endpoint>();
  const goneEndpoints: DiffReport['goneEndpoints'] = [];
  const unconfirmedEndpoints: DiffReport['unconfirmedEndpoints'] = [];
  const literals = (inventory.paths ?? []).map(pathKey);
  const changedEndpoints: ShapeChange[] = [];
  for (const file of spec.services) {
    const service = file.hosts.map((h) => serviceByHost.get(h)).find((s) => s !== undefined);
    const endpoints = service ? (byService.get(service) ?? []) : [];
    const docs = service ? published.get(service) : undefined;
    for (const op of file.operations) {
      const key = pathKey(op.path);
      const candidates = endpoints.filter((e) => e.method === op.method && pathsMatch(pathKey(e.path), key));
      if (!candidates.length) {
        if (op.source === 'openapi' && docs?.has(`${op.method} ${key}`)) continue;
        const ref = { service: service ?? file.service, method: op.method, path: op.path, file: file.file };
        if (literals.some((l) => key.endsWith(l))) unconfirmedEndpoints.push({ ...ref, reason: 'mentioned' });
        else if (op.source === 'live') unconfirmedEndpoints.push({ ...ref, reason: 'live' });
        else goneEndpoints.push(ref);
        continue;
      }
      candidates.forEach((e) => matched.add(e));
      const exact = candidates.find((e) => pathKey(e.path) === key) ?? candidates[0]!;
      const change = op.source === 'openapi' ? undefined : shapeChange(op, exact);
      if (change) changedEndpoints.push(change);
    }
  }

  // Excluded endpoints and the OAuth token endpoint (described by environments.yaml) are not new.
  const tokenUrl = spec.environments?.oauth?.tokenUrl;
  const known = (e: Endpoint) =>
    hostsOf(inventory, e.service).some(
      (host) =>
        spec.excluded.hosts.has(host) ||
        spec.excluded.endpoints.has(endpointKey(e.method, host, e.path)) ||
        (e.method === 'POST' && tokenUrl === `https://${host}${e.path}`),
    );

  // Hosts the frontend calls vs. hosts the spec knows; a spec host only counts as removed when
  // the frontend no longer mentions it at all (env config included).
  const calledHosts = [...serviceByHost.keys()].filter((h) => !spec.excluded.hosts.has(h));
  const mentionedHosts = new Set([...calledHosts, ...Object.values(inventory.env).map((u) => hostOf(u)).filter((h) => !!h)]);
  const specHosts = new Set([...(spec.environments?.hosts ?? []), ...spec.services.flatMap((s) => s.hosts)]);

  const report: DiffReport = {
    build: inventory.build,
    specFiles: spec.services.length,
    drift: false,
    newHosts: calledHosts.filter((h) => !specHosts.has(h)).sort(),
    removedHosts: [...specHosts].filter((h) => !mentionedHosts.has(h)).sort(),
    oauth: oauthChanges(inventory, spec),
    newEndpoints: inventory.endpoints.filter((e) => !matched.has(e) && !known(e)).map(ref),
    goneEndpoints,
    unconfirmedEndpoints,
    changedEndpoints,
  };
  report.drift = !!(
    report.newHosts.length ||
    report.removedHosts.length ||
    report.oauth.length ||
    report.newEndpoints.length ||
    report.goneEndpoints.length ||
    report.changedEndpoints.length
  );
  return report;
}

function oauthChanges(inventory: Inventory, spec: Spec): DiffReport['oauth'] {
  const client = spec.environments?.oauth;
  if (!client) return [];
  const changes: DiffReport['oauth'] = [];
  if (client.tokenUrl !== undefined && client.tokenUrl !== inventory.oauth.tokenUrl) {
    changes.push({ field: 'tokenUrl', spec: client.tokenUrl, inventory: inventory.oauth.tokenUrl });
  }
  if (client.basic !== undefined && client.basic !== inventory.oauth.basic) {
    changes.push({ field: 'basic', spec: client.basic, inventory: inventory.oauth.basic });
  }
  return changes;
}

function groupByService<T extends EndpointRef>(items: T[]): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) groups.set(item.service, [...(groups.get(item.service) ?? []), item]);
  return groups;
}

const UNCONFIRMED = {
  mentioned: 'path is in the code, the call was not resolved',
  live: 'verified against the server, not called by the frontend',
};

function describeFields(label: string, names: string[] | undefined): string[] {
  return (names ?? []).map((k) => `${label} +${k}`);
}

export function renderMarkdown(report: DiffReport): string {
  const out: string[] = ['# whitebird API drift', ''];
  out.push(`Build \`${report.build.entry}\` (fetched ${report.build.fetchedAt}) vs ${report.specFiles} spec file(s).`, '');
  out.push(report.drift ? '**Drift detected.**' : 'No drift.', '');

  const section = (title: string, lines: string[]) => {
    if (!lines.length) return;
    out.push(`## ${title}`, '', ...lines, '');
  };
  section(`New hosts (${report.newHosts.length})`, report.newHosts.map((h) => `- \`${h}\``));
  section(`Removed hosts (${report.removedHosts.length})`, report.removedHosts.map((h) => `- \`${h}\``));
  section(
    'OAuth client',
    report.oauth.length
      ? ['| field | spec | inventory |', '| --- | --- | --- |', ...report.oauth.map((c) => `| ${c.field} | \`${c.spec}\` | \`${c.inventory}\` |`)]
      : [],
  );
  const endpointList = (items: EndpointRef[]) =>
    [...groupByService(items)].flatMap(([service, list]) => [
      `### ${service} (${list.length})`,
      '',
      ...list.map((e) => `- \`${e.method} ${e.path}\``),
      '',
    ]);
  section(`New endpoints (${report.newEndpoints.length})`, endpointList(report.newEndpoints));
  section(`Gone endpoints (${report.goneEndpoints.length})`, endpointList(report.goneEndpoints));
  section(
    `Unconfirmed endpoints (${report.unconfirmedEndpoints.length}, not drift)`,
    report.unconfirmedEndpoints.map(
      (e) => `- \`${e.method} ${e.path}\` (${e.service}): ${UNCONFIRMED[e.reason]}`,
    ),
  );
  section(
    `Changed request shapes (${report.changedEndpoints.length})`,
    report.changedEndpoints.map((c) => {
      const details = [
        ...describeFields('query', c.query),
        ...describeFields('body', c.body),
        ...(c.bodyKind ? [`body kind ${c.bodyKind.spec} -> ${c.bodyKind.inventory}`] : []),
      ];
      return `- \`${c.method} ${c.path}\` (${c.service}): ${details.join(', ')}`;
    }),
  );
  return `${out.join('\n').trimEnd()}\n`;
}
