import { staticKey } from './ast.ts';
import { isUrl, mergeEnv } from './env.ts';
import { Evaluator } from './evaluate.ts';
import { Hosts } from './hosts.ts';
import {
  mergeEndpoints,
  sortRecord,
  sortUnresolved,
  type Endpoint,
  type Inventory,
  type OAuthInfo,
  type Unresolved,
} from './inventory.ts';
import { parseChunk } from './program.ts';
import { SiteBuilder, collect, locate, snippetOf, type Candidates, type Site } from './requests.ts';
import { normalizePath, parseUrl, pathKey, type ParsedUrl, type Placeholders } from './url.ts';
import { literal, strings } from './values.ts';

export interface ChunkSource {
  name: string;
  code: string;
}

export interface BuildInfo {
  entry: string;
  fetchedAt: string;
  chunks: string[];
}

/** A literal path segment, so `{?}/{?}` style keys of unrelated maps are not reported. */
const PATH_LIKE = /\/[A-Za-z][\w-]*/;
const SSE_PATH = /(^|\/)sse(\/|$)/;

export function extractInventory(sources: ChunkSource[], build: BuildInfo): Inventory {
  const ordered = [...sources].sort((a, b) => a.name.localeCompare(b.name));
  const chunks = new Map(ordered.map((s) => [s.name, parseChunk(s.name, s.code)]));
  const candidates = [...chunks.values()].map(collect);
  const env = mergeEnv(candidates.flatMap((c) => c.envObjects));
  const evaluator = new Evaluator(chunks, env);
  const hosts = new Hosts(env);
  const placeholders = placeholderTable(candidates, evaluator);
  const builder = new SiteBuilder(evaluator);

  const endpoints: Endpoint[] = [];
  const unresolved: Unresolved[] = [];
  const basics: { value: string; site: Site }[] = [];
  const observedHosts = new Set<string>();

  for (const c of candidates) {
    for (const site of builder.build(c)) {
      for (const result of strings(site.url).flatMap((s) => parseUrl(s, placeholders))) {
        if (!result.ok) {
          if (site.strict || PATH_LIKE.test(result.partial)) unresolved.push(unresolvedOf(site, result.partial));
          continue;
        }
        const { host, path } = result.url;
        if (!hosts.isApi(host, path)) continue;
        if (!site.methods.length) {
          unresolved.push(unresolvedOf(site, `{method} ${render(result.url)}`));
          continue;
        }
        observedHosts.add(host);
        if (site.basicAuth) basics.push({ value: site.basicAuth, site });
        for (const method of site.methods) endpoints.push(endpointOf(site, method, result.url, hosts));
      }
    }
  }

  const merged = mergeEndpoints(endpoints);
  const services = servicesOf(observedHosts, hosts);
  unresolved.push(...orphanUrls(candidates, evaluator, placeholders, hosts, merged, env));

  return {
    build: { ...build, chunks: [...build.chunks].sort() },
    env: sortRecord(new Map([...env].filter(([, v]) => isUrl(v)))),
    oauth: oauthOf(merged, services, env, basics, candidates),
    services,
    endpoints: merged,
    unresolved: sortUnresolved(unresolved),
  };
}

function render(url: ParsedUrl): string {
  return `https://${url.host}${url.path}${url.query.length ? `?${url.query.join('&')}` : ''}`;
}

function unresolvedOf(site: Site, partial: string): Unresolved {
  return { chunk: site.chunk, line: site.line, symbol: site.symbol, snippet: site.snippet, partial };
}

function endpointOf(site: Site, method: string, url: ParsedUrl, hosts: Hosts): Endpoint {
  const sse = site.responseType === 'json' && SSE_PATH.test(url.path);
  return {
    service: hosts.serviceName(url.host),
    method,
    path: url.path,
    query: [...new Set([...site.query, ...url.query])].sort(),
    body: site.body,
    headers: site.headers,
    responseType: sse ? 'event-stream' : site.responseType,
    sources: [{ chunk: site.chunk, line: site.line, symbol: site.symbol }],
  };
}

/** Values of `{BASE_URL_X: ...}`-style properties, substituted into `{{BASE_URL_X}}` templates. */
function placeholderTable(candidates: Candidates[], evaluator: Evaluator): Placeholders {
  const table = new Map<string, Set<string>>();
  for (const c of candidates) {
    const ctx = evaluator.root(c.chunk.name);
    for (const prop of c.placeholders) {
      const key = staticKey(prop.node)!;
      const values = evaluator.enumerate(() => evaluator.evaluate(prop.get('value'), ctx));
      for (const option of values.flatMap(strings)) {
        const text = literal(option);
        if (!text || !/^https?:\/\/[^/]+/.test(text)) continue;
        if (!table.has(key)) table.set(key, new Set());
        table.get(key)!.add(text.replace(/\/+$/, ''));
      }
    }
  }
  return new Map([...table].map(([k, v]) => [k, [...v].sort()]));
}

/** API URLs that appear in the code but feed no detected request (redirects, iframes, missed clients). */
function orphanUrls(
  candidates: Candidates[],
  evaluator: Evaluator,
  placeholders: Placeholders,
  hosts: Hosts,
  endpoints: Endpoint[],
  env: Map<string, string>,
): Unresolved[] {
  // Known paths and their segment prefixes, so base URLs like `https://x/api/v1` are not orphans.
  const known = new Set<string>();
  for (const e of endpoints) {
    const segments = pathKey(e.path).split('/');
    for (let i = 2; i <= segments.length; i++) known.add(`${e.service} ${segments.slice(0, i).join('/')}`);
  }
  for (const value of env.values()) {
    for (const r of parseUrl({ kind: 'str', parts: [value] })) {
      if (r.ok) known.add(`${hosts.serviceName(r.url.host)} ${pathKey(normalizePath(r.url.path))}`);
    }
  }
  const orphans: Unresolved[] = [];
  for (const c of candidates) {
    const ctx = evaluator.root(c.chunk.name);
    for (const path of c.urlLiterals) {
      const values = evaluator.enumerate(() => evaluator.evaluate(path, ctx));
      for (const s of values.flatMap(strings)) {
        for (const r of parseUrl(s, placeholders)) {
          if (!r.ok || !hosts.isApi(r.url.host, r.url.path) || r.url.path === '/') continue;
          const key = `${hosts.serviceName(r.url.host)} ${pathKey(r.url.path)}`;
          if (known.has(key)) continue;
          orphans.push({ ...locate(path, c.chunk.name), snippet: snippetOf(path, c.chunk.code), partial: render(r.url) });
        }
      }
    }
  }
  return orphans;
}

function servicesOf(observedHosts: Set<string>, hosts: Hosts): Inventory['services'] {
  const services = new Map<string, Inventory['services'][string]>();
  for (const host of [...observedHosts].sort()) {
    const live = hosts.liveHost(host);
    const name = hosts.serviceName(host);
    const entry = services.get(name) ?? hosts.describe(live);
    if (host !== live && !entry.demo) entry.demo = `https://${host}`;
    services.set(name, entry);
  }
  return sortRecord(services);
}

function decodeClientId(basic: string): string | null {
  const decoded = Buffer.from(basic, 'base64').toString('utf8');
  return /^[\x20-\x7e]+:/.test(decoded) ? decoded.split(':')[0]! : null;
}

function oauthOf(
  endpoints: Endpoint[],
  services: Inventory['services'],
  env: Map<string, string>,
  basics: { value: string; site: Site }[],
  candidates: Candidates[],
): OAuthInfo {
  const token = endpoints.find((e) => e.method === 'POST' && e.path.endsWith('/oauth/token'));
  const tokenOrigin = token ? services[token.service]?.production : undefined;
  const tokenUrl = token && tokenOrigin ? `${tokenOrigin}${token.path}` : null;

  const envBasic = [...env].filter(([k]) => /BASIC/.test(k)).map(([, v]) => v);
  const literals = [...new Set(basics.map((b) => b.value).filter((v) => !v.includes('{')))].sort();
  const basic = envBasic[0] ?? literals[0] ?? null;
  const envVars = [...env]
    .filter(([k, v]) => v === basic || (tokenOrigin && v.replace(/\/+$/, '') === tokenOrigin && /TOKEN/.test(k)))
    .map(([k]) => k)
    .sort();

  const notes: string[] = [];
  if (envVars.length) notes.push(`env: ${envVars.join(', ')}`);
  const hardcoded = basics.filter((b) => b.value === basic).map((b) => `${b.site.chunk}:${b.site.line}`);
  if (hardcoded.length) notes.push(`hardcoded in ${[...new Set(hardcoded)].sort().join(', ')}`);
  const others = literals.filter((v) => v !== basic);
  if (others.length) notes.push(`other Basic values: ${others.join(', ')}`);
  if (token) notes.push(`token request body: ${token.body.kind} [${token.body.fields.join(', ')}]`);

  return {
    tokenUrl,
    basic,
    clientId: basic ? decodeClientId(basic) : null,
    envVars,
    grantTypes: [...new Set(candidates.flatMap((c) => [...c.grantTypes]))].sort(),
    notes: notes.join('; '),
  };
}
