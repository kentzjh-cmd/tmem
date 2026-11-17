import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { detectLanguage, parseBraceStructure } from "../tmem/ast_parser.mjs";
import {
  extractLocalImports,
  extractSymbolsForLineRanges,
  extractSymbolsFromText,
} from "../tmem/adapters/base.mjs";
import { checkSymbolInAst } from "../tmem/guards.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("detectLanguage maps extensions accurately", () => {
  assert.equal(detectLanguage("src/app.py"), "python");
  assert.equal(detectLanguage("web/index.ts"), "typescript");
  assert.equal(detectLanguage("web/App.tsx"), "tsx");
  assert.equal(detectLanguage("pkg/main.go"), "go");
  assert.equal(detectLanguage("src/lib.rs"), "rust");
  assert.equal(detectLanguage("README.md"), null);
});

test("TS/JS structural parser extracts classes, methods, arrow fns, line ranges, and local imports", () => {
  const tsCode = [
    '// comment mentioning ghost_fn',
    'import { verify } from "../utils/auth";',
    "export class UserService {",
    "  login(user) {",
    "    return verify(user);",
    "  }",
    "  logout() {",
    "    return true;",
    "  }",
    "}",
    "export const handleSubmit = () => {",
    "  return 1;",
    "};",
  ].join("\n");

  // Line 5 is inside UserService.login()
  assert.deepEqual(
    extractSymbolsForLineRanges(tsCode, "src/services/user.ts", [[5, 5]]),
    ["UserService.login"]
  );
  assert.deepEqual(extractSymbolsFromText(tsCode, "src/services/user.ts"), [
    "UserService",
    "handleSubmit",
    "login",
    "logout",
  ]);
  assert.equal(
    checkSymbolInAst(tsCode, "src/services/user.ts", "UserService.login"),
    true
  );
  // Comment-only mention must NOT fool AST guard
  assert.equal(
    checkSymbolInAst(tsCode, "src/services/user.ts", "ghost_fn"),
    false
  );

  const imports = extractLocalImports(
    tsCode,
    "src/services/user.ts",
    new Set(["src/utils/auth.ts", "src/utils/other.ts"])
  );
  assert.deepEqual(Array.from(imports), ["src/utils/auth.ts"]);
});

test("Python structural parser rejects comments and docstrings", () => {
  const pyCode = [
    "# Comment mentioning fake_func",
    '"""Docstring mentioning fake_class"""',
    "class Worker:",
    "    def run_job(self):",
    "        # def inside_comment():",
    '        x = "def inside_string(): pass"',
    "        return 1",
  ].join("\n");

  assert.equal(checkSymbolInAst(pyCode, "src/worker.py", "Worker.run_job"), true);
  assert.equal(checkSymbolInAst(pyCode, "src/worker.py", "fake_func"), false);
  assert.equal(checkSymbolInAst(pyCode, "src/worker.py", "fake_class"), false);
  assert.equal(checkSymbolInAst(pyCode, "src/worker.py", "inside_comment"), false);
  assert.equal(checkSymbolInAst(pyCode, "src/worker.py", "inside_string"), false);
});

test("Python structural parser handles escaped quotes inside triple-quoted strings", () => {
  const pyCode = [
    'def real_before():',
    '    doc = """escaped triple quote \\"\\"\\" still inside docstring',
    'def fake_inside_triple():',
    '    pass',
    '"""',
    '    return 1',
    'def real_after():',
    '    return 2',
  ].join("\n");

  assert.equal(checkSymbolInAst(pyCode, "src/mod.py", "real_before"), true);
  assert.equal(checkSymbolInAst(pyCode, "src/mod.py", "real_after"), true);
  assert.equal(checkSymbolInAst(pyCode, "src/mod.py", "fake_inside_triple"), false);
});

test("JS/TS parser handles regex literals, multiline destructured params, single-expr arrows, and ignores inner calls", () => {
  const jsCode = [
    "export function withRegexAndCalls(a, b) {",
    '  const re1 = /Cannot find module\\s+[\'"][^\'"]+[\'"]/;',
    "  const re2 = /[\\s;{}()]/g;",
    "  const ratio = a / b / 2;",
    "  helperCall(a);",
    "  return ratio;",
    "}",
    "",
    "export function multiLineConfig(",
    "  first,",
    "  second,",
    "  {",
    "    host = 'localhost',",
    "    port = 8080,",
    "  } = {}",
    ") {",
    "  nestedCall(host, port);",
    "  return `${host}:${port}`;",
    "}",
    "",
    "export const addOne = (x) => x + 1;",
    "",
    "export class Engine {",
    "  run() {",
    "    return addOne(1);",
    "  }",
    "}",
  ].join("\n");

  const defs = parseBraceStructure(jsCode, "javascript");
  const byQualified = new Map(defs.map((d) => [d.qualified, d]));

  assert.ok(byQualified.has("withRegexAndCalls"));
  assert.equal(byQualified.get("withRegexAndCalls").startLine, 1);
  assert.equal(byQualified.get("withRegexAndCalls").endLine, 7);

  assert.ok(byQualified.has("multiLineConfig"));
  assert.equal(byQualified.get("multiLineConfig").startLine, 9);
  assert.equal(byQualified.get("multiLineConfig").endLine, 19);

  assert.ok(byQualified.has("addOne"));
  assert.equal(byQualified.get("addOne").startLine, 21);
  assert.equal(byQualified.get("addOne").endLine, 21);

  assert.ok(byQualified.has("Engine.run"));
  assert.equal(byQualified.has("withRegexAndCalls.helperCall"), false);
  assert.equal(byQualified.has("multiLineConfig.nestedCall"), false);

  assert.deepEqual(
    extractSymbolsForLineRanges(jsCode, "src/config.mjs", [[17, 17]]),
    ["multiLineConfig"]
  );
  assert.deepEqual(
    extractSymbolsForLineRanges(jsCode, "src/config.mjs", [[25, 25]]),
    ["Engine.run"]
  );
});

test("parseBraceStructure parses tmem/slicer.mjs and tmem/installer.mjs cleanly", () => {
  const slicerCode = readFileSync(path.join(REPO_ROOT, "tmem", "slicer.mjs"), "utf-8");
  const slicerDefs = parseBraceStructure(slicerCode, "javascript");
  const slicerMap = new Map(slicerDefs.map((d) => [d.qualified, d]));

  assert.ok(slicerMap.has("BackwardTraceSlicer"));
  assert.ok(slicerMap.has("BackwardTraceSlicer.sliceSession"));
  assert.ok(slicerMap.has("BackwardTraceSlicer._extractCausalRepairsWithIndices"));
  const repairMethod = slicerMap.get("BackwardTraceSlicer._extractCausalRepairsWithIndices");
  assert.ok(repairMethod.endLine > repairMethod.startLine + 50);

  const installerCode = readFileSync(path.join(REPO_ROOT, "tmem", "installer.mjs"), "utf-8");
  const installerDefs = parseBraceStructure(installerCode, "javascript");
  const installerMap = new Map(installerDefs.map((d) => [d.qualified, d]));

  assert.ok(installerMap.has("resolveDshEntryJs"));
  assert.ok(installerMap.has("installInvisibleIntegration"));
  const installFn = installerMap.get("installInvisibleIntegration");
  assert.ok(installFn.endLine > installFn.startLine + 50);
  assert.equal(installerMap.has("resolveDshEntryJs.existsSync"), false);
  assert.equal(installerMap.has("resolveDshEntryJs.statSync"), false);
});
