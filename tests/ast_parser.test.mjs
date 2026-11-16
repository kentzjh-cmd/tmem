import test from "node:test";
import assert from "node:assert/strict";
import { detectLanguage } from "../tmem/ast_parser.mjs";
import {
  extractLocalImports,
  extractSymbolsForLineRanges,
  extractSymbolsFromText,
} from "../tmem/adapters/base.mjs";
import { checkSymbolInAst } from "../tmem/guards.mjs";

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
