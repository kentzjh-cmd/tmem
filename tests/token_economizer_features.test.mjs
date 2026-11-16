import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { ParsedSession, normalizeRelPath } from "../tmem/adapters/base.mjs";
import { parseApplyPatchDetails, parseApplyPatchTargets } from "../tmem/adapters/dsh.mjs";
import { SweBenchAdapter } from "../tmem/adapters/swe_bench.mjs";
import { executeCapture } from "../tmem/cli.mjs";
import { evaluateGuards } from "../tmem/guards.mjs";
import { defaultRuntimeState, handleDshHook, loadRuntimeState, saveRuntimeState } from "../tmem/hooks/index.mjs";
import { extractMicroSkills, formatMicroSkillsBlock, normalizeCommandTemplate } from "../tmem/mem_doc.mjs";
import { ActionKind, ActionNode, CausalRepairPair, GuardProbe, TracePlaybook } from "../tmem/models.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { setConnectionTrackingHooks } from "../tmem/sqlite_compat.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

function makeSamplePlaybook(traceId, targetFile = "app.py", verifyCmd = "pytest -q", confidence = 0.5) {
  return new TracePlaybook({
    trace_id: traceId,
    agent_source: "dsh",
    problem_intent: `Fix issue in ${targetFile}`,
    error_signatures: ["ValueError: bad input"],
    touched_files: [targetFile],
    touched_symbols: ["process"],
    guards: [new GuardProbe({ probe_type: "file_exists", target: targetFile })],
    l0_critical_path: [{ action: "MUTATE", target: targetFile }],
    l1_cochange_topology: [`Co-change: ${targetFile}`],
    l1_verification_cmds: [verifyCmd],
    confidence,
  });
}

test("failure-loop circuit breaker triggers once and resets on pass", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_loop_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  writeFileSync(path.join(ws, "app.py"), "def process():\n    return 1\n");

  let lastRes = {};
  for (let i = 0; i < 3; i++) {
    lastRes = handleDshHook("post_tool_use", {
      workspaceRoot: ws,
      sessionId: "conv-loop",
      toolCall: { name: "bash", args: { command: "pytest -q" } },
      resultText: "ValueError: bad input\n\n[exit code: 1]",
      isError: true,
    });
  }
  assert.ok((lastRes.additionalContext || "").includes("<tmem_loop_breaker>"));
  assert.ok((lastRes.additionalContext || "").includes("ValueError: bad input"));

  const res4 = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "conv-loop",
    toolCall: { name: "bash", args: { command: "pytest -q" } },
    resultText: "ValueError: bad input\n\n[exit code: 1]",
    isError: true,
  });
  assert.ok(!(res4.additionalContext || "").includes("<tmem_loop_breaker>"));
});

test("outcome-driven metabolism updates confidence and degrades L0 -> L1", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_meta_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  writeFileSync(path.join(ws, "app.py"), "def process():\n    return 1\n");

  const store = new ProjectMemoryStore(ws);
  store.savePlaybook(makeSamplePlaybook("trace-001", "app.py", "pytest -q", 0.5));

  const up1 = store.recordTraceOutcome(["trace-001"], "success");
  assert.ok(Math.abs(up1["trace-001"] - 0.6) < 1e-6);

  store.recordTraceOutcome(["trace-001"], "contradict");
  const up2 = store.recordTraceOutcome(["trace-001"], "contradict");
  assert.ok(Math.abs(up2["trace-001"] - 0.2) < 1e-6);

  const reloaded = store.getAllProjectPlaybooks().find((p) => p.trace_id === "trace-001");
  assert.equal(reloaded.status, "stale");
  const ev = evaluateGuards(reloaded, ws);
  assert.equal(ev.l0_valid, false);
  assert.equal(ev.effective_tier, "L1_TOPOLOGY");
});

test("zero-LLM micro-skills extraction and rendering", () => {
  const pb1 = makeSamplePlaybook("t1", "app.py", "PYTHONPATH=. pytest -q 2>&1 | tail -n 20");
  const pb2 = makeSamplePlaybook("t2", "app.py", "PYTHONPATH=. pytest -q");
  const pb3 = makeSamplePlaybook("t3", "app.py", "python3 only_once.py");
  const rep = new CausalRepairPair({
    error_signature: "ModuleNotFoundError: foo",
    failed_command: "pytest",
    repair_action: "pip install foo",
    verification_cmd: "pytest -q",
    is_env_or_toolchain: true,
  });
  pb1.l2_causal_repairs.push(rep);
  pb2.l2_causal_repairs.push(rep);

  assert.equal(
    normalizeCommandTemplate("PYTHONPATH=. pytest -q 2>&1 | tail -n 20"),
    "PYTHONPATH=. pytest -q"
  );
  const skills = extractMicroSkills([pb1, pb2, pb3], { minTraces: 2 });
  assert.ok(skills.includes("PYTHONPATH=. pytest -q"));
  assert.ok(skills.includes("pip install foo"));
  assert.ok(!skills.includes("python3 only_once.py"));
  assert.ok(formatMicroSkillsBlock(skills).includes("<tmem_env_facts>"));
});

test("destroy barrier, persistent env install, dangling read pruning, and apply_patch variants", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_phase1_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  const patch =
    "*** Begin Patch\n" +
    "*** Update File: src/app.py\n" +
    "*** 1,3 ****\n" +
    "--- 1,3 ----\n" +
    "+def added():\n" +
    "+    return 1\n" +
    "*** End Patch\n";
  assert.deepEqual(
    parseApplyPatchTargets(patch).map((x) => x[0]),
    ["src/app.py"]
  );

  // SWE-bench syntax error & file viewer fix
  const trajPath = path.join(ws, "sample.traj");
  writeFileSync(
    trajPath,
    JSON.stringify({
      info: {},
      trajectory: [
        { action: "open src/qdp.py\n", observation: "[File: /repo/src/qdp.py (10 lines total)]\n1: raise ValueError('ERROR: bad')\n" },
        { action: "edit 2:2\nbad indent\nend_of_edit\n", observation: "Your proposed edit has introduced new syntax error(s).\n\nERRORS:\n- E999 SyntaxError\n" },
        { action: "edit 2:2\n    return 42\nend_of_edit\n", observation: "[File: /repo/src/qdp.py (11 lines total)]\n2:     return 42\n" },
        { action: "pytest tests/test_qdp.py\n", observation: "1 passed in 0.10s", exit_code: 0 },
      ],
    })
  );
  const sweSess = new SweBenchAdapter().parseTranscript(trajPath, ws);
  assert.equal(sweSess.actions[0].exit_code, 0);
  assert.equal(sweSess.actions[1].exit_code, 1);
  assert.equal(sweSess.actions[2].exit_code, 0);
  const slicer = new BackwardTraceSlicer();
  assert.ok(slicer.sliceSession(sweSess, false));
  assert.ok(!slicer.last_sliced_step_indices.includes(2));
  assert.ok(slicer.last_sliced_step_indices.includes(3));

  // Destroy barrier
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  writeFileSync(path.join(ws, "src", "billing.py"), "def bill():\n    return 1\n");
  const barrierPb = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "s-barrier",
      workspace_root: ws,
      user_intents: ["Fix billing module"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.READ, tool_name: "read_file", target_file: "src/reverted.py" }),
        new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/reverted.py" }),
        new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "git checkout -- src/reverted.py", exit_code: 0 }),
        new ActionNode({ step_idx: 4, kind: ActionKind.MUTATE, tool_name: "write_file", target_file: "tests/scratch_debug.py" }),
        new ActionNode({ step_idx: 5, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "python3 tests/scratch_debug.py", exit_code: 0 }),
        new ActionNode({ step_idx: 6, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "rm -f tests/scratch_debug.py", exit_code: 0 }),
        new ActionNode({ step_idx: 7, kind: ActionKind.READ, tool_name: "read_file", target_file: "src/billing.py" }),
        new ActionNode({ step_idx: 8, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/billing.py", symbols_touched: ["bill"] }),
        new ActionNode({ step_idx: 9, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "PYTHONPATH=. python3 -m unittest discover -s tests -v", exit_code: 0 }),
      ],
    }),
    false
  );
  assert.deepEqual(barrierPb.touched_files, ["src/billing.py"]);
  assert.deepEqual(slicer.last_sliced_step_indices, [7, 8, 9]);

  // Persistent env install & dangling post-read pruning
  writeFileSync(path.join(ws, "src", "limiter.py"), "def limit():\n    return True\n");
  slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "s-install",
      workspace_root: ws,
      user_intents: ["Add redis rate limiter"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pip install redis pytest", exit_code: 0 }),
        new ActionNode({ step_idx: 2, kind: ActionKind.READ, tool_name: "read_file", target_file: "src/limiter.py" }),
        new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_limiter.py", exit_code: 0 }),
        new ActionNode({ step_idx: 4, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/limiter.py", symbols_touched: ["limit"] }),
        new ActionNode({ step_idx: 5, kind: ActionKind.READ, tool_name: "read_file", target_file: "src/limiter.py" }),
        new ActionNode({ step_idx: 6, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_limiter.py", exit_code: 0 }),
        new ActionNode({ step_idx: 7, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "ruff check src/ || true", exit_code: 0 }),
      ],
    }),
    false
  );
  assert.deepEqual(slicer.last_sliced_step_indices, [1, 2, 4, 6]);

  assert.equal(normalizeRelPath("./src/limiter.py", ws), "src/limiter.py");
  assert.equal(
    slicer._extractCanonicalVerificationCmd("which pytest && pytest tests/test_a.py"),
    "pytest tests/test_a.py"
  );
  assert.equal(slicer._extractCanonicalVerificationCmd("which pytest"), null);

  // Atomic state saves leave no .tmp leftovers
  for (let i = 0; i < 8; i++) {
    const st = defaultRuntimeState(`conc_sess_${i}`);
    st.recent_files = [`src/file_${i}.py`];
    saveRuntimeState(ws, st);
  }
  const leftovers = readdirSync(path.join(ws, ".tmem")).filter((f) =>
    f.startsWith(".runtime_state.") && f.endsWith(".tmp")
  );
  assert.deepEqual(leftovers, []);
  for (let i = 0; i < 8; i++) {
    assert.deepEqual(loadRuntimeState(ws, `conc_sess_${i}`).recent_files, [`src/file_${i}.py`]);
  }
});

test("SQLite connections are strictly closed on all store operations", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_sqlite_close_"));
  t.after(() => {
    setConnectionTrackingHooks(null, null);
    rmSync(ws, { recursive: true, force: true });
  });

  let opened = 0;
  let closed = 0;
  setConnectionTrackingHooks(
    () => {
      opened += 1;
    },
    () => {
      closed += 1;
    }
  );

  writeFileSync(path.join(ws, "app.py"), "def run():\n    return 1\n");
  const pb = makeSamplePlaybook("tr_close_test", "app.py");
  pb.problem_intent = "Fix KeyError in app";
  pb.error_signatures = ["KeyError: 'cfg'"];
  pb.l2_causal_repairs = [
    new CausalRepairPair({
      error_signature: "KeyError: 'cfg'",
      failed_command: "pytest -q",
      repair_action: "edit app.py (run)",
      verification_cmd: "pytest -q",
    }),
  ];
  pb.touched_symbols = ["run"];

  const st = new ProjectMemoryStore(ws);
  st.savePlaybook(pb, { mergeSimilar: false });
  st.cascadingLookup({ queryText: "KeyError in app", filePaths: ["app.py"] });
  st.lookupL2Repair({ errorSignatures: ["KeyError: 'cfg'"], filePaths: ["app.py"] });
  st.recordTraceOutcome(["tr_close_test"], "success");
  st.getLinkedTraces("run");
  st.runMetabolismGc();
  st.deletePlaybook("tr_close_test");

  assert.ok(opened > 0);
  assert.equal(opened, closed, `Leaked SQLite connections: opened=${opened}, closed=${closed}`);
});

test("unified diff apply_patch preserves L2 and symbols in capture and hooks", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_ud_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  writeFileSync(path.join(ws, "src", "parser.py"), 'def parse(text):\n    if text is None:\n        return ""\n    return text.strip()\n');
  writeFileSync(path.join(ws, "tests", "test_parser.py"), "def test_p(): assert True\n");

  const parsedUd = parseApplyPatchDetails(
    "diff --git a/src/parser.py b/src/parser.py\n" +
      "--- a/src/parser.py\n" +
      "+++ b/src/parser.py\n" +
      "@@ -1,2 +1,4 @@ def parse(text):\n" +
      " def parse(text):\n" +
      "-    return text.strip()\n" +
      "+    if text is None:\n" +
      '+        return ""\n' +
      "+    return text.strip()\n"
  );
  assert.equal(parsedUd.length, 1);
  assert.equal(parsedUd[0][0], "src/parser.py");
  assert.deepEqual(parsedUd[0][2], [[1, 4]]);

  const sessPath = path.join(ws, "ud.v4.jsonl");
  const events = [
    { version: 4, id: "sess_ud", createdAt: 1731225600000 },
    { type: "user/message", seq: 1, data: { role: "user", content: [{ type: "text", text: "Fix AttributeError in src/parser.py" }], source: { kind: "user" } } },
    { type: "tool/call", seq: 2, data: { callId: "c1", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_parser.py" }) } },
    { type: "tool/result", seq: 3, data: { message: { callId: "c1", content: [{ type: "text", text: 'File "src/parser.py", line 2, in parse\nAttributeError: NoneType object has no attribute strip\n[exit code: 1]' }], isError: false } } },
    { type: "tool/call", seq: 4, data: { callId: "c2", name: "apply_patch", arguments: JSON.stringify({ patch: "diff --git a/src/parser.py b/src/parser.py\n--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1,2 +1,4 @@ def parse(text):\n+    if text is None:\n+        return \"\"\n" }) } },
    { type: "tool/result", seq: 5, data: { message: { callId: "c2", content: [{ type: "text", text: "OK" }], isError: false } } },
    { type: "tool/call", seq: 6, data: { callId: "c3", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_parser.py" }) } },
    { type: "tool/result", seq: 7, data: { message: { callId: "c3", content: [{ type: "text", text: "1 passed\n[exit code: 0]" }], isError: false } } },
  ];
  writeFileSync(sessPath, events.map((e) => JSON.stringify(e)).join("\n"));

  const cap = executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: sessPath, sessionId: "sess_ud" });
  assert.equal(cap.status, "compiled");
});
