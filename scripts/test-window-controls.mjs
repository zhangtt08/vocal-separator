import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import Module from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToString } from 'react-dom/server';

const source = path.resolve(import.meta.dirname, '../src/components/TitleBar.tsx');
const compiled = ts.transpileModule(fs.readFileSync(source, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const target = new Module(source);
target.filename = source;
target.paths = Module._nodeModulePaths(path.dirname(source));
target._compile(compiled, source);
const before = global.window;
try {
  delete global.window;
  const server = renderToString(React.createElement(target.exports.WindowControls));
  global.window = { vocal: { windowControls: {} } };
  assert.equal(renderToString(React.createElement(target.exports.WindowControls)), server);
  assert.equal(server, '');
  console.log('PASS window controls preserve the server hydration snapshot');
} finally {
  if (before === undefined) delete global.window;
  else global.window = before;
}
