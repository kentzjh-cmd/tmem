import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanRationale, ParsedSession } from "../tmem/adapters/base.mjs";
import { DshAdapter } from "../tmem/adapters/dsh.mjs";
import { evaluateGuards, formatInjectedContext } from "../tmem/guards.mjs";
import { installInvisibleIntegration } from "../tmem/installer.mjs";
import { ActionKind, ActionNode, GuardProbe, TracePlaybook } from "../tmem/models.mjs";
import { BackwardTraceSlicer, extractCompactDiffSummary } from "../tmem/slicer.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");

test("cleanRationale scrubs secrets and truncates cleanly", () => {
  const raw = "   ### Verify auth token ghp_secret1234567890abcdef\nwith retry logic   ";
  const cleaned = cleanRationale(raw, 40);
  assert.ok(cleaned);
  assert.ok(!cleaned.includes("ghp_"));
  assert.ok(cleaned.includes("[REDACTED_SECRET]"));
  assert.ok(cleaned.length <= 40);
  assert.equal(cleanRationale(""), null);
  assert.equal(cleanRationale("   \n\t  "), null);
});

test("extractCompactDiffSummary isolates target file lines", () => {
  const patch =
    "diff --git a/tmem/guards.mjs b/tmem/guards.mjs\n" +
    "--- a/tmem/guards.mjs\n" +
    "+++ b/tmem/guards.mjs\n" +
    "@@ -25,3 +25,5 @@\n" +
    "+function evaluateGuards() {\n" +
    "+    // Added body hash check\n" +
    "+    return true;\n" +
    "diff --git a/tmem/other.mjs b/tmem/other.mjs\n" +
    "--- a/tmem/other.mjs\n" +
    "+++ b/tmem/other.mjs\n" +
    "@@ -1,2 +1,2 @@\n" +
    "+function other() {}\n";
  const summary = extractCompactDiffSummary(patch, "tmem/guards.mjs");
  assert.ok(summary);
  assert.ok(summary.startsWith("+ "));
  assert.ok(summary.includes("function evaluateGuards()"));
  assert.ok(summary.includes("return true;"));
  assert.ok(!summary.includes("other()"));
});

test("schema v1/v2 backward compatibility strips obsolete bloat fields", () => {
  const v2Data = {
    trace_id: "tr_legacy_001",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-10T10:00:00Z",
    schema_version: 2,
    problem_intent: "Legacy intent",
    semantic_summary: "Legacy summary",
    error_signatures: ["SomeError"],
    touched_files: ["src/legacy.py"],
    touched_symbols: ["legacy_func"],
    guards: [{ probe_type: "file_exists", target: "src/legacy.py", expected: "" }],
    l0_critical_path: [{ action: "MUTATE", target: "src/legacy.py" }],
    l1_cochange_topology: ["Co-edited in session: src/legacy.py <-> tests/test_legacy.py"],
    l1_verification_cmds: ["pytest"],
    l2_causal_repairs: [],
    requirements: [{ req_id: "R1", text: "legacy req", source: "user" }],
    intent_edges: [{ src: "R1", dst: "s1", relation: "served_by", why: "legacy" }],
    confidence: 0.95,
    hit_count: 0,
    status: "active",
  };
  const pb = TracePlaybook.fromDict(v2Data);
  assert.equal(pb.trace_id, "tr_legacy_001");
  assert.equal(pb.schema_version, 2);
  assert.equal(pb.root_cause, null);
  assert.deepEqual(pb.pitfalls, []);
  const d = pb.toDict();
  assert.equal("requirements" in d, false);
  assert.equal("intent_edges" in d, false);
});

test("partial guard degradation retains surviving files and hides drifted files", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_partial_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "valid.py"), "def valid(): pass\n", "utf-8");

  const playbook = new TracePlaybook({
    trace_id: "tr_partial_001",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-11T12:00:00Z",
    problem_intent: "Fix dual file validation bug",
    guards: [
      new GuardProbe({ probe_type: "file_exists", target: "src/valid.py", expected: "" }),
      new GuardProbe({ probe_type: "file_exists", target: "src/deleted.py", expected: "" }),
    ],
    l0_critical_path: [
      { step: 1, action: "MUTATE", target: "src/valid.py", rationale: "Updated valid handler" },
      { step: 2, action: "MUTATE", target: "src/deleted.py", rationale: "Updated deleted handler" },
      { step: 3, action: "ENV_EXEC", command: "pytest", rationale: "Run regression test" },
    ],
    touched_files: ["src/valid.py", "src/deleted.py"],
    l1_cochange_topology: ["src/valid.py <-> src/deleted.py"],
    l1_verification_cmds: ["pytest"],
  });

  const ev = evaluateGuards(playbook, ws);
  assert.equal(ev.l0_valid, false);
  assert.equal(ev.effective_tier, "L1_TOPOLOGY");
  assert.ok(ev.failed_guards.includes("missing_file:src/deleted.py"));
  assert.ok(ev.passed_files.includes("src/valid.py"));

  const context = formatInjectedContext([ev]);
  assert.ok(context.includes("Partial L0 Path (Drifted files hidden)"));
  assert.ok(context.includes("MUTATE(src/valid.py)"));
  assert.ok(!context.includes("MUTATE(src/deleted.py)"));
});

test("FTS5 semantic indexing, trace_links, orphan cleanup, and GC DB sync", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_fts_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  const store = new ProjectMemoryStore(ws);

  const pb = new TracePlaybook({
    trace_id: "tr_graph_indexed",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-11T12:00:00Z",
    problem_intent: "Fix authentication session refresh token race condition",
    semantic_summary: "Fixed race condition by introducing mutex lock",
    root_cause: "Concurrent refresh requests invalidated rotating tokens",
    touched_files: ["src/auth_session.py"],
    touched_symbols: ["refresh_mutex_lock", "validate_token_rotation"],
    guards: [new GuardProbe({ probe_type: "file_exists", target: "src/auth_session.py" })],
    l0_critical_path: [
      {
        step: 1,
        action: "MUTATE",
        target: "src/auth_session.py",
        rationale: "Implemented atomic compare-and-swap token rotation lock",
      },
    ],
    l1_verification_cmds: ["pytest tests/test_auth_session.py"],
    confidence: 1.0,
  });

  store.savePlaybook(pb, { targetScope: "project" });

  const res1 = store.cascadingLookup({ queryText: "compare-and-swap" });
  assert.ok(res1.length >= 1);
  assert.equal(res1[0].playbook.trace_id, "tr_graph_indexed");

  const res2 = store.cascadingLookup({ queryText: "Concurrent refresh" });
  assert.ok(res2.length >= 1);
  assert.equal(res2[0].playbook.trace_id, "tr_graph_indexed");

  const linked = store.getLinkedTraces("refresh_mutex_lock", "tr_other");
  assert.ok(linked.includes("tr_graph_indexed"));

  // GC degrades stale & syncs SQLite
  const stats = store.runMetabolismGc();
  assert.equal(stats.stale, 1);
  const afterGc = store.cascadingLookup({ queryText: "Concurrent refresh" });
  assert.equal(afterGc.length, 1);
  assert.equal(afterGc[0].playbook.status, "stale");
  assert.ok(Math.abs(afterGc[0].playbook.confidence - 0.7) < 1e-6);

  // Deleting JSON directly purges orphan from SQLite
  unlinkSync(path.join(store.project_traces_dir, "tr_graph_indexed.json"));
  assert.deepEqual(store.cascadingLookup({ queryText: "Concurrent refresh" }), []);
});

test("AST symbol guard precision rejects comments and docstrings", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_ast_guard_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(
    path.join(ws, "src", "service.py"),
    '# Comment mentioning old_legacy_handler\n' +
      '"""Docstring mentioning deprecated_method."""\n' +
      "class WorkerService:\n" +
      "    def process_task(self):\n" +
      "        pass\n" +
      "def standalone_runner():\n" +
      "    pass\n",
    "utf-8"
  );

  const pbValid = new TracePlaybook({
    trace_id: "tr_ast_valid",
    agent_source: "dsh",
    guards: [
      new GuardProbe({ probe_type: "symbol_present", target: "src/service.py", expected: "WorkerService" }),
      new GuardProbe({ probe_type: "symbol_present", target: "src/service.py", expected: "WorkerService.process_task" }),
      new GuardProbe({ probe_type: "symbol_present", target: "src/service.py", expected: "standalone_runner" }),
    ],
    l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/service.py" }],
  });
  const evValid = evaluateGuards(pbValid, ws);
  assert.equal(evValid.l0_valid, true);
  assert.equal(evValid.effective_tier, "L0_FULL");

  for (const sym of ["old_legacy_handler", "deprecated_method"]) {
    const pbBad = new TracePlaybook({
      trace_id: `tr_bad_${sym}`,
      agent_source: "dsh",
      guards: [new GuardProbe({ probe_type: "symbol_present", target: "src/service.py", expected: sym })],
      l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/service.py" }],
    });
    const evBad = evaluateGuards(pbBad, ws);
    assert.equal(evBad.l0_valid, false);
    assert.ok(evBad.failed_guards.includes(`missing_symbol:src/service.py#${sym}`));
  }
});

test("portable init contracts and quoted post-commit hook with spaces in path", (t) => {
  const tempDir = mkdtempSync(path.join(os.tmpdir(), "tmem_init_portable_"));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const ws = path.join(tempDir, "my repo with spaces");
  mkdirSync(ws, { recursive: true });
  const dshHome = path.join(tempDir, "dsh_home");
  execFileSync("git", ["init"], { cwd: ws, stdio: "ignore" });

  const prevDshHome = process.env.DSH_HOME;
  process.env.DSH_HOME = dshHome;
  t.after(() => {
    if (prevDshHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = prevDshHome;
  });

  const info = installInvisibleIntegration(ws, ["dsh"], true);
  assert.equal(info.git_hook, true);
  assert.equal(existsSync(path.join(ws, ".agents")), false);
  assert.ok(existsSync(path.join(ws, ".dsh", "skills", "tmem", "SKILL.md")));

  const wsPatchPath = path.join(ws, ".dsh", "tmem.patch.yml");
  const wsPatch = readFileSync(wsPatchPath, "utf-8");
  const wsNameMatch = wsPatch.match(/^\s*name:\s*(.+)\s*$/m);
  assert.ok(wsNameMatch, "workspace patch should declare plugin name");
  const wsRawName = wsNameMatch[1].trim().replace(/^["']|["']$/g, "");
  const resolvedWsPlugin = path.resolve(path.dirname(wsPatchPath), wsRawName);
  assert.ok(existsSync(resolvedWsPlugin), `resolved patch target must exist: ${resolvedWsPlugin}`);
  assert.equal(resolvedWsPlugin, path.join(ws, ".dsh", "plugins", "tmem-dsh-plugin.mjs"));

  const homePatch = readFileSync(path.join(dshHome, "cordis.patch.yml"), "utf-8");
  const homeNameMatch = homePatch.match(/^\s*name:\s*(.+)\s*$/m);
  assert.ok(homeNameMatch, "home cordis.patch.yml should declare plugin name");
  const homeRawName = JSON.parse(homeNameMatch[1].trim());
  assert.ok(path.isAbsolute(homeRawName), `home patch target must be absolute: ${homeRawName}`);
  assert.ok(existsSync(homeRawName), `home patch target must exist: ${homeRawName}`);

  const symlinkBin = path.join(tempDir, "tmem-symlink");
  symlinkSync(path.join(REPO_ROOT, "tmem", "cli.mjs"), symlinkBin);
  const helpOut = execFileSync(process.execPath, [symlinkBin, "--help"], { encoding: "utf-8" });
  assert.ok(helpOut.includes("tmem"), "symlinked CLI entrypoint must execute main()");

  const postCommitPath = path.join(ws, ".git", "hooks", "post-commit");
  const postCommit = readFileSync(postCommitPath, "utf-8");
  assert.ok(postCommit.includes("TMEM_NODE="));
  assert.ok(postCommit.includes("TMEM_CLI="));
  assert.ok(postCommit.includes(path.join(REPO_ROOT, "tmem", "cli.mjs")));
  assert.ok(!postCommit.includes("|| true"));
  execFileSync("/bin/sh", ["-n", postCommitPath]);
});

test("repeated mutations merge symbols and L2 repair matches exact repair step", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_rep_mut_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  const session = new ParsedSession({
    agent_name: "dsh",
    session_id: "sess_multi_mut",
    workspace_root: ws,
    user_intents: ["Fix signature verification in src/auth.py"],
    actions: [
      new ActionNode({ step_idx: 1, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/other.py", symbols_touched: ["init_other"] }),
      new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/auth.py", symbols_touched: ["parse_header"] }),
      new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_auth.py", exit_code: 1, error_signature: "ValueError: invalid signature" }),
      new ActionNode({ step_idx: 4, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/auth.py", symbols_touched: ["verify_signature"], rationale: "Fix signature verification padding" }),
      new ActionNode({ step_idx: 5, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_auth.py", exit_code: 0 }),
    ],
  });

  const pb = new BackwardTraceSlicer().sliceSession(session, false);
  assert.ok(pb);
  const authMut = pb.l0_critical_path.find((s) => s.target === "src/auth.py");
  assert.deepEqual(authMut.symbols, ["parse_header", "verify_signature"]);
  assert.equal(authMut.rationale, "Fix signature verification padding");
  assert.equal(pb.l2_causal_repairs.length, 1);
  assert.ok(pb.l2_causal_repairs[0].repair_action.includes("src/auth.py"));
});
