import type { Hole, Part, Str } from './values.ts';

export interface ParsedUrl {
  host: string;
  /** Path with `{param}` placeholders. */
  path: string;
  query: string[];
}

export type UrlResult = { ok: true; url: ParsedUrl } | { ok: false; partial: string };

/** `{{NAME}}` templates used by config-driven clients, resolved to absolute base URLs. */
export type Placeholders = Map<string, string[]>;

const MARK = '\u0000';
const MAX_VARIANTS = 8;

export function renderPartial(parts: Part[]): string {
  return parts.map((p) => (typeof p === 'string' ? p : `{${p.name ?? '?'}}`)).join('');
}

export function parseUrl(value: Str, placeholders: Placeholders = new Map()): UrlResult[] {
  const holes: Hole[] = [];
  const text = value.parts
    .map((p) => {
      if (typeof p === 'string') return p;
      holes.push(p);
      return `${MARK}${holes.length - 1}${MARK}`;
    })
    .join('');
  return expandPlaceholders(text, placeholders).map((variant) => parseText(variant, holes, value));
}

function expandPlaceholders(text: string, placeholders: Placeholders): string[] {
  const match = /\{\{([A-Z0-9_]+)\}\}/.exec(text);
  if (!match) return [text];
  const options = placeholders.get(match[1]!) ?? [];
  if (!options.length) return [text];
  const head = text.slice(0, match.index);
  const tail = text.slice(match.index + match[0].length);
  return options.flatMap((o) => expandPlaceholders(head + o + tail, placeholders)).slice(0, MAX_VARIANTS);
}

function parseText(text: string, holes: Hole[], original: Str): UrlResult {
  const m = new RegExp(`^(https?)://([^/?#${MARK}]+)(.*)$`, 's').exec(text);
  if (!m || m[2]!.endsWith('.')) return { ok: false, partial: restore(text, holes) || renderPartial(original.parts) };
  const host = m[2]!.toLowerCase().replace(/:\d+$/, '');
  const rest = m[3]!.split('#')[0]!;
  const q = rest.indexOf('?');
  const rawPath = q < 0 ? rest : rest.slice(0, q);
  const rawQuery = q < 0 ? '' : rest.slice(q + 1);

  const query = new Set<string>();
  const pathHoles: Hole[] = [];
  let dynamicTail = false;
  const pathText = rawPath.replace(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'), (_, i: string, offset: number) => {
    const hole = holes[Number(i)]!;
    if (hole.query) {
      hole.query.forEach((k) => query.add(k));
      return '';
    }
    // `${base}${path}` or `/api/v1${path}`: the hole may span several segments.
    if (offset === 0 || rawPath[offset - 1] !== '/') dynamicTail = true;
    pathHoles.push(hole);
    return `${MARK}${pathHoles.length - 1}${MARK}`;
  });
  if (dynamicTail) return { ok: false, partial: restore(text, holes) };

  for (const pair of rawQuery.split('&')) {
    const key = pair.split('=')[0]!;
    if (!key) continue;
    if (!key.includes(MARK)) {
      query.add(decodeKey(key));
      continue;
    }
    for (const [, i] of key.matchAll(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'))) {
      holes[Number(i)]!.query?.forEach((k) => query.add(k));
    }
  }

  return { ok: true, url: { host, path: normalizePath(nameParams(pathText, pathHoles)), query: [...query].sort() } };
}

function decodeKey(key: string): string {
  try {
    return decodeURIComponent(key);
  } catch {
    return key;
  }
}

function restore(text: string, holes: Hole[]): string {
  return text.replace(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'), (_, i: string) => `{${holes[Number(i)]?.name ?? '?'}}`);
}

/** Names dynamic path segments after their source when meaningful, else `{id}` / `{id1}`, `{id2}`... */
function nameParams(pathText: string, holes: Hole[]): string {
  const unnamed = holes.filter((h) => !validName(h.name)).length;
  const used = new Set<string>();
  let positional = 0;
  return pathText.replace(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'), (_, i: string) => {
    const hole = holes[Number(i)]!;
    let name = validName(hole.name) ? hole.name! : unnamed === 1 ? 'id' : `id${++positional}`;
    if (used.has(name)) name = `${name}${used.size + 1}`;
    used.add(name);
    return `{${name}}`;
  });
}

function validName(name: string | undefined): boolean {
  return !!name && /^[A-Za-z_$][\w$]*$/.test(name);
}

export function normalizePath(path: string): string {
  const collapsed = `/${path}`.replace(/\/{2,}/g, '/');
  return collapsed.length > 1 ? collapsed.replace(/\/+$/, '') : collapsed;
}

/** Path with every parameter reduced to `{}`, the matching key used by `diff`. */
export function pathKey(path: string): string {
  return path.replace(/\{[^}]*\}/g, '{}');
}
