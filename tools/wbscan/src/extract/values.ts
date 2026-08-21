import type { Binding, NodePath } from '@babel/traverse';
import type * as t from '@babel/types';

/** A dynamic piece of a string. `query` carries keys known to be serialized into it. */
export interface Hole {
  name?: string;
  query?: string[];
}

export type Part = string | Hole;

export interface Str {
  kind: 'str';
  parts: Part[];
}

export interface Obj {
  kind: 'obj';
  props: Map<string, () => Val>;
}

export interface Fn {
  kind: 'fn';
  path: NodePath<t.Function>;
  ctx: Ctx;
}

export type Val =
  | Str
  | Obj
  | Fn
  | { kind: 'alt'; options: Str[] }
  | { kind: 'form'; fields: string[] }
  | { kind: 'query'; keys: string[] }
  | { kind: 'json'; value: Val }
  | { kind: 'ns'; chunk: string }
  | { kind: 'undef' }
  | { kind: 'unknown'; name?: string };

/** Evaluation context: the chunk being read and argument values of inlined calls. */
export interface Ctx {
  chunk: string;
  params: Map<Binding, Val>;
  depth: number;
}

const MAX_ALTERNATIVES = 8;

export const UNDEF: Val = { kind: 'undef' };

export function str(value: string): Str {
  return { kind: 'str', parts: value ? [value] : [] };
}

export function unknown(name?: string): Val {
  return name ? { kind: 'unknown', name } : { kind: 'unknown' };
}

export function isKnown(v: Val): boolean {
  return v.kind !== 'unknown' && v.kind !== 'undef';
}

/** Literal string value, if `v` has no dynamic parts. */
export function literal(v: Val): string | undefined {
  if (v.kind !== 'str') return undefined;
  return v.parts.every((p) => typeof p === 'string') ? v.parts.join('') : undefined;
}

function toHole(v: Val): Hole {
  if (v.kind === 'query') return { query: v.keys };
  if (v.kind === 'unknown' && v.name) return { name: v.name };
  return {};
}

/** Coerces any value to string alternatives, as a template literal would. */
export function strings(v: Val): Str[] {
  if (v.kind === 'str') return [v];
  if (v.kind === 'alt') return v.options;
  return [{ kind: 'str', parts: [toHole(v)] }];
}

function join(a: Str, b: Str): Str {
  const parts = [...a.parts];
  for (const p of b.parts) {
    const last = parts.at(-1);
    if (typeof p === 'string' && typeof last === 'string') parts[parts.length - 1] = last + p;
    else parts.push(p);
  }
  return { kind: 'str', parts };
}

export function alternatives(options: Str[]): Val {
  const unique = [...new Map(options.map((o) => [JSON.stringify(o.parts), o])).values()];
  if (unique.length === 1) return unique[0]!;
  if (unique.length > MAX_ALTERNATIVES) return unknown();
  return { kind: 'alt', options: unique };
}

export function concat(...vals: Val[]): Val {
  let acc: Str[] = [str('')];
  for (const v of vals) {
    const next = strings(v);
    if (acc.length * next.length > MAX_ALTERNATIVES) return unknown();
    acc = acc.flatMap((a) => next.map((b) => join(a, b)));
  }
  return alternatives(acc);
}

const isStringy = (v: Val) => v.kind === 'str' || v.kind === 'alt';

/** Merges two branches of a conditional into alternatives (`cond ? '/email' : ''` yields both paths). */
export function either(a: Val, b: Val): Val {
  if (a.kind === 'undef') return b;
  if (b.kind === 'undef') return a;
  if (a.kind === 'obj' && b.kind === 'obj') return { kind: 'obj', props: new Map([...b.props, ...a.props]) };
  if (isStringy(a) && isStringy(b)) return alternatives([...strings(a), ...strings(b)]);
  if (literal(a) === '') return b;
  if (literal(b) === '') return a;
  return isKnown(a) ? a : b;
}

/** Field names of a request body or query value. */
export function fieldNames(v: Val): string[] {
  switch (v.kind) {
    case 'obj':
      return [...v.props.keys()];
    case 'form':
      return v.fields;
    case 'query':
      return v.keys;
    case 'json':
      return fieldNames(v.value);
    default:
      return [];
  }
}
