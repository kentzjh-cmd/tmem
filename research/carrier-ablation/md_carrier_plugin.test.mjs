/**
 * Zero-cost smoke test for the prose carrier injector.
 *
 * Runs the plugin against a fake DSH `ctx` and a fake agent, and checks that
 * the carrier text is spliced in front of the last user turn exactly once.
 *
 *   node md_carrier_plugin.test.mjs
 */
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin from "./md_carrier_plugin.mjs";

const ws = mkdtempSync(join(tmpdir(), "mdcarrier-"));
writeFileSync(join(ws, "MEMORY.md"), "# 项目工程决策记录\n\n- 事实一：money.py → settlement.py\n", "utf-8");

function mount(config) {
  let handler = null;
  const ctx = {
    on(event, fn) {
      if (event === "agent/pre-step") handler = fn;
    },
    get() {
      return null;
    },
  };
  plugin.apply(ctx, config);
  if (!handler) {
    console.error("FAIL: the plugin did not register an agent/pre-step handler");
    process.exit(1);
  }
  return handler;
}

const handler = mount({ workspaceRoot: ws });

const messages = [
  { role: "user", content: [{ type: "text", text: "第一个问题" }] },
  { role: "assistant", content: [{ type: "text", text: "回答" }] },
  { role: "user", content: [{ type: "text", text: "第二个问题" }] },
];

const decision = await handler({ agent: { session: { id: "s1", header: { cwd: ws } } }, messages, step: 1 }, async () => ({ action: "continue", messages }));

const injected = (decision.messages || []).filter((m) => m && m.source?.kind === "md-carrier");
const ok = [];
ok.push(["notice spliced exactly once", injected.length === 1]);
ok.push([
  "spliced in front of the last user turn",
  decision.messages.findIndex((m) => m?.source?.kind === "md-carrier") ===
    decision.messages.findLastIndex((m) => m?.role === "user" && (!m.source || m.source.kind === "user")) - 1,
]);
ok.push(["carrier text carried", (injected[0]?.content?.[0]?.text || "").includes("money.py")]);
ok.push(["original messages preserved", decision.messages.length === messages.length + 1]);

// second turn with the same input must not inject twice
const decision2 = await handler({ agent: { session: { id: "s1", header: { cwd: ws } } }, messages: decision.messages, step: 2 }, async () => ({ action: "continue", messages: decision.messages }));
const injected2 = (decision2.messages || []).filter((m) => m && m.source?.kind === "md-carrier");
ok.push(["no duplicate injection on the next turn", injected2.length === 1]);

// PHASE-1 BUG REGRESSION: DSH rebuilds the message list between steps, so the
// previous injection is NOT in the list any more. The cadence guard must still
// hold (this is what made B1 pay 4-7 copies of the prose in phase 1).
const rebuilt = [
  { role: "user", content: [{ type: "text", text: "第一个问题" }] },
  { role: "assistant", content: [{ type: "text", text: "回答" }] },
  { role: "user", content: [{ type: "text", text: "第二个问题" }] },
];
const decision2b = await handler({ agent: { session: { id: "s1", header: { cwd: ws } } }, messages: rebuilt, step: 3 }, async () => ({ action: "continue", messages: rebuilt }));
ok.push([
  "no re-injection when the message list was rebuilt (same user turn)",
  (decision2b.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 0,
]);

// a NEW user turn must receive the carrier again (once per user turn)
const nextTurn = [
  { role: "user", content: [{ type: "text", text: "第三个问题" }] },
];
const decision2c = await handler({ agent: { session: { id: "s1", header: { cwd: ws } } }, messages: nextTurn, step: 4 }, async () => ({ action: "continue", messages: nextTurn }));
ok.push([
  "a new user turn gets the carrier again",
  (decision2c.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 1,
]);

// the guard is session-scoped: another session with the same input must inject
const otherSession = [
  { role: "user", content: [{ type: "text", text: "第一个问题" }] },
  { role: "assistant", content: [{ type: "text", text: "回答" }] },
  { role: "user", content: [{ type: "text", text: "第二个问题" }] },
];
const decision2d = await handler({ agent: { session: { id: "s2", header: { cwd: ws } } }, messages: otherSession, step: 1 }, async () => ({ action: "continue", messages: otherSession }));
ok.push([
  "the guard is session-scoped (a new session injects again)",
  (decision2d.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 1,
]);

// a workspace without the carrier injects nothing
const emptyWs = mkdtempSync(join(tmpdir(), "mdcarrier-empty-"));
mkdirSync(join(emptyWs, "sub"), { recursive: true });
const handler3 = mount({ workspaceRoot: emptyWs });
const decision3 = await handler3({ agent: { session: { id: "s3", header: { cwd: emptyWs } } }, messages, step: 1 }, async () => ({ action: "continue", messages }));
ok.push(["no carrier -> no injection", (decision3.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 0]);

// DSH delivers environment metadata as a user-role message without a source.
// It is NOT a user turn: the injector must stay silent for it, otherwise the
// carrier is injected twice per session (phase-1b attempt #1 measured 2).
const reminderOnly = [
  { role: "user", content: [{ type: "text", text: "<system-reminder>\nCurrent working directory: \"/work\".\n</system-reminder>" }] },
];
const handlerR = mount({ workspaceRoot: ws });
const decisionR = await handlerR({ agent: { session: { id: "s-r", header: { cwd: ws } } }, messages: reminderOnly, step: 1 }, async () => ({ action: "continue", messages: reminderOnly }));
ok.push([
  "a system-reminder-only message is not a user turn (no injection)",
  (decisionR.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 0,
]);

// after the reminder, the real prompt is the first (and only) user turn
const realPrompt = [
  { role: "user", content: [{ type: "text", text: "<system-reminder>\nCurrent working directory: \"/work\".\n</system-reminder>" }] },
  { role: "user", content: [{ type: "text", text: "第一个问题" }] },
  { role: "assistant", content: [{ type: "text", text: "回答" }] },
  { role: "user", content: [{ type: "text", text: "第二个问题" }] },
];
const decisionR2 = await handlerR({ agent: { session: { id: "s-r", header: { cwd: ws } } }, messages: realPrompt, step: 2 }, async () => ({ action: "continue", messages: realPrompt }));
ok.push([
  "the real prompt injects exactly once",
  (decisionR2.messages || []).filter((m) => m?.source?.kind === "md-carrier").length === 1,
]);

let failed = 0;
for (const [name, pass] of ok) {
  console.log(`${pass ? "ok  " : "FAIL"} ${name}`);
  if (!pass) failed++;
}
process.exit(failed ? 1 : 0);
