import type { ServiceHosts } from './hosts.ts';
import type { Body, BodyKind, Location, ResponseType } from './requests.ts';
import { pathKey } from './url.ts';

export interface Endpoint {
  service: string;
  method: string;
  path: string;
  query: string[];
  body: Body;
  headers: string[];
  responseType: ResponseType;
  sources: Location[];
}

export interface Unresolved extends Location {
  snippet: string;
  /** Best-effort URL with unresolved parts as `{name}` / `{?}`. */
  partial: string;
}

export interface OAuthInfo {
  tokenUrl: string | null;
  basic: string | null;
  clientId: string | null;
  envVars: string[];
  grantTypes: string[];
  notes: string;
}

export interface Inventory {
  build: { entry: string; fetchedAt: string; chunks: string[] };
  env: Record<string, string>;
  oauth: OAuthInfo;
  services: Record<string, ServiceHosts>;
  endpoints: Endpoint[];
  unresolved: Unresolved[];
}

const BODY_RANK: BodyKind[] = ['none', 'json', 'form-urlencoded', 'multipart'];
const RESPONSE_RANK: ResponseType[] = ['json', 'text', 'arraybuffer', 'blob', 'event-stream'];

export function endpointKey(e: Pick<Endpoint, 'service' | 'method' | 'path'>): string {
  return `${e.service} ${e.method} ${pathKey(e.path)}`;
}

const union = (a: string[], b: string[]) => [...new Set([...a, ...b])].sort();

const genericParams = (path: string) => (path.match(/\{id\d*\}/g) ?? []).length;

function betterPath(a: string, b: string): string {
  const diff = genericParams(a) - genericParams(b);
  if (diff !== 0) return diff < 0 ? a : b;
  return a <= b ? a : b;
}

function compareLocation(a: Location, b: Location): number {
  return a.chunk.localeCompare(b.chunk) || a.line - b.line || (a.symbol ?? '').localeCompare(b.symbol ?? '');
}

function mergeSources(a: Location[], b: Location[]): Location[] {
  const all = new Map([...a, ...b].map((l) => [`${l.chunk}:${l.line}:${l.symbol}`, l]));
  return [...all.values()].sort(compareLocation);
}

function merge(a: Endpoint, b: Endpoint): Endpoint {
  const kind = BODY_RANK.indexOf(a.body.kind) >= BODY_RANK.indexOf(b.body.kind) ? a.body.kind : b.body.kind;
  return {
    service: a.service,
    method: a.method,
    path: betterPath(a.path, b.path),
    query: union(a.query, b.query),
    body: { kind, fields: union(a.body.fields, b.body.fields) },
    headers: union(a.headers, b.headers),
    responseType:
      RESPONSE_RANK.indexOf(a.responseType) >= RESPONSE_RANK.indexOf(b.responseType) ? a.responseType : b.responseType,
    sources: mergeSources(a.sources, b.sources),
  };
}

/** Collapses occurrences of the same operation and sorts everything for stable output. */
export function mergeEndpoints(endpoints: Endpoint[]): Endpoint[] {
  const byKey = new Map<string, Endpoint>();
  for (const e of endpoints) {
    const key = endpointKey(e);
    const existing = byKey.get(key);
    byKey.set(key, existing ? merge(existing, e) : { ...e, sources: mergeSources([], e.sources) });
  }
  return [...byKey.values()].sort(
    (a, b) => a.service.localeCompare(b.service) || a.path.localeCompare(b.path) || a.method.localeCompare(b.method),
  );
}

export function sortUnresolved(items: Unresolved[]): Unresolved[] {
  const unique = new Map(items.map((u) => [`${u.chunk}:${u.line}:${u.partial}`, u]));
  return [...unique.values()].sort((a, b) => compareLocation(a, b) || a.partial.localeCompare(b.partial));
}

export function sortRecord<T>(record: Map<string, T> | Record<string, T>): Record<string, T> {
  const entries = record instanceof Map ? [...record] : Object.entries(record);
  return Object.fromEntries(entries.sort(([a], [b]) => a.localeCompare(b)));
}
