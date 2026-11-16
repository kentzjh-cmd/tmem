import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ParsedSession, isHighEntropyErrorSignature } from "../tmem/adapters/base.mjs";
import { executeCapture } from "../tmem/cli.mjs";
import { computeCochangeStats } from "../tmem/cochange.mjs";
import { handleDshHook } from "../tmem/hooks/index.mjs";
import { extractMicroSkills, renderMemoriesMarkdown } from "../tmem/mem_doc.mjs";
import { ActionKind, ActionNode, CausalRepairPair, TracePlaybook } from "../tmem/models.mjs";
import { BackwardTraceSlicer, assessPlaybookTriviality } from "../tmem/slicer.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");

function setupGitWs(prefix) {
  const ws = mkdtempSync(path.join(os.tmpdir(), prefix));
  execFileSync("git", ["init", "-b", "main"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["config", "user.name", "Tester"], { cwd: ws });
  execFileSync("git", ["config", "user.email", "tester@test.local"], { cwd: ws });
  return ws;
}

test("assessPlaybookTriviality detects single-file routine vs multi-file / L2 repairs", () => {
  const pbTriv = new TracePlaybook({
    trace_id: "tr_trivial_01",
    agent_source: "dsh",
    touched_files: ["lib/calc.py"],
    l0_critical_path: [
      { action: "READ", target: "lib/calc.py" },
      { action: "MUTATE", target: "lib/calc.py" },
      { action: "ENV_EXEC", command: "pytest tests/test_calc.py" },
    ],
  });
  const [isTriv, reason] = assessPlaybookTriviality(pbTriv);
  assert.equal(isTriv, true);
  assert.ok(reason.includes("Tautological single-file routine"));

  const pbMulti = new TracePlaybook({
    trace_id: "tr_multi_01",
    agent_source: "dsh",
    touched_files: ["lib/a.py", "lib/b.py"],
    l0_critical_path: [
      { action: "MUTATE", target: "lib/a.py" },
      { action: "MUTATE", target: "lib/b.py" },
      { action: "ENV_EXEC", command: "pytest" },
    ],
  });
  assert.equal(assessPlaybookTriviality(pbMulti)[0], false);

  const pbRepair = new TracePlaybook({
    trace_id: "tr_repair_01",
    agent_source: "dsh",
    touched_files: ["lib/calc.py"],
    l0_critical_path: [{ action: "MUTATE", target: "lib/calc.py" }, { action: "ENV_EXEC", command: "pytest" }],
    l2_causal_repairs: [
      new CausalRepairPair({
        error_signature: "ZeroDivisionError",
        failed_command: "pytest",
        repair_action: "edit lib/calc.py",
        verification_cmd: "pytest",
      }),
    ],
  });
  assert.equal(assessPlaybookTriviality(pbRepair)[0], false);
});

test("enhanced L2 causal repair extraction on reproduction scripts and git boundary isolation", (t) => {
  const ws = setupGitWs("tmem_opt_");
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  const sessionRep = new ParsedSession({
    session_id: "sess_rep_01",
    agent_name: "swe_bench",
    workspace_root: ws,
    user_intents: ["Fix TypeError in table header rows formatting"],
    actions: [
      new ActionNode({ step_idx: 1, kind: ActionKind.READ, tool_name: "read_file", target_file: "lib/rst.py" }),
      new ActionNode({
        step_idx: 2,
        kind: ActionKind.ENV_EXEC,
        tool_name: "run_command",
        command: "python reproduce_issue.py",
        exit_code: 1,
        error_signature: "TypeError: can only join an iterable",
      }),
      new ActionNode({
        step_idx: 3,
        kind: ActionKind.MUTATE,
        tool_name: "edit_file",
        target_file: "lib/rst.py",
        symbols_touched: ["write", "header_rows"],
      }),
      new ActionNode({
        step_idx: 4,
        kind: ActionKind.ENV_EXEC,
        tool_name: "run_command",
        command: "python reproduce_issue.py",
        exit_code: 0,
      }),
    ],
  });
  const pbRep = new BackwardTraceSlicer().sliceSession(sessionRep, false);
  assert.equal(pbRep.l2_causal_repairs.length, 1);
  assert.equal(pbRep.l2_causal_repairs[0].verification_cmd, "python reproduce_issue.py");

  // Git ground truth boundary isolation
  writeFileSync(path.join(ws, "unrelated.py"), "print('old')\n");
  writeFileSync(path.join(ws, "target.py"), "def run(): pass\n");
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "init"], { cwd: ws, stdio: "ignore" });
  writeFileSync(path.join(ws, "unrelated.py"), "print('dirty unrelated change')\n");
  writeFileSync(path.join(ws, "target.py"), "def run(): return 42\n");

  const slicer = new BackwardTraceSlicer();
  const pbBoundary = slicer.sliceSession(
    new ParsedSession({
      session_id: "sess_boundary_01",
      agent_name: "dsh",
      workspace_root: ws,
      user_intents: ["Update run in target.py"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.READ, tool_name: "read_file", target_file: "target.py" }),
        new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "target.py", symbols_touched: ["run"] }),
        new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "node --test", exit_code: 0 }),
      ],
    }),
    true
  );
  assert.deepEqual(pbBoundary.touched_files, ["target.py"]);
  assert.ok((slicer.last_git_diff || "").includes("target.py"));
  assert.ok(!(slicer.last_git_diff || "").includes("unrelated.py"));
});

test("high-entropy error gate, tr_37a01e0db5 fixture, confidence cap, meta non_task, and pipe escaping", (t) => {
  const ws = setupGitWs("tmem_gate_");
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  for (const sig of [
    "TypeError: unexpected keyword argument 'header_rows'",
    "ModuleNotFoundError: No module named 'six'",
    "error[E0432]: unresolved import `std::collections`",
    "Cannot find module 'express'",
  ]) {
    assert.equal(isHighEntropyErrorSignature(sig), true);
  }
  for (const sig of [
    "FAILED (failures=1)",
    "FAIL: test_feature",
    "Command failed (exit 2): tmem inspect ...",
    "Process exited with code 1",
    "Error",
  ]) {
    assert.equal(isHighEntropyErrorSignature(sig), false);
  }

  // tr_37a01e0db5 fixture produces 0 L2 repairs
  const fixturePath = path.join(__dirname, "fixtures", "tr_37a01e0db5.raw.json");
  const rawData = JSON.parse(readFileSync(fixturePath, "utf-8"));
  const actions = rawData.actions.map((a) => ActionNode.fromDict(a));
  const slicer = new BackwardTraceSlicer();
  const [repairs] = slicer._extractCausalRepairsWithIndices(
    actions,
    new Set([
      "README.md",
      "README_EN.md",
      "tests/test_optimizations.py",
      "tmem/adapters/base.py",
      "tmem/evaluate.py",
      "tmem/slicer.py",
    ]),
    { workspaceRoot: ws }
  );
  assert.equal(repairs.length, 0);

  // Inline assignment rejected
  assert.equal(
    slicer._extractCanonicalVerificationCmd("r = re.compile(r'test') && print('test')"),
    null
  );

  // Meta review marked non_task
  const metaPb = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "test_meta_sess",
      workspace_root: ws,
      user_intents: ["务实的评价下本项目，客观甚至刻薄的给与评分", "根据反馈优化项目"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.READ, tool_name: "read_file", target_file: "tmem/slicer.mjs" }),
        new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "README.md" }),
      ],
    }),
    false
  );
  assert.equal(metaPb, null);

  // MEMORIES.md multiline & pipe escaping
  const md = renderMemoriesMarkdown(
    [
      new TracePlaybook({
        trace_id: "tr_multi_cmd",
        agent_source: "dsh",
        l2_causal_repairs: [
          new CausalRepairPair({
            error_signature: "TypeError: bad | signature\nline 2",
            failed_command: "python3 -c '\nprint(\"foo | bar\")\n'",
            repair_action: "edit a.py | edit b.py",
            verification_cmd: "pytest -k 'test | pipe'",
          }),
        ],
      }),
    ],
    ws
  );
  const l2Rows = md.split("\n").filter((ln) => ln.includes("python3 -c") && ln.startsWith("|"));
  assert.equal(l2Rows.length, 1);
  const cols = l2Rows[0].replace(/^\|+|\|+$/g, "").split(/(?<!\\)\|/);
  assert.equal(cols.length, 4);

  // AGENTS.md check
  const agentsContent = readFileSync(path.join(REPO_ROOT, "AGENTS.md"), "utf-8");
  assert.ok(agentsContent.toLowerCase().includes("clues"));
  assert.ok(!agentsContent.includes("redundant exploratory `grep`/`find` scans"));
});

test("same-origin L2 matching, AST diff line-range, statistical co-change, and default-deny + env_facts", (t) => {
  const ws = setupGitWs("tmem_p2_");
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  // Milestone suppression on docs
  handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "conv_doc_01",
    toolCall: { name: "edit", args: { file_path: path.join(ws, "README.md") } },
  });
  const docPost = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "conv_doc_01",
    toolCall: { name: "bash", args: { command: "node --test" } },
    resultText: "OK\n[exit code: 0]",
  });
  assert.ok(!(docPost.additionalContext || "").includes("<tmem_milestone_reached>"));

  // Mismatched test file vs broader runner
  const slicer = new BackwardTraceSlicer();
  const pbMismatch = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "sess_mismatch",
      workspace_root: ws,
      user_intents: ["Fix src/a.py test failure"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_a.py", exit_code: 1, error_signature: "ValueError: bad input" }),
        new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/a.py", symbols_touched: ["parse"] }),
        new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest tests/test_b.py", exit_code: 0 }),
      ],
    }),
    false
  );
  assert.deepEqual(pbMismatch.l2_causal_repairs, []);

  // AST diff line range & guard priority
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "engine.py"), "def compute_tax(amount):\n    rate = 0.1\n    return amount * rate\n");
  writeFileSync(path.join(ws, "README.md"), "# Engine\n");
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "init"], { cwd: ws, stdio: "ignore" });
  writeFileSync(path.join(ws, "src", "engine.py"), "def compute_tax(amount):\n    rate = 0.2\n    return amount * rate\n");
  writeFileSync(path.join(ws, "README.md"), "# Engine v2\n");

  const pbAst = slicer.sliceSession(
    new ParsedSession({
      agent_name: "dsh",
      session_id: "sess_ast_guard",
      workspace_root: ws,
      user_intents: ["Update tax rate in engine"],
      actions: [
        new ActionNode({ step_idx: 1, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "README.md" }),
        new ActionNode({ step_idx: 2, kind: ActionKind.MUTATE, tool_name: "edit_file", target_file: "src/engine.py" }),
        new ActionNode({ step_idx: 3, kind: ActionKind.ENV_EXEC, tool_name: "run_command", command: "pytest", exit_code: 0 }),
      ],
    }),
    true
  );
  assert.ok(pbAst.touched_symbols.includes("compute_tax"));
  const fileGuards = pbAst.guards.filter((g) => g.probe_type === "file_exists").map((g) => g.target);
  assert.equal(fileGuards[0], "src/engine.py");
  assert.ok(pbAst.confidence <= 0.7);

  // Statistical co-change bulk filter
  for (let idx = 0; idx < 11; idx++) {
    writeFileSync(path.join(ws, `mod_${idx}.py`), "x = 1\n");
  }
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "bulk"], { cwd: ws, stdio: "ignore" });
  for (const rev of ["v2", "v3"]) {
    writeFileSync(path.join(ws, "mod_0.py"), `# ${rev}\n`);
    writeFileSync(path.join(ws, "mod_1.py"), `# ${rev}\n`);
    execFileSync("git", ["add", "."], { cwd: ws });
    execFileSync("git", ["commit", "-m", `pair ${rev}`], { cwd: ws, stdio: "ignore" });
  }
  const pairs = computeCochangeStats(ws);
  assert.ok(pairs.some((p) => p.file_a === "mod_0.py" && p.file_b === "mod_1.py"));
  assert.ok(pairs.every((p) => p.file_a !== "README.md" && p.file_b !== "README.md"));

  // Default-deny + env_facts persistence across >=2 routine sessions
  const writeRoutine = (p, prompt) => {
    const entries = [
      { version: 4, id: path.parse(p).name, createdAt: 1731225600000 },
      { type: "user/message", seq: 1, data: { role: "user", content: [{ type: "text", text: prompt }], source: { kind: "user" } } },
      { type: "tool/call", seq: 2, data: { callId: "c1", name: "edit", arguments: JSON.stringify({ file_path: "src/helper.py" }) } },
      { type: "tool/result", seq: 3, data: { message: { callId: "c1", content: [{ type: "text", text: "Edited" }], isError: false } } },
      { type: "tool/call", seq: 4, data: { callId: "c2", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_helper.py" }) } },
      { type: "tool/result", seq: 5, data: { message: { callId: "c2", content: [{ type: "text", text: "1 passed\n[exit code: 0]" }], isError: false } } },
    ];
    writeFileSync(p, entries.map((e) => JSON.stringify(e)).join("\n"));
  };
  const r1 = path.join(ws, "routine1.v4.jsonl");
  const r2 = path.join(ws, "routine2.v4.jsonl");
  writeRoutine(r1, "Refactor helper docstring");
  writeRoutine(r2, "Second routine helper edit");

  assert.equal(
    executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: r1, customIntent: "Long custom intent", force: false }).status,
    "skipped_trivial"
  );
  assert.equal(
    executeCapture({ workspaceRoot: ws, agentName: "dsh", transcriptPath: r2, force: false }).status,
    "skipped_trivial"
  );
  const skills = extractMicroSkills([], { minTraces: 2, workspaceRoot: ws });
  assert.ok(skills.includes("pytest tests/test_helper.py"));
});
