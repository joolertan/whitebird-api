import * as t from '@babel/types';

export function staticKey(prop: t.ObjectProperty | t.ObjectMethod | t.ClassMethod | t.ClassProperty): string | undefined {
  const key = prop.key;
  if (t.isIdentifier(key) && !prop.computed) return key.name;
  return staticString(key);
}

/** Value of a string constant; webcrack prints plain strings as expression-less template literals. */
export function staticString(node: t.Node | null | undefined): string | undefined {
  if (t.isStringLiteral(node)) return node.value;
  if (t.isNumericLiteral(node)) return String(node.value);
  if (t.isTemplateLiteral(node) && node.expressions.length === 0) return node.quasis[0]!.value.cooked ?? undefined;
  return undefined;
}

export function moduleName(node: t.Identifier | t.StringLiteral): string {
  return t.isIdentifier(node) ? node.name : node.value;
}
