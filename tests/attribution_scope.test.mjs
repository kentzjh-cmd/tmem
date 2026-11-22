import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { ParsedSession } from "../tmem/adapters/base.mjs";
import { ActionKind, ActionNode } from "../tmem/models.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";
import { TracePlaybook } from "../tmem/models.mjs";
import { handleDshHook } from "../tmem/hooks/dsh.mjs";
import { DshAdapter } from "../tmem/adapters/dsh.mjs";
import { computeTelemetrySummary, loadTelemetryEvents } from "../tmem/telemetry.mjs";

/**
 * Regression guards for two defects found by the independent E2E study
 * (see docs/dsh-independent-e2e-report.md, N2 / N3).
 *
 * N3 - cross-session attribution leak: when a captured span carries no
 *      recognized change action, the slicer used to fall back to "every file
 *      dirty in the worktree". Those uncommitted files may belong to an
 *      earlier session that never committed, so one session's trace claimed
 *      another session's edits.
 *
 * N2 - injection telemetry blind spot: an anchor-driven memory injection
 *      (pre_step / pre_invocation) delivered the memory block but never
 *      recorded an `l2_injected` telemetry event, so the telemetry reported
 *      zero injections while memory was actually delivered.
 */

function gitInitRepo(ws) {
  execFileSync("git", ["init"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "t@t.com"], { cwd: ws });
  execFileSync("git", ["config", "user.name", "t"], { cwd: ws });
}

function gitCommitAll(ws, message) {
  execFileSync("git", ["add", "-A"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["commit", "-m", message], { cwd: ws, stdio: "ignore" });
}

function makeRepo(t) {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_scope_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  gitInitRepo(ws);
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "a.py"), "def a():\n    return 1\n", "utf-8");
  writeFileSync(path.join(ws, "src", "b.py"), "def b():\n    return 2\n", "utf-8");
  gitCommitAll(ws, "init");
  return ws;
}

/**
 * A span that produces a trace but carries no recognized change action:
 * it reads one file and runs a successful verification command.
 */
function spanWithoutChangeAction(target) {
  return [
    new ActionNode({
      step_idx: 1,
      kind: ActionKind.READ,
      tool_name: "read",
      target_file: target,
    }),
    new ActionNode({
      step_idx: 2,
      kind: ActionKind.ENV_EXEC,
      tool_name: "bash",
      command: "python3 -m pytest -q",
      exit_code: 0,
    }),
  ];
}

function sliceWith(ws, actions, intent) {
  const slicer = new BackwardTraceSlicer();
  return slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "sess_scope",
      workspace_root: ws,
      user_intents: [intent],
      actions,
    }),
    true
  );
}

test("N3: a span without change actions must not claim another session's uncommitted files", (t) => {
  const ws = makeRepo(t);

  // An earlier session edited src/a.py and never committed it.
  writeFileSync(path.join(ws, "src", "a.py"), "def a():\n    return 99\n", "utf-8");
  // This session worked on src/b.py and also left it uncommitted.
  writeFileSync(path.join(ws, "src", "b.py"), "def b():\n    return 42\n", "utf-8");

  const pb = sliceWith(
    ws,
    spanWithoutChangeAction("src/b.py"),
    "修复 src/b.py 的取整逻辑"
  );

  assert.ok(pb, "the span must still produce a trace");
  const touched = pb.touched_files || [];
  assert.ok(
    !touched.includes("src/a.py"),
    `src/a.py belongs to an earlier uncommitted session and must not be attributed here (got ${JSON.stringify(
      touched
    )})`
  );
  assert.deepEqual(
    touched,
    ["src/b.py"],
    "attribution must stay inside the scope of this span"
  );
});

test("N3: a committed session keeps its commit-scoped attribution", (t) => {
  const ws = makeRepo(t);

  // The session committed its work, so the worktree is clean.
  writeFileSync(path.join(ws, "src", "a.py"), "def a():\n    return 7\n", "utf-8");
  gitCommitAll(ws, "session change");

  const pb = sliceWith(
    ws,
    spanWithoutChangeAction("src/a.py"),
    "排查 src/a.py 返回值的变化"
  );

  assert.ok(pb, "the span must still produce a trace");
  assert.ok(
    (pb.touched_files || []).includes("src/a.py"),
    `a committed session must keep its commit-scoped attribution (got ${JSON.stringify(
      pb.touched_files
    )})`
  );
});

test("N2: an anchor-driven injection records an l2_injected telemetry event", (t) => {
  const ws = makeRepo(t);

  const store = new ProjectMemoryStore(ws);
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_anchor_scope",
      agent_source: "dsh",
      scope: "project",
      created_at: "2026-11-22T00:00:00Z",
      problem_intent: "Rounding helper in src/b.py must use ROUND_HALF_UP",
      semantic_summary: "Amount rounding uses ROUND_HALF_UP",
      error_signatures: ["ValueError"],
      touched_files: ["src/b.py"],
      touched_symbols: ["b"],
      confidence: 0.8,
    }),
    { mergeSimilar: false }
  );

  const res = handleDshHook("pre_invocation", {
    workspaceRoot: ws,
    sessionId: "sess_anchor",
    userMessage: "Please fix the rounding behaviour in src/b.py",
  });

  const injected = String(res.injectedContext || "");
  assert.ok(
    injected.includes("<tmem_speculative_memory>"),
    "the anchor must still trigger an injection"
  );

  const tel = computeTelemetrySummary(ws);
  assert.equal(
    tel.l2_injections,
    1,
    "a delivered memory block must be visible in telemetry"
  );
  assert.equal(tel.l2_injection_sources.anchor_recall, 1);
});

test("N4: transcript discovery prefers the workspace's own sessions", (t) => {
  const dshHome = mkdtempSync(path.join(os.tmpdir(), "tmem_dsh_home_"));
  t.after(() => rmSync(dshHome, { recursive: true, force: true }));

  const wsA = path.join(dshHome, "..", "tmem_ws_a");
  const wsB = path.join(dshHome, "..", "tmem_ws_b");
  for (const ws of [wsA, wsB]) {
    mkdirSync(ws, { recursive: true });
    const enc = "--" + ws.replace(/^\/+|\/+$/g, "").replace(/[\\/]/g, "-") + "--";
    const sessDir = path.join(dshHome, "sessions", enc, "session-11111111-1111-1111-1111-111111111111");
    mkdirSync(sessDir, { recursive: true });
    writeFileSync(path.join(sessDir, "session.v4.jsonl"), '{"type":"user/message"}\n', "utf-8");
  }
  // wsB's transcript is the newer one on disk.
  const newer = path.join(
    dshHome,
    "sessions",
    "--" + wsB.replace(/^\/+|\/+$/g, "").replace(/[\\/]/g, "-") + "--",
    "session-11111111-1111-1111-1111-111111111111",
    "session.v4.jsonl"
  );
  const future = new Date(Date.now() + 60_000);
  execFileSync("touch", ["-d", future.toISOString(), newer]);

  const adapter = new DshAdapter();
  const previousHome = process.env.DSH_HOME;
  process.env.DSH_HOME = dshHome;
  t.after(() => {
    if (previousHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previousHome;
  });

  const found = adapter.discoverLatestTranscript(wsA);
  assert.ok(
    String(found).includes("--" + wsA.replace(/^\/+|\/+$/g, "").replace(/[\\/]/g, "-") + "--"),
    `discovery must stay inside the workspace's own sessions (got ${found})`
  );
});

test("N2: the recorded signature is the anchor that triggered the injection", (t) => {
  const ws = makeRepo(t);

  const store = new ProjectMemoryStore(ws);
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_sig_scope",
      agent_source: "dsh",
      scope: "project",
      created_at: "2026-11-22T00:00:00Z",
      problem_intent: "KeyError was also seen earlier in this project",
      error_signatures: ["KeyError", "ValueError"],
      touched_files: ["src/b.py"],
      confidence: 0.8,
    }),
    { mergeSimilar: false }
  );

  handleDshHook("pre_invocation", {
    workspaceRoot: ws,
    sessionId: "sess_sig",
    userMessage: "修一下 ValueError: cannot convert NaN to integer 这个报错",
  });

  const tel = computeTelemetrySummary(ws);
  assert.equal(tel.l2_injections, 1, "the injection must be recorded");
  const events = loadTelemetryEvents(ws);
  const injected = events.find((e) => e.event === "l2_injected");
  assert.ok(injected, "an l2_injected event must exist");
  assert.equal(
    String(injected.error_signature || "").toLowerCase().includes("valueerror"),
    true,
    `the recorded signature must be the triggering anchor, not the playbook's first signature (got ${injected.error_signature})`
  );

  // Subsequent passing verification resolves the pending L2 hint from anchor_recall
  handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "sess_sig",
    toolCall: { name: "bash", args: { command: "python3 -m pytest -q" } },
    resultText: "1 passed\n[exit code: 0]",
    isError: false,
    exitCode: 0,
  });
  const telAfter = computeTelemetrySummary(ws);
  assert.equal(
    telAfter.l2_resolved_green,
    1,
    "passing verification after anchor_recall error injection must resolve the pending L2 hint"
  );
});
