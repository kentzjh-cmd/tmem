import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

/**
 * Regression guard for N12 (P1): the plugin copy that `tmem init` writes into
 * `<workspace>/.dsh/plugins/` is a *standalone* file — it has no sibling
 * `hooks/` and no `cli.mjs`, and Node's module resolution never reaches the
 * globally installed `dsh-tmem` package from that directory. Before the fix,
 * such a copy silently degraded to the CLI fallback: the hook kept working
 * (memory was still injected) but `tmem_search` / `tmem_expand` were never
 * registered and the tmem system-prompt section was missing — the agent saw
 * a half-mounted plugin.
 *
 * The fix resolves the installed package through the `tmem` CLI on PATH.
 */

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PLUGIN_SRC = path.join(REPO_ROOT, "tmem", "dsh_plugin.mjs");

function makeStandaloneCopy() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tmem-standalone-"));
  const pluginsDir = path.join(tmp, "workspace", ".dsh", "plugins");
  fs.mkdirSync(pluginsDir, { recursive: true });
  const copy = path.join(pluginsDir, "tmem-dsh-plugin.mjs");
  fs.copyFileSync(PLUGIN_SRC, copy);
  return { tmp, copy, pluginsDir };
}

/** Put a `tmem` executable on PATH that points at this repository. */
function withRepoCliOnPath(tmp) {
  const binDir = path.join(tmp, "bin");
  fs.mkdirSync(binDir, { recursive: true });
  fs.symlinkSync(path.join(REPO_ROOT, "tmem", "cli.mjs"), path.join(binDir, "tmem"));
  const previous = process.env.PATH;
  process.env.PATH = `${binDir}${path.delimiter}${previous}`;
  return () => {
    process.env.PATH = previous;
  };
}

test("N12: a standalone plugin copy resolves the installed package via the CLI on PATH", async () => {
  const { tmp, copy, pluginsDir } = makeStandaloneCopy();
  const restorePath = withRepoCliOnPath(tmp);
  try {
    // RED condition: the copy carries none of the package's own modules, so
    // the module-location lookup cannot resolve the runtime.
    assert.equal(fs.existsSync(path.join(pluginsDir, "hooks", "dsh.mjs")), false);
    assert.equal(fs.existsSync(path.join(pluginsDir, "cli.mjs")), false);

    const mod = await import(pathToFileURL(copy).href);
    assert.equal(typeof mod.resolveTmemRootFromCli, "function");
    const root = mod.resolveTmemRootFromCli();
    assert.ok(root, "the package root should be resolvable through the CLI on PATH");
    assert.equal(root, REPO_ROOT);
    assert.ok(fs.existsSync(path.join(root, "tmem", "hooks", "index.mjs")));
    assert.ok(fs.existsSync(path.join(root, "tmem", "cli.mjs")));

    // The runtime resolved from that root must expose the tool factory the
    // plugin needs in order to register `tmem_search` / `tmem_expand`.
    const hooks = await import(
      pathToFileURL(path.join(root, "tmem", "hooks", "index.mjs")).href
    );
    assert.equal(typeof hooks.createTmemTools, "function");
    assert.equal(typeof hooks.handleDshHook, "function");
  } finally {
    restorePath();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("N12: without any CLI on PATH the standalone copy reports no package root", async () => {
  const { tmp, copy } = makeStandaloneCopy();
  const previous = process.env.PATH;
  process.env.PATH = path.join(tmp, "empty-bin");
  try {
    fs.mkdirSync(path.join(tmp, "empty-bin"), { recursive: true });
    const mod = await import(pathToFileURL(copy).href);
    assert.equal(mod.resolveTmemRootFromCli(), null);
  } finally {
    process.env.PATH = previous;
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("the in-repo plugin still exposes the DSH entry points", async () => {
  const mod = await import(pathToFileURL(PLUGIN_SRC).href);
  assert.equal(mod.name, "tmem");
  assert.equal(typeof mod.apply, "function");
  assert.equal(typeof mod.resolveTmemRootFromCli, "function");
});

test("N12: a standalone copy registers the model-facing tools (tmem_search / tmem_expand)", async () => {
  const { tmp, copy } = makeStandaloneCopy();
  const restorePath = withRepoCliOnPath(tmp);
  try {
    const mod = await import(pathToFileURL(copy).href);
    const registered = [];
    let toolsCtx = null;
    const ctx = {
      inject(names, callback) {
        if (names.includes("tools")) {
          toolsCtx = { tools: { register: (def) => registered.push(def.name) } };
          callback(toolsCtx);
        }
      },
      on() {},
      get() {
        return null;
      },
    };
    mod.apply(ctx, { workspaceRoot: path.join(tmp, "workspace") });
    // The runtime is loaded asynchronously at mount time.
    for (let i = 0; i < 50 && registered.length === 0; i += 1) {
      await new Promise((r) => setTimeout(r, 20));
    }
    assert.ok(toolsCtx, "the plugin should request the tools service");
    assert.ok(
      registered.includes("tmem_search") && registered.includes("tmem_expand"),
      `expected tmem_search/tmem_expand to be registered, got: ${JSON.stringify(registered)}`
    );
  } finally {
    restorePath();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
