import { parse } from '@babel/parser';
import traverse, { type NodePath } from '@babel/traverse';
import * as t from '@babel/types';
import { posix } from 'node:path';
import { moduleName } from './ast.ts';

export interface Chunk {
  name: string;
  code: string;
  program: NodePath<t.Program>;
  /** exported name -> local binding name */
  exports: Map<string, string>;
  /** exported name -> re-exported binding of another chunk */
  reexports: Map<string, { chunk: string; name: string }>;
}

export function chunkNameOf(source: string): string {
  return posix.basename(source);
}

export function parseChunk(name: string, code: string): Chunk {
  const ast = parse(code, {
    sourceType: 'module',
    plugins: ['jsx'],
    errorRecovery: true,
  });
  let program: NodePath<t.Program> | undefined;
  traverse(ast, {
    Program(path) {
      program = path;
      path.stop();
    },
  });
  if (!program) throw new Error(`${name}: no program`);

  const exports = new Map<string, string>();
  const reexports = new Map<string, { chunk: string; name: string }>();
  for (const stmt of ast.program.body) {
    if (!t.isExportNamedDeclaration(stmt)) continue;
    for (const spec of stmt.specifiers) {
      if (!t.isExportSpecifier(spec)) continue;
      const exported = moduleName(spec.exported);
      const local = moduleName(spec.local);
      if (stmt.source) reexports.set(exported, { chunk: chunkNameOf(stmt.source.value), name: local });
      else exports.set(exported, local);
    }
    const decl = stmt.declaration;
    if (t.isVariableDeclaration(decl)) {
      for (const d of decl.declarations) if (t.isIdentifier(d.id)) exports.set(d.id.name, d.id.name);
    } else if ((t.isFunctionDeclaration(decl) || t.isClassDeclaration(decl)) && decl.id) {
      exports.set(decl.id.name, decl.id.name);
    }
  }
  return { name, code, program, exports, reexports };
}
