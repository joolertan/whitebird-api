import * as t from '@babel/types';
import { staticKey, staticString } from './ast.ts';

const ENV_KEY = /^VITE_[A-Z0-9_]+$/;
const MIN_ENV_KEYS = 3;

/**
 * Vite inlines `import.meta.env` as an object literal of string constants. A key map
 * (`{VITE_X: "VITE_X"}`) looks similar, so objects whose values merely repeat their keys are skipped.
 */
export function envEntries(node: t.ObjectExpression): Map<string, string> | undefined {
  const entries = new Map<string, string>();
  let echoes = 0;
  for (const prop of node.properties) {
    if (!t.isObjectProperty(prop)) continue;
    const key = staticKey(prop);
    if (!key || !ENV_KEY.test(key)) continue;
    const value = staticString(prop.value);
    if (value === undefined) continue;
    if (value === key) echoes++;
    entries.set(key, value);
  }
  if (entries.size < MIN_ENV_KEYS || echoes * 2 >= entries.size) return undefined;
  return entries;
}

/** Merges env objects found across chunks; the largest one wins on conflicts. */
export function mergeEnv(found: Map<string, string>[]): Map<string, string> {
  const merged = new Map<string, string>();
  for (const entries of [...found].sort((a, b) => a.size - b.size)) {
    for (const [k, v] of entries) merged.set(k, v);
  }
  return new Map([...merged].sort(([a], [b]) => a.localeCompare(b)));
}

export function isUrl(value: string): boolean {
  return /^(https?|wss?):\/\/[^\s]+$/i.test(value);
}
