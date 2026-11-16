import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { ParsedSession } from "../tmem/adapters/base.mjs";
import { executeCapture } from "../tmem/cli.mjs";
import { evaluateGuards, formatInjectedContext } from "../tmem/guards.mjs";
import { handleDshHook } from "../tmem/hooks/index.mjs";
import { renderMemoriesMarkdown } from "../tmem/mem_doc.mjs";
import {
  ActionKind,
  ActionNode,
  CausalRepairPair,
  GuardProbe,
  RawExecutionTrace,
  TracePlaybook,
} from "../tmem/models.mjs";
import { formatProgressiveView, formatRawTraceReplay } from "../tmem/raw_storage.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";
import { computeTelemetrySummary } from "../tmem/telemetry.mjs";

test("pre-write similarity merging avoids duplicate playbooks", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_merge_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  const store = new ProjectMemoryStore(ws);

  const pb1 = new TracePlaybook({
    trace_id: "tr_first_001",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-13T10:00:00Z",
    problem_intent: "Fix ZeroDivisionError in billing tax calculator",
    error_signatures: ["ZeroDivisionError: division by zero"],
    touched_files: ["src/billing.py", "tests/test_billing.py"],
    touched_symbols: ["calculate_unit_tax"],
    guards: [new GuardProbe({ probe_type: "file_exists", target: "src/billing.py" })],
    l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/billing.py" }],
    l2_causal_repairs: [
      new CausalRepairPair({
        error_signature: "ZeroDivisionError: division by zero",
        failed_command: "pytest tests/test_billing.py",
        repair_action: "edit src/billing.py (symbols: calculate_unit_tax)",
        verification_cmd: "pytest tests/test_billing.py",
        is_env_or_toolchain: false,
      }),
    ],
    confidence: 0.6,
  });

  const pb2 = new TracePlaybook({
    trace_id: "tr_second_002",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-13T11:00:00Z",
    problem_intent: "Fix ZeroDivisionError in billing tax calculator edge case",
    error_signatures: ["ZeroDivisionError: division by zero"],
    touched_files: ["src/billing.py", "tests/test_billing.py"],
    touched_symbols: ["calculate_unit_tax", "normalize_rate"],
    guards: [new GuardProbe({ probe_type: "file_exists", target: "src/billing.py" })],
    l0_critical_path: [
      { step: 1, action: "READ", target: "src/billing.py" },
      { step: 2, action: "MUTATE", target: "src/billing.py" },
    ],
    l2_causal_repairs: [
      new CausalRepairPair({
        error_signature: "ZeroDivisionError: division by zero",
        failed_command: "pytest tests/test_billing.py",
        repair_action: "edit src/billing.py (symbols: calculate_unit_tax)",
        verification_cmd: "pytest tests/test_billing.py",
        is_env_or_toolchain: false,
      }),
    ],
    confidence: 0.6,
  });

  const [id1] = store.savePlaybook(pb1);
  const [id2] = store.savePlaybook(pb2);
  assert.equal(id1, id2);
  const allPbs = store.getAllProjectPlaybooks();
  assert.equal(allPbs.length, 1);
  assert.equal(allPbs[0].merge_count, 1);
  assert.ok(Math.abs(allPbs[0].confidence - 0.68) < 1e-6);
  assert.ok(allPbs[0].touched_symbols.includes("normalize_rate"));

  const pbOther = new TracePlaybook({
    trace_id: "tr_other_003",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-13T11:30:00Z",
    problem_intent: "Add Redis cache layer for session store",
    touched_files: ["src/cache.py", "tests/test_cache.py"],
    confidence: 0.6,
  });
  const [id3] = store.savePlaybook(pbOther);
  assert.equal(id3, "tr_other_003");
  assert.equal(store.getAllProjectPlaybooks().length, 2);
});

test("guard drift streak and GC pruning keep L2 invariants", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_gc_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  const store = new ProjectMemoryStore(ws);

  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_drift_prune",
      agent_source: "dsh",
      problem_intent: "Edit removed file",
      touched_files: ["src/gone.py", "tests/test_gone.py"],
      guards: [new GuardProbe({ probe_type: "file_exists", target: "src/gone.py" })],
      l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/gone.py" }],
      confidence: 0.7,
    })
  );
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_drift_keep_l2",
      agent_source: "dsh",
      problem_intent: "Fix missing package error",
      touched_files: ["src/gone.py"],
      guards: [new GuardProbe({ probe_type: "file_exists", target: "src/gone.py" })],
      l2_causal_repairs: [
        new CausalRepairPair({
          error_signature: "ModuleNotFoundError: No module named 'httpx'",
          failed_command: "pytest",
          repair_action: "pip install httpx",
          verification_cmd: "pytest",
          is_env_or_toolchain: true,
        }),
      ],
      confidence: 0.8,
    })
  );

  store.runMetabolismGc(false);
  store.runMetabolismGc(false);
  const stats3 = store.runMetabolismGc(true);
  assert.equal(stats3.pruned, 1);
  const remaining = store.getAllProjectPlaybooks().map((p) => p.trace_id);
  assert.ok(!remaining.includes("tr_drift_prune"));
  assert.ok(remaining.includes("tr_drift_keep_l2"));
});

test("pre_step silences vague NL and post_tool emits L1 & L2 telemetry", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_tel_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  execFileSync("git", ["init"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "t@t.com"], { cwd: ws });
  execFileSync("git", ["config", "user.name", "t"], { cwd: ws });
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "a.py"), "def a(): return 1\n");
  writeFileSync(path.join(ws, "src", "b.py"), "def b(): return 2\n");
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "init"], { cwd: ws, stdio: "ignore" });

  for (const rev of [1, 2]) {
    writeFileSync(path.join(ws, "src", "a.py"), `def a(): return ${rev}\n`);
    writeFileSync(path.join(ws, "src", "b.py"), `def b(): return ${rev}\n`);
    execFileSync("git", ["add", "."], { cwd: ws });
    execFileSync("git", ["commit", "-m", `pair ${rev}`], { cwd: ws, stdio: "ignore" });
  }

  const store = new ProjectMemoryStore(ws);
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_l2_tel",
      agent_source: "dsh",
      problem_intent: "Fix KeyError in src/a.py",
      error_signatures: ["KeyError: 'user_id'"],
      touched_files: ["src/a.py", "src/b.py"],
      guards: [new GuardProbe({ probe_type: "file_exists", target: "src/a.py" })],
      l2_causal_repairs: [
        new CausalRepairPair({
          error_signature: "KeyError: 'user_id'",
          failed_command: "pytest -q",
          repair_action: "edit src/a.py (symbols: a)",
          verification_cmd: "pytest -q",
          is_env_or_toolchain: false,
        }),
      ],
      confidence: 0.7,
    })
  );

  const preVague = handleDshHook("pre_invocation", {
    workspaceRoot: ws,
    sessionId: "s_vague",
    lastUserInput: "Can you inspect the architecture and explain error handling?",
  });
  assert.ok(!(preVague.injectedContext || "").includes("<tmem_speculative_memory>"));

  const editA = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "s_flow",
    toolCall: { name: "edit", args: { file_path: "src/a.py" } },
  });
  assert.ok((editA.additionalContext || "").includes("<tmem_cochange_hint>"));
  assert.ok((editA.additionalContext || "").includes("src/b.py"));

  handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "s_flow",
    toolCall: { name: "read", args: { file_path: "src/b.py" } },
  });

  const failRes = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "s_flow",
    toolCall: { name: "bash", args: { command: "pytest -q" } },
    resultText: "KeyError: 'user_id'\n[exit code: 1]",
  });
  assert.ok((failRes.additionalContext || "").includes("L2 Causal Repair"));

  handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "s_flow",
    toolCall: { name: "bash", args: { command: "pytest -q" } },
    resultText: "1 passed\n[exit code: 0]",
  });

  const tel = computeTelemetrySummary(ws);
  assert.equal(tel.l1_hints, 1);
  assert.equal(tel.l1_adopted, 1);
  assert.equal(tel.l2_injections, 1);
  assert.equal(tel.l2_resolved_green, 1);
});

test("zero-token behavior summary and inline session_id lineage", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_lineage_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  writeFileSync(path.join(ws, "src", "router.py"), "def dispatch(): return 1\n");
  writeFileSync(path.join(ws, "tests", "test_router.py"), "def test_dispatch(): assert True\n");

  const slicer = new BackwardTraceSlicer();
  const actions = [
    new ActionNode({ step_idx: 1, kind: ActionKind.READ, tool_name: "read", target_file: "src/router.py" }),
    new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit", target_file: "src/router.py", symbols_touched: ["dispatch"] }),
    new ActionNode({ step_idx: 3, kind: ActionKind.MUTATE, tool_name: "edit", target_file: "tests/test_router.py", symbols_touched: ["test_dispatch"] }),
    new ActionNode({ step_idx: 4, kind: ActionKind.ENV_EXEC, tool_name: "bash", command: "pytest -q", exit_code: 0 }),
  ];

  const pb1 = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "sess_hist_alpha",
      workspace_root: ws,
      user_intents: ["Fix route dispatch fallback in src/router.py"],
      actions,
    }),
    false
  );
  assert.ok(pb1);
  assert.deepEqual(pb1.source_session_ids, ["sess_hist_alpha"]);
  assert.ok(pb1.semantic_summary.includes("Behavior:"));
  assert.ok(pb1.semantic_summary.includes("edited 2 file(s)"));

  const store = new ProjectMemoryStore(ws);
  const [id1] = store.savePlaybook(pb1);

  const pb2 = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "sess_hist_beta",
      workspace_root: ws,
      user_intents: ["Fix route dispatch fallback in src/router.py edge case"],
      actions,
    }),
    false
  );
  const [id2] = store.savePlaybook(pb2);
  assert.equal(id1, id2);

  const mergedPb = store.getAllProjectPlaybooks()[0];
  assert.deepEqual(mergedPb.source_session_ids, ["sess_hist_alpha", "sess_hist_beta"]);

  const injected = formatInjectedContext([evaluateGuards(mergedPb, ws)]);
  assert.ok(injected.includes("Session(s): sess_hist_alpha, sess_hist_beta"));

  const md = renderMemoriesMarkdown([mergedPb], ws);
  assert.ok(md.includes("`sess_hist_alpha`"));
  assert.ok(md.includes("`sess_hist_beta`"));

  assert.ok(
    formatProgressiveView(mergedPb, null, "index").includes(
      "Session(s): sess_hist_alpha, sess_hist_beta"
    )
  );
  assert.ok(
    formatRawTraceReplay(
      new RawExecutionTrace({
        trace_id: mergedPb.trace_id,
        session_id: "sess_hist_beta",
        agent_source: "dsh",
        workspace_root: ws,
        created_at: "2026-11-13T12:00:00Z",
      })
    ).includes("Session: sess_hist_beta")
  );

  const inspBySess = handleDshHook("command", {
    workspaceRoot: ws,
    args: "inspect sess_hist_alpha index",
  });
  assert.equal(inspBySess.kind, "success");
  assert.ok(inspBySess.text.includes(mergedPb.trace_id));
});

test("concurrent session state isolation and strict L2 matching", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_iso_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "a.py"), "x = 1\n");
  writeFileSync(path.join(ws, "src", "b.py"), "y = 2\n");

  handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "sess_A",
    toolCall: { name: "edit", args: { file_path: "src/a.py" } },
  });
  handleDshHook("pre_invocation", {
    workspaceRoot: ws,
    sessionId: "sess_B",
    lastUserInput: "Check status of b.py",
  });
  const resA = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "sess_A",
    toolCall: { name: "bash", args: { command: "pytest -q" } },
    resultText: "1 passed\n[exit code: 0]",
  });
  assert.ok((resA.additionalContext || "").includes("<tmem_milestone_reached>"));
  assert.ok((resA.additionalContext || "").includes("src/a.py"));

  const store = new ProjectMemoryStore(ws);
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_yaml_only",
      agent_source: "dsh",
      problem_intent: "Fix missing yaml module",
      error_signatures: ["ModuleNotFoundError: No module named 'yaml'"],
      touched_files: ["src/a.py"],
      l2_causal_repairs: [
        new CausalRepairPair({
          error_signature: "ModuleNotFoundError: No module named 'yaml'",
          failed_command: "pytest -q",
          repair_action: "pip install pyyaml",
          verification_cmd: "pytest -q",
          is_env_or_toolchain: true,
        }),
      ],
      confidence: 0.8,
    })
  );
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_key_id",
      agent_source: "dsh",
      problem_intent: "Fix KeyError id",
      error_signatures: ["KeyError: 'id'"],
      touched_files: ["src/a.py"],
      l2_causal_repairs: [
        new CausalRepairPair({
          error_signature: "KeyError: 'id'",
          failed_command: "pytest -q",
          repair_action: "edit src/a.py (parse_id)",
          verification_cmd: "pytest -q",
          is_env_or_toolchain: false,
        }),
      ],
      confidence: 0.8,
    })
  );

  assert.deepEqual(
    store.lookupL2Repair({ errorSignatures: ["ModuleNotFoundError: No module named 'httpx'"] }),
    []
  );
  assert.deepEqual(store.lookupL2Repair({ errorSignatures: ["KeyError: 'id_token'"] }), []);
  assert.deepEqual(
    store.lookupL2Repair({ errorSignatures: ["ValueError: No module named 'yaml'"] }),
    []
  );
  const matched = store.lookupL2Repair({
    errorSignatures: ["ModuleNotFoundError: No module named 'yaml' in src/a.py"],
  });
  assert.equal(matched.length, 1);
  assert.equal(matched[0].playbook.trace_id, "tr_yaml_only");
});

test("merged capture binds raw trace without orphans and GC prunes exact raw trace", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_bind_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  writeFileSync(path.join(ws, "src", "calc.py"), "def div(a, b):\n    return a / b\n");
  writeFileSync(path.join(ws, "tests", "test_calc.py"), "def test_div(): assert True\n");

  const writeTranscript = (p, sessId, prompt, includeExtraRead) => {
    const events = [
      { version: 4, id: sessId, createdAt: 1731225600000 },
      {
        type: "user/message",
        seq: 1,
        data: { role: "user", content: [{ type: "text", text: prompt }], source: { kind: "user" } },
      },
    ];
    let seq = 2;
    if (includeExtraRead) {
      events.push(
        { type: "tool/call", seq, data: { callId: "r1", name: "read", arguments: JSON.stringify({ file_path: "src/calc.py" }) } },
        { type: "tool/result", seq: seq + 1, data: { message: { callId: "r1", content: [{ type: "text", text: "def div(a, b): return a / b" }], isError: false } } }
      );
      seq += 2;
    }
    events.push(
      { type: "tool/call", seq, data: { callId: "e1", name: "edit", arguments: JSON.stringify({ file_path: "src/calc.py", new_string: "def div(a, b):\n    return a / b if b else 0" }) } },
      { type: "tool/result", seq: seq + 1, data: { message: { callId: "e1", content: [{ type: "text", text: "Edited" }], isError: false } } },
      { type: "tool/call", seq: seq + 2, data: { callId: "e2", name: "edit", arguments: JSON.stringify({ file_path: "tests/test_calc.py", new_string: "def test_div(): assert True" }) } },
      { type: "tool/result", seq: seq + 3, data: { message: { callId: "e2", content: [{ type: "text", text: "Edited" }], isError: false } } },
      { type: "tool/call", seq: seq + 4, data: { callId: "b1", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_calc.py" }) } },
      { type: "tool/result", seq: seq + 5, data: { message: { callId: "b1", content: [{ type: "text", text: "1 passed\n[exit code: 0]" }], isError: false } } }
    );
    writeFileSync(p, events.map((ev) => JSON.stringify(ev)).join("\n"), "utf-8");
  };

  const t1 = path.join(ws, "s1.v4.jsonl");
  const t2 = path.join(ws, "s2.v4.jsonl");
  const t3 = path.join(ws, "s3.v4.jsonl");
  writeTranscript(t1, "sess_merge_1", "Fix ZeroDivisionError in src/calc.py", true);
  writeTranscript(t2, "sess_merge_2", "Fix ZeroDivisionError in src/calc.py edge case", false);
  writeTranscript(t3, "sess_merge_3", "Fix ZeroDivisionError in src/calc.py another case", true);

  const res1 = executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: t1, sessionId: "sess_merge_1" });
  assert.equal(res1.status, "compiled");
  const primaryId = res1.project_trace_id;

  const res2 = executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: t2, sessionId: "sess_merge_2" });
  assert.equal(res2.project_trace_id, primaryId);
  assert.deepEqual(readdirSync(path.join(ws, ".tmem", "raw")), [`${primaryId}.raw.json`]);

  const res3 = executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: t3, sessionId: "sess_merge_3" });
  assert.equal(res3.project_trace_id, primaryId);
  const store = new ProjectMemoryStore(ws);
  const loadedRaw = store.getRawTrace(primaryId);
  assert.equal(loadedRaw.session_id, "sess_merge_3");

  // GC deletes exact prefix target without harming neighbor
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_gc",
      agent_source: "dsh",
      problem_intent: "Removed file trace",
      touched_files: ["src/missing_only.py", "tests/test_missing_only.py"],
      guards: [new GuardProbe({ probe_type: "file_exists", target: "src/missing_only.py" })],
      l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/missing_only.py" }],
      confidence: 0.7,
    }),
    {
      mergeSimilar: false,
      rawTrace: new RawExecutionTrace({
        trace_id: "tr_gc",
        session_id: "s_gc",
        agent_source: "dsh",
        workspace_root: ws,
        created_at: "2026-11-16T00:00:00Z",
      }),
    }
  );
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_gc_keep",
      agent_source: "dsh",
      problem_intent: "Existing file neighbor sharing prefix",
      touched_files: ["src/calc.py", "tests/test_calc.py"],
      guards: [new GuardProbe({ probe_type: "file_exists", target: "src/calc.py" })],
      l0_critical_path: [{ step: 1, action: "MUTATE", target: "src/calc.py" }],
      confidence: 0.7,
    }),
    {
      mergeSimilar: false,
      rawTrace: new RawExecutionTrace({
        trace_id: "tr_gc_keep",
        session_id: "s_gc_keep",
        agent_source: "dsh",
        workspace_root: ws,
        created_at: "2026-11-16T00:00:00Z",
      }),
    }
  );
  for (let i = 0; i < 3; i++) store.runMetabolismGc(true);
  assert.equal(existsSync(path.join(ws, ".tmem", "traces", "tr_gc.json")), false);
  assert.equal(existsSync(path.join(ws, ".tmem", "raw", "tr_gc.raw.json")), false);
  assert.equal(existsSync(path.join(ws, ".tmem", "traces", "tr_gc_keep.json")), true);
  assert.equal(existsSync(path.join(ws, ".tmem", "raw", "tr_gc_keep.raw.json")), true);
});
