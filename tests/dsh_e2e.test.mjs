import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { getAdapter, DshAdapter } from "../tmem/adapters/index.mjs";
import { extractSymbolsFromText } from "../tmem/adapters/base.mjs";
import { executeCapture } from "../tmem/cli.mjs";
import { handleDshHook } from "../tmem/hooks/index.mjs";
import { installInvisibleIntegration, resolveDshEntryJs } from "../tmem/installer.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");

function createGitWorkspace(rootDir) {
  const ws = path.join(rootDir, "dsh_repo");
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  execFileSync("git", ["init"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "dev@tmem.local"], { cwd: ws });
  execFileSync("git", ["config", "user.name", "tmem-tester"], { cwd: ws });

  writeFileSync(
    path.join(ws, "src", "billing.py"),
    "def calculate_unit_tax(amount, units):\n    return amount / units\n",
    "utf-8"
  );
  writeFileSync(
    path.join(ws, "src", "tax_rules.py"),
    "DEFAULT_TAX_FLOOR = 0.0\n",
    "utf-8"
  );
  writeFileSync(
    path.join(ws, "src", "dead_experiment.py"),
    "DEAD = False\n",
    "utf-8"
  );
  writeFileSync(
    path.join(ws, "tests", "test_billing.py"),
    "from src.billing import calculate_unit_tax\ndef test_zero_units():\n    assert calculate_unit_tax(100, 0) == 0.0\n",
    "utf-8"
  );
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "initial commit"], { cwd: ws, stdio: "ignore" });
  return ws;
}

function createDshV4Session(ws, dshHome, sessionId = "sess_dsh_001") {
  writeFileSync(
    path.join(ws, "src", "billing.py"),
    "from src.tax_rules import normalize_rate\n\n" +
      "def calculate_unit_tax(amount, units):\n" +
      "    if units == 0:\n" +
      "        return normalize_rate(0.0)\n" +
      "    return normalize_rate(amount / units)\n",
    "utf-8"
  );
  writeFileSync(
    path.join(ws, "src", "tax_rules.py"),
    "DEFAULT_TAX_FLOOR = 0.0\n\n" +
      "def normalize_rate(val):\n" +
      "    return max(DEFAULT_TAX_FLOOR, round(float(val), 4))\n",
    "utf-8"
  );

  const sessDir = path.join(dshHome, "sessions", sessionId);
  mkdirSync(sessDir, { recursive: true });
  const v4Path = path.join(sessDir, "session.v4.jsonl");

  const patchText =
    "*** Begin Patch\n" +
    "*** Update File: src/billing.py\n" +
    "@@\n" +
    "-def calculate_unit_tax(amount, units):\n" +
    "-    return amount / units\n" +
    "+from src.tax_rules import normalize_rate\n" +
    "+def calculate_unit_tax(amount, units):\n" +
    "+    if units == 0:\n" +
    "+        return normalize_rate(0.0)\n" +
    "+    return normalize_rate(amount / units)\n" +
    "*** Update File: src/tax_rules.py\n" +
    "@@\n" +
    " DEFAULT_TAX_FLOOR = 0.0\n" +
    "+def normalize_rate(val):\n" +
    "+    return max(DEFAULT_TAX_FLOOR, round(float(val), 4))\n" +
    "*** End Patch";

  const events = [
    { version: 4, id: sessionId, createdAt: 1731225600000, isSeeded: false, delegationDepth: 0 },
    {
      type: "user/message",
      seq: 1,
      time: 1731225601000,
      data: {
        id: "msg_sys",
        role: "user",
        content: [{ type: "text", text: "<system-instruction>AGENTS.md</system-instruction>" }],
        source: { kind: "agent-instructions", form: "notice" },
      },
    },
    {
      type: "user/message",
      seq: 2,
      time: 1731225602000,
      data: {
        id: "msg_u1",
        role: "user",
        content: [
          {
            type: "text",
            text: "Fix calculate_unit_tax ZeroDivisionError in src/billing.py and normalize rate in src/tax_rules.py (token github_pat_11ABCDEF0123456789_secretTokenValue)",
          },
        ],
        source: { kind: "user" },
      },
    },
    {
      type: "tool/call",
      seq: 3,
      time: 1731225603000,
      data: {
        callId: "call_read_1",
        name: "read",
        arguments: JSON.stringify({ file_path: path.join(ws, "src", "billing.py") }),
      },
    },
    {
      type: "tool/result",
      seq: 4,
      time: 1731225604000,
      sourceEventSeqs: [3],
      data: {
        message: {
          id: "msg_tr_1",
          role: "tool",
          callId: "call_read_1",
          content: [{ type: "text", text: "1\tdef calculate_unit_tax(amount, units):" }],
          isError: false,
        },
      },
    },
    {
      type: "tool/call",
      seq: 5,
      time: 1731225605000,
      data: {
        callId: "call_dead_edit",
        name: "edit",
        arguments: JSON.stringify({
          file_path: path.join(ws, "src", "dead_experiment.py"),
          old_string: "DEAD = False",
          new_string: "def abandoned_helper(): return True",
        }),
      },
    },
    {
      type: "tool/result",
      seq: 6,
      time: 1731225606000,
      sourceEventSeqs: [5],
      data: {
        message: {
          id: "msg_tr_2",
          role: "tool",
          callId: "call_dead_edit",
          content: [{ type: "text", text: "Successfully edited src/dead_experiment.py" }],
          isError: false,
        },
      },
    },
    {
      type: "tool/call",
      seq: 7,
      time: 1731225607000,
      data: { callId: "call_bash_fail", name: "bash", arguments: JSON.stringify({ command: "pytest -q" }) },
    },
    {
      type: "tool/result",
      seq: 8,
      time: 1731225608000,
      sourceEventSeqs: [7],
      data: {
        message: {
          id: "msg_tr_3",
          role: "tool",
          callId: "call_bash_fail",
          content: [
            {
              type: "text",
              text: "FAILED tests/test_billing.py::test_zero_units\nZeroDivisionError: division by zero in calculate_unit_tax\n\n[exit code: 1]",
            },
          ],
          isError: false,
        },
      },
    },
    {
      type: "tool/call",
      seq: 9,
      time: 1731225609000,
      data: { callId: "call_patch_fix", name: "apply_patch", arguments: JSON.stringify({ input: patchText }) },
    },
    {
      type: "tool/result",
      seq: 10,
      time: 1731225610000,
      sourceEventSeqs: [9],
      data: {
        message: {
          id: "msg_tr_4",
          role: "tool",
          callId: "call_patch_fix",
          content: [{ type: "text", text: "Applied patch to 2 files." }],
          isError: false,
        },
      },
    },
    {
      type: "tool/call",
      seq: 11,
      time: 1731225611000,
      data: { callId: "call_bash_pass", name: "bash", arguments: JSON.stringify({ command: "pytest -q" }) },
    },
    {
      type: "tool/result",
      seq: 12,
      time: 1731225612000,
      sourceEventSeqs: [11],
      data: {
        message: {
          id: "msg_tr_5",
          role: "tool",
          callId: "call_bash_pass",
          content: [{ type: "text", text: "1 passed in 0.02s" }],
          isError: false,
        },
      },
    },
    {
      type: "assistant/message",
      seq: 13,
      time: 1731225613000,
      data: {
        message: {
          id: "msg_asst_1",
          role: "assistant",
          content: [
            {
              type: "text",
              text: "Fixed ZeroDivisionError in [src/billing.py](file:///src/billing.py) by guarding `units == 0` and normalizing tax rates via `normalize_rate` in [src/tax_rules.py](file:///src/tax_rules.py).",
            },
          ],
        },
      },
    },
    {
      type: "user/message",
      seq: 14,
      time: 1731225614000,
      data: {
        id: "msg_u_filler",
        role: "user",
        content: [{ type: "text", text: "done" }],
        source: { kind: "user" },
      },
    },
  ];

  writeFileSync(v4Path, events.map((e) => JSON.stringify(e)).join("\n") + "\n", "utf-8");
  return v4Path;
}

test("DshAdapter v4 parsing and backward slicing with git ground truth", (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), "tmem_dsh_e2e_"));
  const dshHome = path.join(root, "dsh_home");
  mkdirSync(dshHome, { recursive: true });
  t.after(() => rmSync(root, { recursive: true, force: true }));

  const ws = createGitWorkspace(root);
  const v4Path = createDshV4Session(ws, dshHome, "sess_dsh_001");

  const prevDshHome = process.env.DSH_HOME;
  process.env.DSH_HOME = dshHome;
  t.after(() => {
    if (prevDshHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = prevDshHome;
  });

  const adapter = getAdapter("dsh");
  assert.ok(adapter instanceof DshAdapter);
  assert.equal(adapter.discoverLatestTranscript(ws, "sess_dsh_001"), v4Path);

  const parsed = adapter.parseTranscript(v4Path, ws);
  assert.equal(parsed.session_id, "sess_dsh_001");
  assert.equal(parsed.user_intents.length, 1);
  assert.ok(parsed.user_intents[0].includes("[REDACTED_SECRET]"));
  assert.ok(!parsed.user_intents[0].includes("github_pat_"));

  const slicer = new BackwardTraceSlicer();
  const pb = slicer.sliceSession(parsed, true);
  assert.ok(pb);
  assert.deepEqual(pb.touched_files, ["src/billing.py", "src/tax_rules.py"]);
  assert.ok(!pb.touched_files.includes("src/dead_experiment.py"));
  assert.ok(pb.sliced_steps_count < pb.raw_steps_count);
  assert.ok(pb.semantic_summary.includes("Fixed ZeroDivisionError in src/billing.py"));
  assert.ok(pb.semantic_summary.includes("Key symbols: calculate_unit_tax, normalize_rate"));
  assert.ok(
    pb.l1_cochange_topology.some(
      (c) => c.includes("src/billing.py") && c.includes("src/tax_rules.py")
    )
  );
  assert.ok(pb.l1_verification_cmds.includes("pytest -q"));
  assert.equal(pb.l2_causal_repairs.length, 1);
  assert.ok(
    pb.l2_causal_repairs[0].error_signature.includes(
      "ZeroDivisionError: division by zero in calculate_unit_tax"
    )
  );
});

test("Live @deepseek-ai/dsh Cordis runtime and pure-Node CLI E2E", async (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), "tmem_dsh_live_"));
  const dshHome = path.join(root, "dsh_home");
  mkdirSync(dshHome, { recursive: true });
  t.after(() => rmSync(root, { recursive: true, force: true }));

  const ws = createGitWorkspace(root);
  createDshV4Session(ws, dshHome, "sess_dsh_live");

  const prevDshHome = process.env.DSH_HOME;
  process.env.DSH_HOME = dshHome;
  t.after(() => {
    if (prevDshHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = prevDshHome;
  });

  const info = installInvisibleIntegration(ws, ["dsh"], true);
  assert.ok(existsSync(path.join(ws, ".dsh", "plugins", "tmem-dsh-plugin.mjs")));
  assert.ok(existsSync(path.join(ws, ".dsh", "tmem.patch.yml")));
  assert.ok(existsSync(path.join(ws, ".dsh", "skills", "tmem", "SKILL.md")));
  assert.ok(existsSync(path.join(ws, "AGENTS.md")));
  assert.equal(info.git_hook, true);

  const dshEntry = resolveDshEntryJs();
  if (!dshEntry) {
    t.skip("@deepseek-ai/dsh runtime is not installed on this host");
    return;
  }

  // Verify real dsh --dump-config loads tmem patch
  const dumpOut = execFileSync(
    "dsh",
    ["--profile", "headless", "--patch", path.join(ws, ".dsh", "tmem.patch.yml"), "--dump-config"],
    { cwd: ws, encoding: "utf-8" }
  );
  assert.ok(dumpOut.includes("id: tmem"));
  assert.ok(dumpOut.includes("tmem-dsh-plugin.mjs"));

  // Load real @deepseek-ai/cordis + @deepseek-ai/dsh-commands + generated plugin in-process
  const req = createRequire(pathToFileURL(dshEntry).href);
  const { Context } = req("@deepseek-ai/cordis");
  const dshCommands = req("@deepseek-ai/dsh-commands");
  const tmemPlugin = await import(
    pathToFileURL(path.join(ws, ".dsh", "plugins", "tmem-dsh-plugin.mjs")).href
  );

  const ctx = new Context();
  ctx.plugin(dshCommands.default);
  ctx.plugin(tmemPlugin.default, {
    workspaceRoot: ws,
    tmemRoot: REPO_ROOT,
  });

  await new Promise((r) => setTimeout(r, 50));

  const commandsSvc = ctx.get("commands");
  const mockAgent = {
    session: {
      id: "sess_dsh_live",
      append() {},
    },
  };
  const ac = new AbortController();

  const captureExec = await commandsSvc.execute(mockAgent, "/tmem both", [], ac.signal);
  assert.equal(captureExec.result.kind, "success");
  assert.ok(captureExec.result.text.includes('"status": "compiled"'));
  assert.ok(captureExec.result.text.includes('"agent": "dsh"'));

  const statusExec = await commandsSvc.execute(mockAgent, "/tmem status", [], ac.signal);
  assert.ok(statusExec.result.text.includes("Project Tier"));
  assert.ok(statusExec.result.text.includes("1 trace(s)"));

  const benchExec = await commandsSvc.execute(mockAgent, "/tmem bench", [], ac.signal);
  assert.ok(benchExec.result.text.includes("轨迹切片与特征描述评测"));
  assert.ok(benchExec.result.text.includes("PASSED"));

  const runPostTool = async (exec, result) => {
    let idx = -1;
    const hooks = [...(ctx.events._hooks["tools/post-execute"] || [])];
    const dispatch = async (i) => {
      if (i <= idx) throw new Error("next() called multiple times");
      idx = i;
      const entry = hooks[i];
      if (!entry) return { content: result.content, isError: result.isError };
      return entry.callback.call(ctx, exec, result, () => dispatch(i + 1));
    };
    return dispatch(0);
  };

  await runPostTool(
    {
      name: "edit",
      arguments: JSON.stringify({ file_path: "src/billing.py", old_string: "a", new_string: "b" }),
      agent: { session: { id: "sess_dsh_live" } },
    },
    { content: [{ type: "text", text: "Edited src/billing.py" }], isError: false }
  );

  const postVerify = await runPostTool(
    {
      name: "bash",
      arguments: JSON.stringify({ command: "pytest -q" }),
      agent: { session: { id: "sess_dsh_live" } },
    },
    { content: [{ type: "text", text: "1 passed in 0.01s" }], isError: false }
  );

  assert.equal((postVerify.additionalContexts || []).length, 1);
  const milestoneText = postVerify.additionalContexts[0].content[0].text;
  assert.ok(milestoneText.includes("<tmem_milestone_reached>"));
  assert.ok(milestoneText.includes("/tmem"));

  const runPreStep = async (payload) => {
    const hooks = [...(ctx.events._hooks["agent/pre-step"] || [])];
    const dispatch = async (i) => {
      const entry = hooks[i];
      if (!entry) return { action: "continue", messages: payload.messages };
      return entry.callback.call(ctx, payload, () => dispatch(i + 1));
    };
    return dispatch(0);
  };

  const preStepDecision = await runPreStep({
    agent: { session: { id: "sess_dsh_recall" } },
    step: 0,
    messages: [
      {
        id: "u_new",
        role: "user",
        content: [{ type: "text", text: "Fix calculate_unit_tax ZeroDivisionError in src/billing.py" }],
        source: { kind: "user" },
      },
    ],
  });

  assert.equal(preStepDecision.messages.length, 2);
  assert.equal(preStepDecision.messages[0].source.kind, "tmem");
  const recalledText = preStepDecision.messages[0].content[0].text;
  assert.ok(recalledText.includes("<tmem_speculative_memory>"));
  assert.ok(recalledText.includes("Tier: L0_FULL"));

  // Code drift degrades to L2_INVARIANT
  writeFileSync(
    path.join(ws, "src", "billing.py"),
    "# Refactored completely\ndef compute_v2(amount, units):\n    return amount / units\n",
    "utf-8"
  );
  const errOut = handleDshHook("post_tool_use", {
    workspaceRoot: ws,
    sessionId: "sess_dsh_drift",
    toolCall: { name: "bash", args: { command: "pytest -q" } },
    resultText: "ZeroDivisionError: division by zero in calculate_unit_tax\n\n[exit code: 1]",
    isError: false,
    exitCode: null,
  });
  assert.ok(errOut.additionalContext.includes("Tier: L2_INVARIANT"));
  assert.ok(
    errOut.additionalContext.includes("missing_symbol:src/billing.py#calculate_unit_tax")
  );
  assert.ok(errOut.additionalContext.includes("L2 Causal Repair:"));

  // List and forget
  assert.deepEqual(extractSymbolsFromText("function for the system", "README.md"), []);
  const listOut = handleDshHook("command", {
    workspaceRoot: ws,
    sessionId: "sess_dsh_live",
    args: "list project",
  });
  assert.equal(listOut.kind, "success");
  assert.ok(listOut.text.includes("tr_"));

  const forgetOut = handleDshHook(
    "command",
    {
      workspaceRoot: ws,
      sessionId: "sess_dsh_live",
      args: "forget tr_",
    },
    executeCapture
  );
  assert.equal(forgetOut.kind, "success");
  assert.match(forgetOut.text, /"deleted":\s*true/);
});

test("Pure-Node plugin source contracts, license, and zero Python dependency", () => {
  const licensePath = path.join(REPO_ROOT, "LICENSE");
  assert.ok(existsSync(licensePath));
  const licenseTxt = readFileSync(licensePath, "utf-8");
  assert.ok(licenseTxt.includes("MIT License"));
  assert.ok(licenseTxt.includes("kentzjh-cmd"));

  const pkgJson = JSON.parse(readFileSync(path.join(REPO_ROOT, "package.json"), "utf-8"));
  assert.ok((pkgJson.files || []).includes("LICENSE"));
  assert.equal(pkgJson.bin?.tmem, "tmem/cli.mjs");

  const pluginCode = readFileSync(path.join(REPO_ROOT, "tmem", "dsh_plugin.mjs"), "utf-8");
  assert.ok(!pluginCode.includes("spawnSync"));
  assert.ok(!pluginCode.includes("python3"));
  assert.ok(!pluginCode.includes("PYTHONPATH"));
});
