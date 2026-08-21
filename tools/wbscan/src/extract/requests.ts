import type { NodePath, Scope } from '@babel/traverse';
import * as t from '@babel/types';
import { staticKey, staticString } from './ast.ts';
import { envEntries } from './env.ts';
import type { Evaluator } from './evaluate.ts';
import type { Chunk } from './program.ts';
import { UNDEF, concat, fieldNames, literal, str, strings, type Ctx, type Obj, type Val } from './values.ts';

export type BodyKind = 'json' | 'form-urlencoded' | 'multipart' | 'none';
export type ResponseType = 'json' | 'blob' | 'text' | 'arraybuffer' | 'event-stream';

export interface Body {
  kind: BodyKind;
  fields: string[];
}

export interface Location {
  chunk: string;
  line: number;
  symbol: string | null;
}

export interface Site extends Location {
  snippet: string;
  /** Uppercase HTTP methods; empty when the method could not be resolved. */
  methods: string[];
  url: Val;
  query: string[];
  body: Body;
  headers: string[];
  responseType: ResponseType;
  basicAuth?: string;
  /** Request-shaped enough that an unresolvable URL is worth reporting. */
  strict: boolean;
}

/** Nodes of one chunk worth evaluating, gathered in a single traversal. */
export interface Candidates {
  chunk: Chunk;
  calls: NodePath<t.CallExpression>[];
  eventSources: NodePath<t.NewExpression>[];
  configs: NodePath<t.ObjectExpression>[];
  placeholders: NodePath<t.ObjectProperty>[];
  urlLiterals: NodePath<t.StringLiteral | t.TemplateLiteral>[];
  envObjects: Map<string, string>[];
  grantTypes: Set<string>;
}

const VERBS = new Set(['get', 'delete', 'head', 'options', 'post', 'put', 'patch']);
const BODY_VERBS = new Set(['post', 'put', 'patch']);
const HTTP_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']);
const SSE_HANDLERS = new Set(['onmessage', 'onopen']);
const PLACEHOLDER_KEY = /^(?!VITE_)[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$/;
const ABSOLUTE_URL = /^https?:\/\/[\w.-]+(:\d+)?\/\S*$/i;
/** Object keys that describe a request rather than name it; skipped when picking a symbol. */
const GENERIC_KEYS = new Set([
  'live', 'demo', 'spot', 'url', 'path', 'method', 'data', 'params', 'headers', 'body',
  'queryFn', 'mutationFn', 'queryKey', 'default', 'config', 'request',
]);

const NO_BODY: Body = { kind: 'none', fields: [] };

function keysOf(node: t.ObjectExpression): Set<string> {
  const keys = new Set<string>();
  for (const prop of node.properties) {
    if ((t.isObjectProperty(prop) || t.isObjectMethod(prop)) && !prop.computed) {
      const key = staticKey(prop);
      if (key) keys.add(key);
    }
  }
  return keys;
}

function isGlobal(node: t.Node, name: string, scope: Scope): boolean {
  return t.isIdentifier(node, { name }) && !scope.hasBinding(name);
}

function isFetch(callee: t.Node, scope: Scope): boolean {
  if (isGlobal(callee, 'fetch', scope)) return true;
  return (
    t.isMemberExpression(callee) &&
    t.isIdentifier(callee.property, { name: 'fetch' }) &&
    ['window', 'globalThis', 'self'].some((g) => isGlobal(callee.object, g, scope))
  );
}

function verbOf(callee: t.Node): string | undefined {
  if (!t.isMemberExpression(callee) || callee.computed || !t.isIdentifier(callee.property)) return undefined;
  return VERBS.has(callee.property.name) ? callee.property.name : undefined;
}

function hasSseHandlers(arg: t.Node | undefined): boolean {
  return t.isObjectExpression(arg) && [...keysOf(arg)].some((k) => SSE_HANDLERS.has(k));
}

function isUrlLiteral(path: NodePath<t.StringLiteral | t.TemplateLiteral>): boolean {
  const parent = path.parent;
  if (t.isBinaryExpression(parent) || t.isTemplateLiteral(parent)) return false;
  if (t.isObjectProperty(parent) && parent.value === path.node) {
    const key = staticKey(parent);
    if (key && (key.startsWith('VITE_') || PLACEHOLDER_KEY.test(key))) return false;
  }
  if (path.isStringLiteral()) return ABSOLUTE_URL.test(path.node.value);
  const quasis = (path.node as t.TemplateLiteral).quasis.map((q) => q.value.cooked ?? q.value.raw);
  if (quasis.length === 1) return ABSOLUTE_URL.test(quasis[0]!);
  return /^https?:\/\//.test(quasis[0]!) || (quasis[0] === '' && quasis.length > 1 && quasis[1]!.startsWith('/'));
}

export function collect(chunk: Chunk): Candidates {
  const c: Candidates = {
    chunk,
    calls: [],
    eventSources: [],
    configs: [],
    placeholders: [],
    urlLiterals: [],
    envObjects: [],
    grantTypes: new Set(),
  };
  chunk.program.traverse({
    CallExpression(path) {
      const { callee, arguments: args } = path.node;
      if (!args.length) return;
      if (verbOf(callee) || isFetch(callee, path.scope) || hasSseHandlers(args[1])) c.calls.push(path);
      const grantType = staticString(args[0]) === 'grant_type' ? staticString(args[1]) : undefined;
      if (grantType) c.grantTypes.add(grantType);
    },
    NewExpression(path) {
      if (isGlobal(path.node.callee, 'EventSource', path.scope)) c.eventSources.push(path);
    },
    ObjectExpression(path) {
      const keys = keysOf(path.node);
      if (keys.has('url') || (keys.has('path') && keys.has('method'))) c.configs.push(path);
      const env = envEntries(path.node);
      if (env) c.envObjects.push(env);
    },
    ObjectProperty(path) {
      const key = staticKey(path.node);
      if (key && PLACEHOLDER_KEY.test(key)) c.placeholders.push(path);
      const grantType = key === 'grant_type' ? staticString(path.node.value) : undefined;
      if (grantType) c.grantTypes.add(grantType);
    },
    'StringLiteral|TemplateLiteral'(path) {
      const p = path as NodePath<t.StringLiteral | t.TemplateLiteral>;
      if (isUrlLiteral(p)) c.urlLiterals.push(p);
    },
  });
  return c;
}

export function locate(path: NodePath, chunk: string): Location {
  return { chunk, line: path.node.loc?.start.line ?? 0, symbol: symbolOf(path) };
}

/**
 * Name of the function or module-level constant holding a site. Locals inside a function
 * usually hold the response (`let n = await C.post(...)`), so they are skipped until the
 * enclosing function's own name is reached.
 */
function symbolOf(path: NodePath): string | null {
  let crossedFunction = false;
  for (let p: NodePath | null = path.parentPath; p; p = p.parentPath) {
    const node = p.node;
    if (t.isObjectProperty(node) || t.isObjectMethod(node) || t.isClassMethod(node) || t.isClassProperty(node)) {
      const key = staticKey(node);
      if (key && !GENERIC_KEYS.has(key)) return key;
    } else if (t.isFunctionDeclaration(node) && node.id) {
      return node.id.name;
    } else if (t.isVariableDeclarator(node) && t.isIdentifier(node.id)) {
      if (crossedFunction || p.scope.path.isProgram()) return node.id.name;
    } else if (t.isAssignmentExpression(node)) {
      const left = node.left;
      if (t.isMemberExpression(left) && t.isIdentifier(left.property)) return left.property.name;
      if (t.isIdentifier(left) && (crossedFunction || p.scope.getBinding(left.name)?.scope.path.isProgram())) return left.name;
    }
    if (p.isFunction()) crossedFunction = true;
  }
  return null;
}

export function snippetOf(path: NodePath, code: string, max = 200): string {
  const { start, end } = path.node;
  const text = code.slice(start ?? 0, end ?? 0).replace(/\s+/g, ' ').trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

interface RequestParts {
  method?: Val;
  defaultMethod: string;
  url: Val;
  data?: Val;
  params?: Val;
  headers?: Val;
  responseType?: Val;
  sse?: boolean;
  strict: boolean;
}

function prop(obj: Val, key: string): Val | undefined {
  return obj.kind === 'obj' ? obj.props.get(key)?.() : undefined;
}

function methodsOf(v: Val | undefined, fallback: string): string[] {
  if (!v || v.kind === 'undef') return [fallback];
  const methods = strings(v)
    .map((s) => literal(s)?.toUpperCase())
    .filter((m): m is string => !!m && HTTP_METHODS.has(m));
  return [...new Set(methods)];
}

interface HeaderInfo {
  names: string[];
  contentType?: string;
  accept?: string;
  basic?: string;
}

function headersOf(v: Val | undefined): HeaderInfo {
  const info: HeaderInfo = { names: [] };
  if (v?.kind !== 'obj') return info;
  for (const [key, value] of v.props) {
    const name = key.toLowerCase();
    if (name === 'content-type') info.contentType = literal(value())?.toLowerCase();
    else if (name === 'accept') info.accept = literal(value())?.toLowerCase();
    else info.names.push(name);
    if (name === 'authorization') {
      const auth = literal(value());
      if (auth?.startsWith('Basic ') && auth.length > 6) info.basic = auth.slice(6);
    }
  }
  return info;
}

export function bodyOf(data: Val | undefined, contentType?: string): Body {
  if (!data || data.kind === 'undef') return NO_BODY;
  let kind: BodyKind = data.kind === 'form' ? 'multipart' : data.kind === 'query' ? 'form-urlencoded' : 'json';
  if (contentType?.includes('multipart')) kind = 'multipart';
  else if (contentType?.includes('urlencoded')) kind = 'form-urlencoded';
  else if (contentType?.includes('json')) kind = 'json';
  return { kind, fields: [...new Set(fieldNames(data))].sort() };
}

function responseTypeOf(parts: RequestParts, accept: string | undefined): ResponseType {
  const declared = parts.responseType && literal(parts.responseType)?.toLowerCase();
  if (declared === 'blob' || declared === 'text' || declared === 'arraybuffer' || declared === 'json') return declared;
  if (declared === 'stream' || parts.sse || accept?.includes('text/event-stream')) return 'event-stream';
  return 'json';
}

/** Turns call sites and request-config objects of one chunk into sites. */
export class SiteBuilder {
  private readonly ev: Evaluator;

  constructor(evaluator: Evaluator) {
    this.ev = evaluator;
  }

  build(c: Candidates): Site[] {
    const ctx = this.ev.root(c.chunk.name);
    const sites: Site[] = [];
    const visit = (path: NodePath, describe: () => RequestParts | undefined) => {
      const variants = this.ev.enumerate(() => {
        const parts = describe();
        if (!parts || (this.ev.choosing && !this.ev.reachable(path, ctx))) return undefined;
        return this.site(path, c.chunk, parts);
      });
      for (const site of variants) if (site) sites.push(site);
    };
    for (const path of c.calls) visit(path, () => this.callParts(path, ctx));
    for (const path of c.eventSources) {
      visit(path, () => {
        const [url] = path.get('arguments') as NodePath[];
        return url ? { defaultMethod: 'GET', url: this.ev.evaluate(url, ctx), sse: true, strict: true } : undefined;
      });
    }
    for (const path of c.configs) visit(path, () => this.configParts(this.ev.evaluate(path, ctx) as Obj));
    return sites;
  }

  /** `client.verb(url, [data], config)`, `fetch(url, init)` and fetch-event-source style calls. */
  private callParts(path: NodePath<t.CallExpression>, ctx: Ctx): RequestParts {
    const args = path.get('arguments') as NodePath[];
    const evaluate = (p: NodePath | undefined) => (p ? this.ev.evaluate(p, ctx) : UNDEF);
    const verb = verbOf(path.node.callee);
    if (verb) {
      const hasBody = BODY_VERBS.has(verb);
      const config = evaluate(hasBody ? args[2] : args[1]);
      return {
        method: str(verb),
        defaultMethod: 'GET',
        url: evaluate(args[0]),
        data: hasBody ? evaluate(args[1]) : prop(config, 'data'),
        params: prop(config, 'params'),
        headers: prop(config, 'headers'),
        responseType: prop(config, 'responseType'),
        strict: false,
      };
    }
    const init = evaluate(args[1]);
    const sse = hasSseHandlers(args[1]?.node);
    return {
      method: prop(init, 'method'),
      defaultMethod: 'GET',
      url: evaluate(args[0]),
      data: prop(init, 'body'),
      headers: prop(init, 'headers'),
      responseType: sse ? undefined : this.fetchResponseType(path),
      sse,
      strict: sse,
    };
  }

  /** axios-style `{url, method, data, params, headers}` or action-style `{path, method}` objects. */
  private configParts(config: Obj): RequestParts | undefined {
    const method = prop(config, 'method');
    const url = prop(config, 'url');
    if (url) {
      const baseURL = prop(config, 'baseURL');
      const first = strings(url)[0]!.parts[0];
      const absolute = typeof first === 'string' && /^[a-z]+:\/\//i.test(first);
      return {
        method,
        defaultMethod: 'GET',
        url: baseURL && !absolute ? concat(baseURL, str('/'), url) : url,
        data: prop(config, 'data') ?? prop(config, 'body'),
        params: prop(config, 'params'),
        headers: prop(config, 'headers'),
        responseType: prop(config, 'responseType'),
        strict: !!method,
      };
    }
    if (!method || !methodsOf(method, '').length) return undefined;
    return { method, defaultMethod: 'GET', url: prop(config, 'path')!, strict: true };
  }

  private fetchResponseType(path: NodePath): Val | undefined {
    const awaited = path.parentPath?.isAwaitExpression() ? path.parentPath : path;
    const member = awaited.parentPath;
    if (!member?.isMemberExpression() || !t.isIdentifier(member.node.property)) return undefined;
    const reader = member.node.property.name;
    if (reader === 'blob' || reader === 'text') return str(reader);
    return reader === 'arrayBuffer' ? str('arraybuffer') : undefined;
  }

  private site(path: NodePath, chunk: Chunk, parts: RequestParts): Site {
    const headers = headersOf(parts.headers);
    const site: Site = {
      ...locate(path, chunk.name),
      snippet: snippetOf(path, chunk.code),
      methods: methodsOf(parts.method, parts.defaultMethod),
      url: parts.url,
      query: [...new Set(fieldNames(parts.params ?? UNDEF))].sort(),
      body: bodyOf(parts.data, headers.contentType),
      headers: headers.names.sort(),
      responseType: responseTypeOf(parts, headers.accept),
      strict: parts.strict,
    };
    if (headers.basic) site.basicAuth = headers.basic;
    return site;
  }
}
