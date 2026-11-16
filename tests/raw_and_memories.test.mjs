import test from "node:test";
import assert from "node:assert/strict";
import { lstatSync, mkdirSync, mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { main as cliMain } from "../tmem/cli.mjs";
import { SLASH_TMEM_RE, handleDshHook } from "../tmem/hooks/index.mjs";
import { syncMemoriesFile } from "../tmem/mem_doc.mjs";
import {
  ActionKind,
  ActionNode,
  CausalRepairPair,
  GuardProbe,
  RawExecutionTrace,
  TracePlaybook,
} from "../tmem/models.mjs";
import {
  RawTraceStore,
  formatProgressiveView,
  formatRawTraceDiff,
  formatRawTraceReplay,
} from "../tmem/raw_storage.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

function makeFixtures(workspace) {
  const action1 = new ActionNode({
    step_idx: 1,
    kind: ActionKind.ENV_EXEC,
    tool_name: "bash",
    command: "pytest tests/test_calc.py",
    exit_code: 1,
    error_signature: "ModuleNotFoundError: No module named 'calc'",
    raw_input: { command: "pytest tests/test_calc.py" },
    raw_output: "ModuleNotFoundError: No module named 'calc'",
  });
  const action2 = new ActionNode({
    step_idx: 2,
    kind: ActionKind.READ,
    tool_name: "view",
    target_file: "tests/test_calc.py",
    raw_input: { path: "tests/test_calc.py" },
    raw_output: "import calc\ndef test_add(): assert calc.add(1, 2) == 3",
  });
  const action3 = new ActionNode({
    step_idx: 3,
    kind: ActionKind.MUTATE,
    tool_name: "create",
    target_file: "src/calc.py",
    raw_input: { path: "src/calc.py", content: "def add(a, b): return a + b\n" },
    raw_output: "File created successfully.",
  });
  const action4 = new ActionNode({
    step_idx: 4,
    kind: ActionKind.ENV_EXEC,
    tool_name: "bash",
    command: "pytest tests/test_calc.py",
    exit_code: 0,
    raw_input: { command: "pytest tests/test_calc.py" },
    raw_output: "1 passed in 0.05s",
  });

  const sampleDiff =
    "diff --git a/src/calc.py b/src/calc.py\n" +
    "new file mode 100644\n" +
    "--- /dev/null\n" +
    "+++ b/src/calc.py\n" +
    "@@ -0,0 +1,2 @@\n" +
    "+def add(a, b):\n" +
    "+    return a + b\n";

  const samplePlaybook = new TracePlaybook({
    trace_id: "tr_unit_test_001",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-11T12:00:00Z",
    problem_intent: "Fix missing calc module and pass unit tests",
    semantic_summary: "Created src/calc.py implementing add function to resolve import error",
    error_signatures: ["ModuleNotFoundError: No module named 'calc'"],
    touched_files: ["src/calc.py", "tests/test_calc.py"],
    touched_symbols: ["add", "test_add"],
    guards: [
      new GuardProbe({
        probe_type: "file_exists",
        target: "tests/test_calc.py",
        expected: "",
      }),
    ],
    l0_critical_path: [
      { step: 3, action: "MUTATE", target: "src/calc.py" },
      { step: 4, action: "EXEC", command: "pytest tests/test_calc.py" },
    ],
    l1_cochange_topology: ["tests/test_calc.py <-> src/calc.py"],
    l1_verification_cmds: ["pytest tests/test_calc.py"],
    l2_causal_repairs: [
      new CausalRepairPair({
        error_signature: "ModuleNotFoundError: No module named 'calc'",
        failed_command: "pytest tests/test_calc.py",
        repair_action: "MUTATE(src/calc.py)",
        verification_cmd: "pytest tests/test_calc.py",
      }),
    ],
    confidence: 0.98,
    hit_count: 1,
    status: "active",
    raw_steps_count: 4,
    sliced_steps_count: 2,
    raw_trace_path: ".tmem/raw/tr_unit_test_001.raw.json",
  });

  const sampleRawTrace = new RawExecutionTrace({
    trace_id: "tr_unit_test_001",
    session_id: "sess_unit_test_001",
    agent_source: "unit_test",
    workspace_root: workspace,
    created_at: "2026-11-11T12:00:00Z",
    user_intents: ["Fix missing calc module and pass unit tests"],
    assistant_conclusions: ["All unit tests passed"],
    total_steps: 4,
    actions: [action1.toDict(), action2.toDict(), action3.toDict(), action4.toDict()],
    sliced_step_indices: [3, 4],
    diff_patch: sampleDiff,
    metadata: { test_run: true },
  });

  return { samplePlaybook, sampleRawTrace };
}

test("RawTraceStore lifecycle, replay, diff, and progressive disclosure", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_raw_test_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, ".tmem", "raw"), { recursive: true });
  const { samplePlaybook, sampleRawTrace } = makeFixtures(ws);

  const store = new RawTraceStore(ws);
  const savedPath = store.saveRawTrace(sampleRawTrace);
  assert.ok(existsSync(savedPath));
  assert.equal(path.basename(savedPath), "tr_unit_test_001.raw.json");

  const loaded = store.loadRawTrace("tr_unit_test_001");
  assert.ok(loaded);
  assert.equal(loaded.trace_id, "tr_unit_test_001");
  assert.equal(loaded.actions.length, 4);

  const traces = store.listRawTraces();
  assert.ok(traces.some((tr) => tr.trace_id === "tr_unit_test_001"));

  const replayOutput = formatRawTraceReplay(sampleRawTrace);
  assert.ok(replayOutput.includes("Forensic Trace Replay"));
  assert.ok(replayOutput.includes("NOISE PRUNED"));
  assert.ok(replayOutput.includes("CRITICAL PATH"));

  const diffOutput = formatRawTraceDiff(sampleRawTrace);
  assert.ok(diffOutput.includes("diff --git a/src/calc.py"));

  assert.ok(
    formatProgressiveView(samplePlaybook, sampleRawTrace, "index").includes("[tr_unit_test_001]")
  );
  assert.ok(
    formatProgressiveView(samplePlaybook, sampleRawTrace, "playbook").includes(
      "Playbook: `tr_unit_test_001`"
    )
  );
  assert.ok(
    formatProgressiveView(samplePlaybook, sampleRawTrace, "raw").includes(
      "Forensic Trace Replay"
    )
  );
  assert.ok(
    formatProgressiveView(samplePlaybook, sampleRawTrace, "diff").includes(
      "diff --git a/src/calc.py"
    )
  );

  assert.equal(store.deleteRawTrace("tr_unit_test_001"), true);
  assert.equal(store.loadRawTrace("tr_unit_test_001"), null);
});

test("MEMORIES.md sync, symlink MEM.md, dual-tier store, CLI subcommands, and slash commands", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_mem_test_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  const { samplePlaybook, sampleRawTrace } = makeFixtures(ws);

  const targetPath = syncMemoriesFile(ws, [samplePlaybook]);
  assert.ok(existsSync(targetPath));
  const memoriesContent = readFileSync(targetPath, "utf-8");
  assert.ok(memoriesContent.includes("Project Execution Memories"));
  assert.ok(memoriesContent.includes("tr_unit_test_001"));
  assert.ok(memoriesContent.includes("tmem inspect"));

  const memAlias = path.join(ws, "MEM.md");
  assert.ok(existsSync(memAlias));
  assert.ok(
    lstatSync(memAlias).isSymbolicLink() || readFileSync(memAlias, "utf-8") === memoriesContent
  );

  const store = new ProjectMemoryStore(ws);
  new RawTraceStore(ws).saveRawTrace(sampleRawTrace);
  store.savePlaybook(samplePlaybook, { targetScope: "project" });

  const loadedPbs = store.getAllProjectPlaybooks();
  assert.equal(loadedPbs.length, 1);
  assert.ok(loadedPbs[0].raw_trace_path.endsWith("tr_unit_test_001.raw.json"));
  assert.ok(loadedPbs[0].raw_trace_hash);

  const captureOut = [];
  const io = {
    log: (s) => captureOut.push(String(s)),
    err: (s) => captureOut.push(String(s)),
  };

  assert.equal(
    cliMain(["inspect", "tr_unit_test_001", "--level", "index", "--workspace", ws], io),
    0
  );
  assert.ok(captureOut.join("\n").includes("[tr_unit_test_001]"));

  captureOut.length = 0;
  assert.equal(
    cliMain(["replay", "tr_unit_test_001", "--workspace", ws], io),
    0
  );
  assert.ok(captureOut.join("\n").includes("Forensic Trace Replay"));

  captureOut.length = 0;
  assert.equal(
    cliMain(["diff", "tr_unit_test_001", "--workspace", ws], io),
    0
  );
  assert.ok(captureOut.join("\n").includes("diff --git a/src/calc.py"));

  captureOut.length = 0;
  assert.equal(cliMain(["sync", "--workspace", ws], io), 0);
  assert.ok(captureOut.join("\n").includes("synchronized"));

  assert.ok(SLASH_TMEM_RE.test("/tmem inspect tr_123 --level raw"));
  const resp = handleDshHook("command", {
    sessionId: "conv_test_1",
    workspaceRoot: ws,
    args: "inspect tr_unit_test_001 index",
  });
  assert.equal(resp.kind, "success");
  assert.ok(resp.text.includes("[tr_unit_test_001]"));

  const delRes = store.deletePlaybook("tr_unit_test_001");
  assert.equal(delRes.deleted, true);
  assert.equal(store.getRawTrace("tr_unit_test_001"), null);
});
