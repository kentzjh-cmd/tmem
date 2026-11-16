import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { getAdapter } from "../tmem/adapters/index.mjs";
import { executeCapture } from "../tmem/cli.mjs";
import { handleDshHook } from "../tmem/hooks/index.mjs";
import { installInvisibleIntegration } from "../tmem/installer.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

function createNoisyDshTranscript(ws) {
  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  writeFileSync(
    path.join(ws, "src", "auth.py"),
    "def verify_jwt_token(token: str, leeway: int = 30):\n    return True\n"
  );
  writeFileSync(
    path.join(ws, "tests", "test_auth.py"),
    "def test_jwt_leeway():\n    assert True\n"
  );
  writeFileSync(path.join(ws, "src", "unrelated.py"), "x = 1\n");

  const transcriptFile = path.join(ws, "session.v4.jsonl");
  const events = [
    { version: 4, id: "sess_e2e_01", createdAt: 1731225600000 },
    { type: "user/message", seq: 1, data: { role: "user", content: [{ type: "text", text: "Fix JWT clock skew verification failure in auth module" }], source: { kind: "user" } } },
    { type: "tool/call", seq: 2, data: { callId: "c1", name: "bash", arguments: JSON.stringify({ command: "ls -la" }) } },
    { type: "tool/result", seq: 3, data: { message: { callId: "c1", content: [{ type: "text", text: "total 8\n[exit code: 0]" }], isError: false } } },
    { type: "tool/call", seq: 4, data: { callId: "c2", name: "read", arguments: JSON.stringify({ file_path: path.join(ws, "src/unrelated.py") }) } },
    { type: "tool/result", seq: 5, data: { message: { callId: "c2", content: [{ type: "text", text: "1: x = 1" }], isError: false } } },
    { type: "tool/call", seq: 6, data: { callId: "c3", name: "write", arguments: JSON.stringify({ file_path: path.join(ws, "src/dead_temp.py"), content: "def bad_idea(): pass" }) } },
    { type: "tool/result", seq: 7, data: { message: { callId: "c3", content: [{ type: "text", text: "Created file." }], isError: false } } },
    { type: "tool/call", seq: 8, data: { callId: "c4", name: "read", arguments: JSON.stringify({ file_path: path.join(ws, "src/auth.py") }) } },
    { type: "tool/result", seq: 9, data: { message: { callId: "c4", content: [{ type: "text", text: "1: def verify_jwt_token(token: str): ..." }], isError: false } } },
    { type: "tool/call", seq: 10, data: { callId: "c5", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_auth.py" }) } },
    { type: "tool/result", seq: 11, data: { message: { callId: "c5", content: [{ type: "text", text: "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]" }], isError: false } } },
    { type: "tool/call", seq: 12, data: { callId: "c6", name: "bash", arguments: JSON.stringify({ command: "pip install pyjwt>=2.8.0" }) } },
    { type: "tool/result", seq: 13, data: { message: { callId: "c6", content: [{ type: "text", text: "Successfully installed pyjwt-2.8.0\n[exit code: 0]" }], isError: false } } },
    { type: "tool/call", seq: 14, data: { callId: "c7", name: "edit", arguments: JSON.stringify({ file_path: path.join(ws, "src/auth.py"), new_string: "def verify_jwt_token(token: str, leeway: int = 30):" }) } },
    { type: "tool/result", seq: 15, data: { message: { callId: "c7", content: [{ type: "text", text: "Edited file." }], isError: false } } },
    { type: "tool/call", seq: 16, data: { callId: "c8", name: "write", arguments: JSON.stringify({ file_path: path.join(ws, "tests/test_auth.py"), content: "def test_jwt_leeway(): assert True" }) } },
    { type: "tool/result", seq: 17, data: { message: { callId: "c8", content: [{ type: "text", text: "Created file." }], isError: false } } },
    { type: "tool/call", seq: 18, data: { callId: "c9", name: "bash", arguments: JSON.stringify({ command: "pytest tests/test_auth.py" }) } },
    { type: "tool/result", seq: 19, data: { message: { callId: "c9", content: [{ type: "text", text: "1 passed in 0.03s\n[exit code: 0]" }], isError: false } } },
  ];
  writeFileSync(transcriptFile, events.map((e) => JSON.stringify(e)).join("\n"), "utf-8");
  return transcriptFile;
}

test("backward slicing, guard degradation, and repository isolation", (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), "tmem_e2e_"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const wsA = path.join(root, "repo_a");
  const wsB = path.join(root, "repo_b");
  mkdirSync(wsA, { recursive: true });
  mkdirSync(wsB, { recursive: true });

  const info = installInvisibleIntegration(wsA, ["dsh"]);
  assert.ok(existsSync(path.join(wsA, ".dsh", "plugins", "tmem-dsh-plugin.mjs")));
  assert.equal(info.agents.length, 1);

  const tPath = createNoisyDshTranscript(wsA);
  const session = getAdapter("dsh").parseTranscript(tPath, wsA);
  assert.equal(session.actions.length, 9);

  const pb = new BackwardTraceSlicer().sliceSession(session, false);
  assert.ok(pb);
  assert.deepEqual(pb.touched_files, ["src/auth.py", "tests/test_auth.py"]);
  const targetsInL0 = pb.l0_critical_path.map((s) => s.target).filter(Boolean);
  assert.ok(!targetsInL0.includes("src/unrelated.py"));
  assert.ok(!targetsInL0.includes("src/dead_temp.py"));
  assert.ok(targetsInL0.includes("src/auth.py"));
  assert.equal(pb.l2_causal_repairs.length, 1);
  assert.equal(pb.l2_causal_repairs[0].is_env_or_toolchain, true);

  const storeA = new ProjectMemoryStore(wsA);
  const [projId] = storeA.savePlaybook(pb);
  assert.ok(projId.startsWith("tr_"));

  const hookOut = handleDshHook("pre_invocation", {
    sessionId: "conv_2",
    workspaceRoot: wsA,
    lastUserInput: "Update verify_jwt_token in src/auth.py",
  });
  assert.ok((hookOut.injectedContext || "").includes("Tier: L0_FULL"));
  assert.ok((hookOut.injectedContext || "").includes("Verified L0 Critical Path (Guards Passed)"));

  unlinkSync(path.join(wsA, "src", "auth.py"));
  const degOut = handleDshHook("post_tool_use", {
    sessionId: "conv_3",
    workspaceRoot: wsA,
    toolCall: { name: "bash", args: { command: "pytest tests/test_auth.py" } },
    resultText: "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]",
  });
  assert.ok((degOut.additionalContext || "").includes("L0 concrete paths drifted (missing_file:src/auth.py)"));
  assert.ok((degOut.additionalContext || "").includes("L2 Causal Repair"));

  const crossOut = handleDshHook("post_tool_use", {
    sessionId: "conv_b1",
    workspaceRoot: wsB,
    toolCall: { name: "bash", args: { command: "pytest tests/test_auth.py" } },
    resultText: "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]",
  });
  assert.deepEqual(crossOut, {});
});

test("milestone prompt and /tmem slash command flow", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_ms_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  const tPath = createNoisyDshTranscript(ws);

  handleDshHook("post_tool_use", {
    sessionId: "conv_m1",
    workspaceRoot: ws,
    toolCall: { name: "edit", args: { file_path: path.join(ws, "src/auth.py") } },
  });
  const msOut = handleDshHook("post_tool_use", {
    sessionId: "conv_m1",
    workspaceRoot: ws,
    toolCall: { name: "bash", args: { command: "pytest tests/test_auth.py" } },
    resultText: "1 passed\n[exit code: 0]",
  });
  assert.ok((msOut.additionalContext || "").includes("<tmem_milestone_reached>"));

  const cmdOut = handleDshHook(
    "command",
    { sessionId: "conv_m1", workspaceRoot: ws, args: "Fix JWT leeway validation" },
    (opts) => executeCapture({ ...opts, transcriptPath: tPath })
  );
  assert.equal(cmdOut.kind, "success");
  assert.ok(cmdOut.text.includes('"scope": "project"'));
  assert.ok(cmdOut.text.includes("Fix JWT leeway validation"));

  const statOut = handleDshHook("command", {
    sessionId: "conv_m1",
    workspaceRoot: ws,
    args: "status",
  });
  assert.equal(statOut.kind, "success");
  assert.ok(statOut.text.includes("Project Tier"));
  assert.ok(statOut.text.includes("1 trace(s)"));
});
