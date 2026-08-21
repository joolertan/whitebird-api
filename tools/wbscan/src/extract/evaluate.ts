import type { Binding, NodePath } from '@babel/traverse';
import * as t from '@babel/types';
import { moduleName, staticKey } from './ast.ts';
import { chunkNameOf, type Chunk } from './program.ts';
import {
  UNDEF,
  concat,
  either,
  fieldNames,
  isKnown,
  literal,
  str,
  strings,
  unknown,
  type Ctx,
  type Fn,
  type Obj,
  type Val,
} from './values.ts';

const MAX_INLINE_DEPTH = 4;
const MAX_STACK = 400;
const MAX_INLINE_STATEMENTS = 30;
const MAX_TABLE_KEYS = 32;

interface Choice {
  id: string;
  keys: string[];
}

/**
 * Small symbolic evaluator: resolves expressions to strings with holes by following
 * constant bindings (including Rolldown's `var x; ... x = value` lazy-init pattern),
 * cross-chunk imports, object literals and calls to small functions.
 *
 * Lookups like `table[key]` with an unknown key are "choices": `enumerate()` re-runs an
 * evaluation once per table key, keeping the key consistent for every lookup by the same
 * expression, so related tables (url, method) stay paired.
 */
export class Evaluator {
  private readonly chunks: Map<string, Chunk>;
  private readonly env: Map<string, string>;
  private readonly memo = new Map<Binding, Val>();
  private readonly active = new Set<Binding>();
  private readonly bindingIds = new Map<Binding, number>();
  private forced = new Map<string, string>();
  /** Bindings used as a chosen lookup key hold that key for the rest of the run. */
  private forcedBindings = new Map<Binding, Val>();
  private discovered: Choice[] = [];
  /** Bumped whenever a choice is made; values depending on one are not cached. */
  private choices = 0;
  private stack = 0;

  constructor(chunks: Map<string, Chunk>, env: Map<string, string>) {
    this.chunks = chunks;
    this.env = env;
  }

  root(chunk: string): Ctx {
    return { chunk, params: new Map(), depth: 0 };
  }

  /** Runs `fn` once per key of the first lookup table it indexes with an unknown key. */
  enumerate<T>(fn: () => T): T[] {
    const reset = (forced: [string, string][] = []) => {
      this.forced = new Map(forced);
      this.forcedBindings = new Map();
      this.discovered = [];
    };
    reset();
    const results = [fn()];
    const first = this.discovered[0];
    for (const key of first?.keys.slice(1) ?? []) {
      reset([[first!.id, key]]);
      results.push(fn());
    }
    reset();
    return results;
  }

  /** Whether a choice was made since enumeration started, i.e. guards may be decidable. */
  get choosing(): boolean {
    return this.forced.size > 0;
  }

  evaluate(path: NodePath, ctx: Ctx): Val {
    if (this.stack >= MAX_STACK) return unknown();
    this.stack++;
    try {
      return this.dispatch(path, ctx);
    } finally {
      this.stack--;
    }
  }

  /** False when an enclosing condition is statically known to exclude `path`. */
  reachable(path: NodePath, ctx: Ctx): boolean {
    let child = path;
    for (let parent = path.parentPath; parent && !child.isFunction(); child = parent, parent = parent.parentPath) {
      let test: NodePath | undefined;
      let expected: boolean | undefined;
      if ((parent.isConditionalExpression() || parent.isIfStatement()) && child.key !== 'test') {
        test = parent.get('test') as NodePath;
        expected = child.key === 'consequent';
      } else if (parent.isLogicalExpression() && child.key === 'right' && parent.node.operator !== '??') {
        test = parent.get('left');
        expected = parent.node.operator === '&&';
      }
      if (test && expected !== undefined && this.truth(test, ctx) === !expected) return false;
    }
    return true;
  }

  private truth(path: NodePath, ctx: Ctx): boolean | undefined {
    if (path.isUnaryExpression({ operator: '!' })) {
      const inner = this.truth(path.get('argument'), ctx);
      return inner === undefined ? undefined : !inner;
    }
    if (!path.isBinaryExpression() || !['===', '==', '!==', '!='].includes(path.node.operator)) return undefined;
    const left = literal(this.evaluate(path.get('left'), ctx));
    const right = literal(this.evaluate(path.get('right'), ctx));
    if (left === undefined || right === undefined) return undefined;
    return path.node.operator.startsWith('!') ? left !== right : left === right;
  }

  private dispatch(path: NodePath, ctx: Ctx): Val {
    const node = path.node;
    switch (node.type) {
      case 'StringLiteral':
        return str(node.value);
      case 'NumericLiteral':
      case 'BooleanLiteral':
        return str(String(node.value));
      case 'NullLiteral':
        return UNDEF;
      case 'TemplateLiteral':
        return this.template(path as NodePath<t.TemplateLiteral>, ctx);
      case 'BinaryExpression':
        if (node.operator !== '+') return unknown();
        return concat(this.evaluate(path.get('left') as NodePath, ctx), this.evaluate(path.get('right') as NodePath, ctx));
      case 'Identifier':
        return this.identifier(path as NodePath<t.Identifier>, ctx);
      case 'MemberExpression':
      case 'OptionalMemberExpression':
        return this.member(path as NodePath<t.MemberExpression>, ctx);
      case 'CallExpression':
      case 'OptionalCallExpression':
        return this.call(path as NodePath<t.CallExpression>, ctx);
      case 'NewExpression':
        return this.construct(path as NodePath<t.NewExpression>, ctx);
      case 'ConditionalExpression':
        return either(this.evaluate(path.get('consequent') as NodePath, ctx), this.evaluate(path.get('alternate') as NodePath, ctx));
      case 'LogicalExpression':
        return this.logical(path as NodePath<t.LogicalExpression>, ctx);
      case 'ObjectExpression':
        return this.object(path as NodePath<t.ObjectExpression>, ctx);
      case 'ArrowFunctionExpression':
      case 'FunctionExpression':
        return { kind: 'fn', path: path as NodePath<t.Function>, ctx };
      case 'AwaitExpression':
        return this.evaluate(path.get('argument') as NodePath, ctx);
      case 'ParenthesizedExpression':
        return this.evaluate(path.get('expression') as NodePath, ctx);
      case 'SequenceExpression':
        return this.evaluate((path.get('expressions') as NodePath[]).at(-1)!, ctx);
      case 'AssignmentExpression':
        return node.operator === '=' ? this.evaluate(path.get('right') as NodePath, ctx) : unknown();
      case 'UnaryExpression':
        return node.operator === 'void' ? UNDEF : unknown();
      default:
        return unknown();
    }
  }

  private template(path: NodePath<t.TemplateLiteral>, ctx: Ctx): Val {
    const exprs = path.get('expressions') as NodePath[];
    const pieces: Val[] = [];
    path.node.quasis.forEach((q, i) => {
      pieces.push(str(q.value.cooked ?? q.value.raw));
      if (exprs[i]) pieces.push(this.evaluate(exprs[i], ctx));
    });
    return concat(...pieces);
  }

  private identifier(path: NodePath<t.Identifier>, ctx: Ctx): Val {
    const name = path.node.name;
    const binding = path.scope.getBinding(name);
    if (!binding) return name === 'undefined' ? UNDEF : unknown();
    return this.binding(binding, ctx);
  }

  private binding(binding: Binding, ctx: Ctx): Val {
    const forced = this.forcedBindings.get(binding);
    if (forced) {
      this.choices++;
      return forced;
    }
    const bound = ctx.params.get(binding);
    if (bound) return bound;
    const moduleLevel = binding.scope.path.isProgram();
    const cached = moduleLevel ? this.memo.get(binding) : undefined;
    if (cached) return cached;
    if (this.active.has(binding)) return unknown();
    this.active.add(binding);
    const before = this.choices;
    let value: Val;
    try {
      value = this.resolveBinding(binding, moduleLevel ? this.root(ctx.chunk) : ctx);
      value = this.withAppends(binding, value, ctx);
    } finally {
      this.active.delete(binding);
    }
    if (moduleLevel && this.choices === before) this.memo.set(binding, value);
    return value;
  }

  private resolveBinding(binding: Binding, ctx: Ctx): Val {
    const path = binding.path;
    switch (binding.kind) {
      case 'module':
        return this.imported(path);
      case 'param':
        return unknown(destructuredKey(path.node, binding.identifier) ?? meaningful(binding.identifier.name));
      case 'hoisted':
      case 'local':
        return path.isFunction() ? { kind: 'fn', path, ctx } : unknown();
      default:
        break;
    }
    if (!path.isVariableDeclarator()) return unknown();
    const id = path.node.id;
    if (!t.isIdentifier(id)) {
      const keys = patternKeys(id, binding.identifier);
      const init = path.get('init');
      if (!keys || !init.node) return unknown(destructuredKey(id, binding.identifier) ?? meaningful(binding.identifier.name));
      return keys.reduce<Val>(
        (value, key) => (value.kind === 'obj' ? (value.props.get(key)?.() ?? unknown(key)) : unknown(key)),
        this.evaluate(init as NodePath, ctx),
      );
    }

    const values: Val[] = [];
    const init = path.get('init');
    if (init.node) values.push(this.evaluate(init as NodePath, ctx));
    for (const violation of binding.constantViolations) {
      if (violation.isAssignmentExpression({ operator: '=' }) && t.isIdentifier(violation.node.left, { name: id.name })) {
        values.push(this.evaluate(violation.get('right'), ctx));
      } else if (!violation.isVariableDeclarator()) {
        return unknown(meaningful(id.name));
      }
    }
    const defined = values.filter((v) => v.kind !== 'undef');
    if (defined.length === 0) return values.length ? UNDEF : unknown(meaningful(id.name));
    return defined.reduce<Val>((a, b) => either(a, b), defined[0]!);
  }

  /** FormData / URLSearchParams values collect fields from later `.append()`/`.set()` calls. */
  private withAppends(binding: Binding, value: Val, ctx: Ctx): Val {
    if (value.kind !== 'form' && value.kind !== 'query') return value;
    const extra: string[] = [];
    for (const ref of binding.referencePaths) {
      const member = ref.parentPath;
      if (!member?.isMemberExpression({ object: ref.node })) continue;
      const prop = member.node.property;
      if (!t.isIdentifier(prop) || (prop.name !== 'append' && prop.name !== 'set')) continue;
      const call = member.parentPath;
      if (!call?.isCallExpression({ callee: member.node })) continue;
      const arg = call.get('arguments')[0];
      const key = arg && literal(this.evaluate(arg, ctx));
      if (key) extra.push(key);
    }
    return value.kind === 'form'
      ? { kind: 'form', fields: [...value.fields, ...extra] }
      : { kind: 'query', keys: [...value.keys, ...extra] };
  }

  private imported(path: NodePath): Val {
    const decl = path.parentPath;
    if (!decl?.isImportDeclaration()) return unknown();
    const chunk = chunkNameOf(decl.node.source.value);
    if (path.isImportNamespaceSpecifier()) return { kind: 'ns', chunk };
    if (path.isImportDefaultSpecifier()) return this.exported(chunk, 'default');
    if (path.isImportSpecifier()) return this.exported(chunk, moduleName(path.node.imported));
    return unknown();
  }

  private exported(chunkName: string, name: string, seen = new Set<string>()): Val {
    const chunk = this.chunks.get(chunkName);
    const key = `${chunkName}#${name}`;
    if (!chunk || seen.has(key)) return unknown();
    seen.add(key);
    const local = chunk.exports.get(name);
    if (local) {
      const binding = chunk.program.scope.getBinding(local);
      return binding ? this.binding(binding, this.root(chunkName)) : unknown();
    }
    const re = chunk.reexports.get(name);
    return re ? this.exported(re.chunk, re.name, seen) : unknown();
  }

  private member(path: NodePath<t.MemberExpression>, ctx: Ctx): Val {
    const property = path.get('property') as NodePath;
    const key = path.node.computed ? this.evaluate(property, ctx) : undefined;
    const name = path.node.computed ? literal(key!) : t.isIdentifier(path.node.property) ? path.node.property.name : undefined;
    const object = this.evaluate(path.get('object'), ctx);
    if (name === undefined) {
      return object.kind === 'obj' && key ? this.choose(object, key, property, ctx) : unknown();
    }
    if (object.kind === 'obj') {
      const prop = object.props.get(name);
      if (prop) return prop();
    }
    if (object.kind === 'ns') return this.exported(object.chunk, name);
    const envValue = this.env.get(name);
    if (envValue !== undefined) return str(envValue);
    return unknown(name);
  }

  private choose(table: Obj, key: Val, property: NodePath, ctx: Ctx): Val {
    const candidates = key.kind === 'alt' ? key.options.map(literal).filter((k) => k !== undefined) : [];
    const keys = (candidates.length ? candidates : [...table.props.keys()]).filter((k) => table.props.has(k));
    if (keys.length === 0 || keys.length > MAX_TABLE_KEYS) return unknown();
    const id = this.choiceId(property, ctx);
    this.choices++;
    let chosen = this.forced.get(id);
    if (chosen === undefined) {
      chosen = keys[0]!;
      this.forced.set(id, chosen);
      this.discovered.push({ id, keys });
    }
    const binding = property.isIdentifier() ? property.scope.getBinding(property.node.name) : undefined;
    if (binding) this.forcedBindings.set(binding, str(chosen));
    return table.props.get(chosen)?.() ?? unknown();
  }

  /** Identifies a lookup key expression, e.g. `e` or `e.type` rooted at the same binding. */
  private choiceId(property: NodePath, ctx: Ctx): string {
    let root = property;
    while (root.isMemberExpression()) root = root.get('object');
    const binding = root.isIdentifier() ? root.scope.getBinding(root.node.name) : undefined;
    let scope = `${ctx.chunk}@${property.node.start}`;
    if (binding) {
      if (!this.bindingIds.has(binding)) this.bindingIds.set(binding, this.bindingIds.size);
      scope = `#${this.bindingIds.get(binding)}`;
    }
    const code = this.chunks.get(ctx.chunk)?.code ?? '';
    return `${scope}|${code.slice(property.node.start ?? 0, property.node.end ?? 0)}`;
  }

  private call(path: NodePath<t.CallExpression>, ctx: Ctx): Val {
    const callee = path.get('callee');
    const args = path.get('arguments') as NodePath[];
    const arg = (i: number) => (args[i] ? this.evaluate(args[i], ctx) : UNDEF);

    if (callee.isMemberExpression()) {
      const method = t.isIdentifier(callee.node.property) && !callee.node.computed ? callee.node.property.name : undefined;
      const object = callee.get('object');
      switch (method) {
        case 'toString':
        case 'trim':
          return this.evaluate(object, ctx);
        case 'concat': {
          const base = this.evaluate(object, ctx);
          return base.kind === 'str' || base.kind === 'alt' ? concat(base, ...args.map((a) => this.evaluate(a, ctx))) : unknown();
        }
        case 'stringify':
          if (object.isIdentifier({ name: 'JSON' }) && !object.scope.hasBinding('JSON')) return { kind: 'json', value: arg(0) };
          return { kind: 'query', keys: fieldNames(arg(0)) };
      }
    } else if (callee.isIdentifier() && !callee.scope.hasBinding(callee.node.name)) {
      if (['String', 'encodeURIComponent', 'encodeURI'].includes(callee.node.name)) return arg(0);
    }

    const target = this.evaluate(callee, ctx);
    if (target.kind === 'fn') return this.inline(target, args, ctx);

    // Calls like `getEnv(keys.VITE_X)` or `env("VITE_X")` read the build-time env config.
    const key = args.length ? literal(arg(0)) : undefined;
    if (key !== undefined && this.env.has(key)) return str(this.env.get(key)!);
    return unknown();
  }

  private inline(fn: Fn, args: NodePath[], callerCtx: Ctx): Val {
    if (callerCtx.depth >= MAX_INLINE_DEPTH) return unknown();
    const params = new Map(fn.ctx.params);
    const inner: Ctx = { chunk: fn.ctx.chunk, params, depth: callerCtx.depth + 1 };
    (fn.path.get('params') as NodePath[]).forEach((param, i) => {
      const argPath = args[i];
      const value = argPath && !argPath.isSpreadElement() ? this.evaluate(argPath, callerCtx) : UNDEF;
      this.bindParam(param, value, params, inner);
    });

    const body = fn.path.get('body') as NodePath;
    if (!body.isBlockStatement()) return this.evaluate(body, inner);
    if (body.node.body.length > MAX_INLINE_STATEMENTS) return unknown();
    const returns: NodePath<t.ReturnStatement>[] = [];
    body.traverse({
      Function(p) {
        p.skip();
      },
      ReturnStatement(p) {
        returns.push(p);
      },
    });
    const values = returns.map((r) => (r.node.argument ? this.evaluate(r.get('argument') as NodePath, inner) : UNDEF));
    return values.reduce<Val>((a, b) => either(a, b), values[0] ?? UNDEF);
  }

  private bindParam(param: NodePath, value: Val, params: Map<Binding, Val>, ctx: Ctx): void {
    if (param.isAssignmentPattern()) {
      const fallback = value.kind === 'undef' ? this.evaluate(param.get('right'), ctx) : value;
      this.bindParam(param.get('left'), fallback, params, ctx);
    } else if (param.isIdentifier()) {
      const binding = param.scope.getBinding(param.node.name);
      if (binding) params.set(binding, value);
    } else if (param.isObjectPattern()) {
      for (const prop of param.get('properties')) {
        if (!prop.isObjectProperty()) continue;
        const key = staticKey(prop.node);
        const sub = key !== undefined && value.kind === 'obj' ? value.props.get(key)?.() : undefined;
        this.bindParam(prop.get('value') as NodePath, sub ?? unknown(key), params, ctx);
      }
    }
  }

  private construct(path: NodePath<t.NewExpression>, ctx: Ctx): Val {
    const callee = path.node.callee;
    if (!t.isIdentifier(callee) || path.scope.hasBinding(callee.name)) return unknown();
    const args = path.get('arguments') as NodePath[];
    const arg = (i: number) => (args[i] ? this.evaluate(args[i], ctx) : UNDEF);
    switch (callee.name) {
      case 'FormData':
        return { kind: 'form', fields: [] };
      case 'URLSearchParams': {
        const init = arg(0);
        const text = literal(init);
        return { kind: 'query', keys: text !== undefined ? queryKeysOf(text) : fieldNames(init) };
      }
      case 'URL':
        return args.length > 1 ? resolveRelative(arg(0), arg(1)) : arg(0);
      default:
        return unknown();
    }
  }

  private logical(path: NodePath<t.LogicalExpression>, ctx: Ctx): Val {
    const right = () => this.evaluate(path.get('right'), ctx);
    if (path.node.operator === '&&') return right();
    const left = this.evaluate(path.get('left'), ctx);
    if (isKnown(left)) return left;
    const fallback = right();
    // `${id ?? ''}` keeps the hole; `${provider || 'SUMSUB'}` takes the default.
    return isKnown(fallback) && literal(fallback) !== '' ? fallback : left;
  }

  private object(path: NodePath<t.ObjectExpression>, ctx: Ctx): Obj {
    const props = new Map<string, () => Val>();
    for (const prop of path.get('properties')) {
      if (prop.isSpreadElement()) {
        const spread = this.evaluate(prop.get('argument'), ctx);
        if (spread.kind === 'obj') for (const [k, v] of spread.props) props.set(k, v);
        continue;
      }
      const key = prop.node.computed ? literal(this.evaluate(prop.get('key') as NodePath, ctx)) : staticKey(prop.node);
      if (key === undefined) continue;
      if (prop.isObjectMethod()) props.set(key, () => ({ kind: 'fn', path: prop, ctx }));
      else if (prop.isObjectProperty()) props.set(key, this.lazy(() => this.evaluate(prop.get('value') as NodePath, ctx)));
    }
    return { kind: 'obj', props };
  }

  private lazy(compute: () => Val): () => Val {
    let busy = false;
    let done = false;
    let value: Val = unknown();
    return () => {
      if (done) return value;
      if (busy) return unknown();
      busy = true;
      const before = this.choices;
      try {
        value = compute();
      } finally {
        busy = false;
      }
      done = this.choices === before;
      return value;
    };
  }
}

/** Property name a binding was destructured from, e.g. `id` in `({id: e}) => ...`. */
function destructuredKey(pattern: t.Node, id: t.Identifier): string | undefined {
  if (t.isAssignmentPattern(pattern)) return destructuredKey(pattern.left, id);
  if (t.isArrayPattern(pattern)) {
    for (const el of pattern.elements) {
      const found = el && destructuredKey(el, id);
      if (found) return found;
    }
  }
  if (!t.isObjectPattern(pattern)) return undefined;
  for (const prop of pattern.properties) {
    if (!t.isObjectProperty(prop)) continue;
    const target = t.isAssignmentPattern(prop.value) ? prop.value.left : prop.value;
    if (target === id) return staticKey(prop);
    const nested = destructuredKey(prop.value, id);
    if (nested) return nested;
  }
  return undefined;
}

/** Keys leading from an object pattern's root to `id`, e.g. `['data', 'url']` for `{data: {url: id}}`. */
function patternKeys(pattern: t.Node, id: t.Identifier): string[] | undefined {
  if (pattern === id) return [];
  if (t.isAssignmentPattern(pattern)) return patternKeys(pattern.left, id);
  if (!t.isObjectPattern(pattern)) return undefined;
  for (const prop of pattern.properties) {
    if (!t.isObjectProperty(prop)) continue;
    const key = staticKey(prop);
    const rest = key === undefined ? undefined : patternKeys(prop.value, id);
    if (rest) return [key!, ...rest];
  }
  return undefined;
}

/** Minified local names carry no meaning; anything longer than a minifier would emit does. */
export function meaningful(name: string): string | undefined {
  if (name.length < 4 || !/[a-z]/.test(name) || /^_?Component\d*$/.test(name)) return undefined;
  return name;
}

function queryKeysOf(text: string): string[] {
  return text
    .replace(/^\?/, '')
    .split('&')
    .map((pair) => pair.split('=')[0]!)
    .filter(Boolean);
}

function resolveRelative(rel: Val, base: Val): Val {
  const relText = strings(rel)[0]!.parts[0];
  if (typeof relText === 'string' && /^[a-z]+:\/\//i.test(relText)) return rel;
  const baseText = strings(base)[0]!.parts[0];
  if (typeof relText === 'string' && relText.startsWith('/') && typeof baseText === 'string') {
    const origin = /^[a-z]+:\/\/[^/]+/i.exec(baseText)?.[0];
    if (origin) return concat(str(origin), rel);
  }
  return concat(base, str('/'), rel);
}
