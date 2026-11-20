import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { ParsedSession } from "../tmem/adapters/base.mjs";
import { executeCapture, main as cliMain } from "../tmem/cli.mjs";
import { findCochangePartnersForFiles } from "../tmem/cochange.mjs";
import { expandMemoryTarget } from "../tmem/dsh_tools.mjs";
import {
  SessionEpisode,
  compileEpisodeFromSpan,
  formatEpisodeProgressiveView,
} from "../tmem/episodes.mjs";
import {
  checkSymbolSignatureMatch,
  evaluateGuards,
  extractSymbolSignature,
} from "../tmem/guards.mjs";
import { resolveDshEntryJs } from "../tmem/installer.mjs";
import { tokenizeInvertedTerms } from "../tmem/inverted_index.mjs";
import { ActionKind, ActionNode, GuardProbe, TracePlaybook } from "../tmem/models.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { setConnectionTrackingHooks } from "../tmem/sqlite_compat.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";
import { recordTelemetryEvent } from "../tmem/telemetry.mjs";
import { runCompactRecallBenchmark } from "../benchmarks/eval_compact_recall.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");

test("inverted tokenizer splits identifiers, paths, and CJK n-grams", () => {
  const toks = tokenizeInvertedTerms(
    "修复 src/auth/jwtGuard.ts 中的 verifyClockSkew 问题；禁止使用全局锁"
  );
  assert.ok(toks.has("jwtguard.ts"));
  assert.ok(toks.has("jwt"));
  assert.ok(toks.has("guard"));
  assert.ok(toks.has("verify"));
  assert.ok(toks.has("clock"));
  assert.ok(toks.has("skew"));
  assert.ok(toks.has("全局"));
  assert.ok(toks.has("局锁"));
  assert.ok(toks.has("全局锁"));
});

test("multi-turn episode inverted search pinpoints turns and supports L0->L3 disclosure", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_ep_inv_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  const store = new ProjectMemoryStore(ws);

  const ep = new SessionEpisode({
    episode_id: "ep_alpha_001",
    session_id: "sess_inv_1",
    compaction_id: "cmp_001",
    trigger: "compaction",
    seq_from: 1,
    seq_to: 18,
    phase_intent: "Design retry queue and fix Redis timeout bug",
    decisions: ["禁止使用全局互斥锁，改用按租户分片的令牌桶"],
    touched_files: ["src/queue/retryWorker.ts", "src/redis/client.ts"],
    touched_symbols: ["scheduleRetryJob", "createTenantLimiter"],
    error_signatures: ["RedisTimeoutError: command timed out after 5000ms"],
    verification_cmds: ["npm test"],
    turns: [
      {
        turn_idx: 1,
        seq_from: 1,
        seq_to: 6,
        user_prompt: "讨论重试队列架构与并发约束",
        assistant_summary: "确认采用按租户分片的令牌桶，避免全局锁瓶颈",
        decisions: ["禁止使用全局互斥锁，改用按租户分片的令牌桶"],
        files: ["src/queue/retryWorker.ts"],
        symbols: ["createTenantLimiter"],
        errors: [],
        commands: [],
      },
      {
        turn_idx: 2,
        seq_from: 7,
        seq_to: 18,
        user_prompt: "Fix RedisTimeoutError in scheduleRetryJob",
        assistant_summary: "Adjusted socketTimeout to 12000ms and verified",
        decisions: [],
        files: ["src/redis/client.ts"],
        symbols: ["scheduleRetryJob"],
        errors: ["RedisTimeoutError: command timed out after 5000ms"],
        commands: ["npm test"],
      },
    ],
    diff_snippets: [
      {
        turn_idx: 2,
        seq: 12,
        file: "src/redis/client.ts",
        tool_name: "edit",
        snippet: "- socketTimeout: 5000\n+ socketTimeout: 12000",
      },
    ],
    steps: [
      {
        seq: 10,
        turn_idx: 2,
        kind: "ENV_EXEC",
        tool_name: "bash",
        command: "npm test",
        exit_code: 1,
        error_signature: "RedisTimeoutError: command timed out after 5000ms",
        output_snippet: "RedisTimeoutError: command timed out",
        output_full:
          "HEAD_LOG\n" +
          "x".repeat(4500) +
          "\nPRUNED_MIDDLE_NEEDLE: shard_key_slot=4096\n" +
          "y".repeat(4500) +
          "\nRedisTimeoutError: command timed out after 5000ms",
      },
      {
        seq: 12,
        turn_idx: 2,
        kind: "MUTATE",
        tool_name: "edit",
        target_file: "src/redis/client.ts",
        symbols_touched: ["scheduleRetryJob"],
        exit_code: 0,
        output_snippet: "Edited src/redis/client.ts",
        output_full: "Edited src/redis/client.ts",
      },
    ],
  });

  store.saveEpisode(ep);

  // Check .tmem/.gitignore excludes episodes/
  const gi = readFileSync(path.join(ws, ".tmem", ".gitignore"), "utf-8");
  assert.ok(gi.includes("episodes/"));

  // Also save a playbook to verify unified search across episodes + playbooks
  store.savePlaybook(
    new TracePlaybook({
      trace_id: "tr_redis_pb",
      agent_source: "dsh",
      problem_intent: "Configure Redis TLS fallback",
      touched_files: ["src/redis/tls.ts"],
      touched_symbols: ["configureRedisTls"],
      confidence: 0.7,
    })
  );

  // 1. Decision Chinese query pinpoints Turn #1
  const decHits = store.searchMemory({ query: "令牌桶 全局互斥锁", roles: ["decision"] });
  assert.equal(decHits.length, 1);
  assert.equal(decHits[0].doc_id, "ep_alpha_001");
  assert.equal(decHits[0].matched_turns[0].turn_idx, 1);

  // 2. Identifier sub-word + pruned raw middle query pinpoints Turn #2
  const rawHits = store.searchMemory({ query: "shard_key_slot 4096" });
  assert.equal(rawHits.length, 1);
  assert.equal(rawHits[0].doc_id, "ep_alpha_001");
  assert.ok(rawHits[0].matched_roles.includes("raw_output"));
  assert.equal(rawHits[0].matched_turns[0].turn_idx, 2);

  // 3. Unified search across both episodes and playbooks
  const allRedisHits = store.searchMemory({ query: "redis", scope: "all", limit: 5 });
  const hitIds = allRedisHits.map((h) => h.doc_id);
  assert.ok(hitIds.includes("ep_alpha_001"));
  assert.ok(hitIds.includes("tr_redis_pb"));

  // 4. Progressive disclosure L0 -> L1 -> L2 -> L3
  const l0 = formatEpisodeProgressiveView(ep, { level: "index" });
  assert.ok(l0.includes("[ep_alpha_001]"));

  const l1 = expandMemoryTarget(ws, { id: "ep_alpha", level: "slice" });
  assert.ok(l1.includes("Key Decisions & Constraints"));
  assert.ok(l1.includes("Turn #1"));
  assert.ok(l1.includes("Turn #2"));

  const l2Turn = expandMemoryTarget(ws, { id: "ep_alpha_001", level: "turn", turn: 1 });
  assert.ok(l2Turn.includes("讨论重试队列架构"));
  assert.ok(!l2Turn.includes("PRUNED_MIDDLE_NEEDLE"));

  const l2Diff = expandMemoryTarget(ws, { id: "ep_alpha_001", level: "diff" });
  assert.ok(l2Diff.includes("socketTimeout: 12000"));

  const l3Raw = expandMemoryTarget(ws, {
    id: "ep_alpha_001",
    level: "raw",
    query: "shard_key_slot",
    max_chars: 12000,
  });
  assert.ok(l3Raw.includes("PRUNED_MIDDLE_NEEDLE: shard_key_slot=4096"));
});

test("Live @deepseek-ai/dsh compact + prune integration with tmem_search & tmem_expand tools", async (t) => {
  const dshEntry = resolveDshEntryJs();
  if (!dshEntry) {
    t.skip("@deepseek-ai/dsh runtime is not installed on this host");
    return;
  }

  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_dsh_compact_live_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(
    path.join(ws, "src", "cache.js"),
    "export function getCacheKey(id) { return 'k:' + id; }\n",
    "utf-8"
  );

  const req = createRequire(pathToFileURL(dshEntry).href);
  const { Context } = req("@deepseek-ai/cordis");
  const dshSession = req("@deepseek-ai/dsh-session");
  const dshPrompt = req("@deepseek-ai/dsh-system-prompt");
  const dshTools = req("@deepseek-ai/dsh-tools");
  const dshExec = req("@deepseek-ai/dsh-tool-executor-native");
  const dshCommands = req("@deepseek-ai/dsh-commands");
  const { compactCheckpointSource, CompactionId } = req("@deepseek-ai/dsh-compaction");
  const tmemPlugin = await import(pathToFileURL(path.join(REPO_ROOT, "tmem", "dsh_plugin.mjs")).href);

  const ctx = new Context();
  ctx.plugin(dshSession.default);
  ctx.plugin(dshPrompt.default);
  ctx.plugin(dshTools.default);
  ctx.plugin(dshExec);
  ctx.plugin(dshCommands.default);
  ctx.plugin(tmemPlugin.default, { workspaceRoot: ws, tmemRoot: REPO_ROOT });

  await new Promise((r) => setTimeout(r, 80));

  // Verify `tmem_search`, `tmem_expand`, and prompt section registered cleanly
  const toolsSvc = ctx.get("tools");
  const spSvc = ctx.get("systemPrompt");
  assert.ok(toolsSvc.get("tmem_search"));
  assert.ok(toolsSvc.get("tmem_expand"));
  const assembly = await spSvc.assemble();
  assert.ok(assembly.sections.some((s) => s.name === "tmem:progressive-memory"));

  const session = ctx.sessions.create("sess_live_compact", {
    meta: { cwd: ws, isSeeded: false, delegationDepth: 0 },
  });

  // Turn 1: Decision & single-file edit (normally trivial in Playbook gate, MUST still archive as Episode!)
  session.append("turn/start", { turn: 1 });
  const u1 = session.append(
    "user/message",
    {
      id: "u_1",
      role: "user",
      content: [
        {
          type: "text",
          text: "修改 src/cache.js 的 getCacheKey，注意禁止在缓存 key 中包含时间戳",
        },
      ],
      source: { kind: "user" },
    },
    { surfaceOp: "append" }
  );
  session.append("tool/call", {
    turn: 1,
    step: 1,
    callId: "call_edit_1",
    name: "edit",
    arguments: JSON.stringify({
      file_path: "src/cache.js",
      old_string: "return 'k:' + id;",
      new_string: "return 'v2:' + String(id);",
    }),
  });
  const editRes1 = session.append(
    "tool/result",
    {
      turn: 1,
      step: 1,
      message: {
        id: "tr_1",
        role: "tool",
        callId: "call_edit_1",
        content: [{ type: "text", text: "Edited src/cache.js" }],
        isError: false,
      },
    },
    { surfaceOp: "append" }
  );

  // A massive tool output that gets pruned by toolResultPruner via compaction/prune
  const hugeLog =
    "START_LOG\n" +
    "a".repeat(4500) +
    "\nCRITICAL_PRUNED_DIAGNOSTIC: cache_namespace_mismatch=v1_vs_v2\n" +
    "b".repeat(4500) +
    "\n[exit code: 0]";
  const callHuge = session.append("tool/call", {
    turn: 1,
    step: 2,
    callId: "call_bash_huge",
    name: "bash",
    arguments: JSON.stringify({ command: "node --test" }),
  });
  const hugeRes = session.append(
    "tool/result",
    {
      turn: 1,
      step: 2,
      message: {
        id: "tr_2",
        role: "tool",
        callId: "call_bash_huge",
        content: [{ type: "text", text: hugeLog }],
        isError: false,
      },
    },
    { surfaceOp: "append" }
  );

  // Trigger compaction/prune just like dsh-compaction-tool-result-pruner does
  session.append("compaction/prune", {
    shadowedRange: { start: hugeRes.seq, end: hugeRes.seq },
    shadowedSeqs: [hugeRes.seq],
    shadowedTokenCount: 2500,
  });
  const prunedReplacement = session.append(
    "tool/result",
    {
      turn: 1,
      step: 2,
      message: {
        id: "tr_2",
        role: "tool",
        callId: "call_bash_huge",
        content: [
          {
            type: "text",
            text: "START_LOG\n\n[... tool result middle pruned ...]\n\n[exit code: 0]",
          },
        ],
        isError: false,
      },
    },
    {
      surfaceOp: { op: "replace", startSeq: hugeRes.seq, endSeq: hugeRes.seq },
      sourceEventSeqs: [hugeRes.seq],
    }
  );

  const asst1 = session.append(
    "assistant/message",
    {
      turn: 1,
      step: 3,
      message: {
        id: "a_1",
        role: "assistant",
        content: [
          {
            type: "text",
            text: "Updated getCacheKey in src/cache.js to use v2 prefix without timestamps.",
          },
        ],
        source: { kind: "model", provider: "deepseek", model: "deepseek-chat" },
      },
      stream: [],
    },
    { surfaceOp: "append" }
  );
  session.append("turn/end", { turn: 1, reason: { kind: "completed" } });

  // Allow microtask/runtime warm-up if any
  await new Promise((r) => setTimeout(r, 40));

  // Verify compaction/prune archived the unpruned middle!
  const store = new ProjectMemoryStore(ws);
  const pruneHits = store.searchMemory({ query: "cache_namespace_mismatch" });
  assert.ok(pruneHits.length >= 1, "Pruned tool middle diagnostic should be searchable");

  // Simulate DSH compaction transaction (`compaction/start` -> `compaction/summary` -> replace `user/message` -> `compaction/end`)
  const cid = CompactionId("cmp_live_01");
  const shadowedSeqs = [u1.seq, editRes1.seq, prunedReplacement.seq, asst1.seq];
  session.append("compaction/start", { compactionId: cid, turn: null });
  const sumEv = session.append("compaction/summary", {
    compactionId: cid,
    summary: [{ type: "text", text: "## Files\n- src/cache.js updated getCacheKey" }],
    shadowedRange: { start: u1.seq, end: asst1.seq },
    shadowedSeqs,
    shadowedTokenCount: 3000,
    provider: "deepseek",
    model: "deepseek-chat",
  });
  session.append(
    "user/message",
    {
      id: "cp_1",
      role: "user",
      content: [{ type: "text", text: "<compacted-summary>Brief summary</compacted-summary>" }],
      source: compactCheckpointSource(cid),
    },
    {
      surfaceOp: { op: "replace", startSeq: u1.seq, endSeq: asst1.seq },
      sourceEventSeqs: [sumEv.seq, ...shadowedSeqs],
    }
  );
  session.append("compaction/end", { compactionId: cid, turn: null });

  await new Promise((r) => setTimeout(r, 40));

  // Verify episode was compiled & indexed!
  const episodes = store.listEpisodes({ sessionId: "sess_live_compact" });
  const mainEp = episodes.find((e) => e.compaction_id === "cmp_live_01");
  assert.ok(mainEp, "Compacted span must be saved as an Episode even for single-file edits");

  // Simulate mid-turn agent/pre-step (step=1, no new user prompt) after compaction
  const runPreStep = async (payload) => {
    const hooks = [...(ctx.events._hooks["agent/pre-step"] || [])];
    const dispatch = async (i) => {
      const entry = hooks[i];
      if (!entry) return { kind: "enter", messages: payload.messages };
      return entry.callback.call(ctx, payload, () => dispatch(i + 1));
    };
    return dispatch(0);
  };

  const mockAgent = { session };
  const pre1 = await runPreStep({
    agent: mockAgent,
    step: 1,
    messages: [],
  });
  const injectedMsgs = pre1.messages || [];
  assert.ok(
    injectedMsgs.some((m) =>
      (m.content?.[0]?.text || "").includes(`<tmem_compacted_index compaction_id="cmp_live_01"`)
    ),
    "Post-compaction pre-step must inject <tmem_compacted_index> anchor"
  );

  // Subsequent step without new compaction must NOT re-inject the same anchor
  const pre2 = await runPreStep({
    agent: mockAgent,
    step: 2,
    messages: [],
  });
  assert.equal((pre2.messages || []).length, 0);

  // Invoke `tmem_search` & `tmem_expand` through real DSH `ToolsService.execute`!
  const ac = new AbortController();
  const searchBatch = await toolsSvc.execute({
    agent: mockAgent,
    signal: ac.signal,
    calls: [
      {
        id: "tc_s1",
        name: "tmem_search",
        arguments: JSON.stringify({
          query: "禁止 时间戳 getCacheKey",
          session_id: "current",
        }),
      },
    ],
  });
  const searchOutText = searchBatch.results[0].result.content[0].text;
  assert.ok(searchOutText.includes(mainEp.episode_id));
  assert.ok(searchOutText.includes("Turn #1"));

  const expandDiffBatch = await toolsSvc.execute({
    agent: mockAgent,
    signal: ac.signal,
    calls: [
      {
        id: "tc_e1",
        name: "tmem_expand",
        arguments: JSON.stringify({
          id: mainEp.episode_id,
          level: "diff",
        }),
      },
    ],
  });
  const diffOutText = expandDiffBatch.results[0].result.content[0].text;
  assert.ok(diffOutText.includes("return 'v2:' + String(id);"));

  // Also test `/tmem search`, `/tmem episodes`, `/tmem expand` slash commands
  const cmdSvc = ctx.get("commands");
  const cmdSearch = await cmdSvc.execute(mockAgent, "/tmem search getCacheKey", [], ac.signal);
  assert.equal(cmdSearch.result.kind, "success");
  assert.ok(cmdSearch.result.text.includes(mainEp.episode_id));

  const cmdExp = await cmdSvc.execute(
    mockAgent,
    `/tmem expand ${mainEp.episode_id} slice`,
    [],
    ac.signal
  );
  assert.equal(cmdExp.result.kind, "success");
  assert.ok(cmdExp.result.text.includes("Key Decisions & Constraints"));
});

test("episode GC evicts expired & over-quota episodes while closing all SQLite connections", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_ep_gc_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  let openCount = 0;
  let closeCount = 0;
  setConnectionTrackingHooks(
    () => {
      openCount += 1;
    },
    () => {
      closeCount += 1;
    }
  );
  t.after(() => setConnectionTrackingHooks(null, null));

  const store = new ProjectMemoryStore(ws);
  const oldIso = new Date(Date.now() - 40 * 86400 * 1000).toISOString();
  const freshIso = new Date().toISOString();

  store.saveEpisode(
    new SessionEpisode({
      episode_id: "ep_expired_old",
      session_id: "s1",
      created_at: oldIso,
      last_accessed_at: oldIso,
      phase_intent: "Old obsolete exploration",
      touched_files: ["src/old.js"],
    }),
    { autoGc: false }
  );

  store.saveEpisode(
    new SessionEpisode({
      episode_id: "ep_recent_keep",
      session_id: "s2",
      created_at: freshIso,
      last_accessed_at: freshIso,
      phase_intent: "Fresh critical session",
      touched_files: ["src/fresh.js"],
    }),
    { autoGc: false }
  );

  const gcStats = store.runEpisodeGc({ maxMb: 200, maxAgeDays: 30 });
  assert.equal(gcStats.expired_pruned, 1);
  assert.equal(gcStats.kept, 1);
  assert.equal(existsSync(path.join(ws, ".tmem", "episodes", "ep_expired_old.json")), false);
  assert.equal(existsSync(path.join(ws, ".tmem", "episodes", "ep_recent_keep.json")), true);
  assert.deepEqual(store.searchMemory({ query: "obsolete" }), []);
  assert.equal(store.searchMemory({ query: "Fresh critical" }).length, 1);

  // CLI smoke check for search / episodes / expand
  const logs = [];
  const rcSearch = cliMain(["search", "Fresh", "--workspace", ws], {
    log: (s) => logs.push(s),
    err: () => {},
  });
  assert.equal(rcSearch, 0);
  assert.ok(logs.join("\n").includes("ep_recent_keep"));

  const rcExpand = cliMain(["expand", "ep_recent_keep", "--level", "slice", "--workspace", ws], {
    log: (s) => logs.push(s),
    err: () => {},
  });
  assert.equal(rcExpand, 0);
  assert.ok(logs.join("\n").includes("Fresh critical session"));

  assert.ok(openCount > 0);
  assert.equal(openCount, closeCount, "All SQLite connections must close cleanly");
});

test("cochange directional confidence avoids hub false positives and suppresses ignored pairs", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_dir_cochange_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  execFileSync("git", ["init"], { cwd: ws, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "t@t.com"], { cwd: ws });
  execFileSync("git", ["config", "user.name", "t"], { cwd: ws });
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "hub.js"), "export const h = 0;\n");
  writeFileSync(path.join(ws, "src", "leaf.js"), "export const l = 0;\n");
  execFileSync("git", ["add", "."], { cwd: ws });
  execFileSync("git", ["commit", "-m", "init"], { cwd: ws, stdio: "ignore" });

  // leaf.js changes 2x with hub.js (100% leaf->hub), while hub.js changes 6x total (only 33% hub->leaf)
  for (let i = 1; i <= 2; i++) {
    writeFileSync(path.join(ws, "src", "hub.js"), `export const h = ${i};\n`);
    writeFileSync(path.join(ws, "src", "leaf.js"), `export const l = ${i};\n`);
    execFileSync("git", ["add", "."], { cwd: ws });
    execFileSync("git", ["commit", "-m", `both ${i}`], { cwd: ws, stdio: "ignore" });
  }
  for (let i = 3; i <= 6; i++) {
    writeFileSync(path.join(ws, "src", "hub.js"), `export const h = ${i};\n`);
    execFileSync("git", ["add", "."], { cwd: ws });
    execFileSync("git", ["commit", "-m", `hub only ${i}`], { cwd: ws, stdio: "ignore" });
  }

  // Editing hub.js should NOT push leaf.js (directional P(leaf|hub) = 2/7 < 0.5)
  assert.deepEqual(findCochangePartnersForFiles(ws, ["src/hub.js"], null, 3), []);

  // Editing leaf.js SHOULD push hub.js (directional P(hub|leaf) = 2/3 >= 0.5)
  const leafPartners = findCochangePartnersForFiles(ws, ["src/leaf.js"], null, 3);
  assert.equal(leafPartners.length, 1);
  assert.equal(leafPartners[0][1], "src/hub.js");

  // After 3 ignored L1 hints for leaf->hub, auto-suppression silences it
  for (let k = 0; k < 3; k++) {
    recordTelemetryEvent(ws, "l1_cochange_hinted", {
      edited_file: "src/leaf.js",
      suggested_files: ["src/hub.js"],
    });
    recordTelemetryEvent(ws, "l1_outcome", {
      edited_file: "src/leaf.js",
      adopted_files: [],
      adopted: false,
      resolution: "ignored",
    });
  }
  assert.deepEqual(findCochangePartnersForFiles(ws, ["src/leaf.js"], null, 3), []);
});

test("Milestone 3 & 4: AST signature_match guard degradation, trailing unverified edit pruning, and manual capture episode archival", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_m3_m4_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));

  mkdirSync(path.join(ws, "src"), { recursive: true });
  mkdirSync(path.join(ws, "tests"), { recursive: true });
  const pyFile = path.join(ws, "src", "pricing.py");
  const tsFile = path.join(ws, "src", "auth.ts");
  writeFileSync(
    pyFile,
    "class PricingEngine:\n    def calculate(self, subtotal: float, tax_rate: float = 0.08) -> float:\n        return subtotal * (1 + tax_rate)\n",
    "utf-8"
  );
  writeFileSync(
    tsFile,
    "export async function verifySession<T>(token: string, strictMode: boolean = true): Promise<boolean> {\n  return Boolean(token) && strictMode;\n}\n",
    "utf-8"
  );

  // 1. AST signature extraction & matching across Python and TypeScript
  const pyContent = readFileSync(pyFile, "utf-8");
  const tsContent = readFileSync(tsFile, "utf-8");
  assert.equal(
    extractSymbolSignature(pyContent, "src/pricing.py", "PricingEngine.calculate"),
    "calculate(subtotal,tax_rate)"
  );
  assert.equal(
    extractSymbolSignature(tsContent, "src/auth.ts", "verifySession"),
    "verifySession(token,strictMode)"
  );
  assert.equal(
    checkSymbolSignatureMatch(
      pyContent,
      "src/pricing.py",
      "PricingEngine.calculate:calculate(subtotal,tax_rate)"
    ),
    true
  );
  assert.equal(
    checkSymbolSignatureMatch(
      tsContent,
      "src/auth.ts",
      "verifySession:verifySession(token,strictMode)"
    ),
    true
  );

  // 2. Slicer trailing unverified edit pruning + signature_match guard generation
  const slicer = new BackwardTraceSlicer();
  const sess = new ParsedSession({
    agent_name: "dsh",
    session_id: "sess_m4_slicer",
    workspace_root: ws,
    user_intents: ["Fix pricing calculation and auth session verification"],
    actions: [
      new ActionNode({
        step_idx: 1,
        kind: ActionKind.MUTATE,
        tool_name: "edit",
        target_file: "src/pricing.py",
        symbols_touched: ["PricingEngine.calculate"],
      }),
      new ActionNode({
        step_idx: 2,
        kind: ActionKind.MUTATE,
        tool_name: "edit",
        target_file: "src/auth.ts",
        symbols_touched: ["verifySession"],
      }),
      new ActionNode({
        step_idx: 3,
        kind: ActionKind.ENV_EXEC,
        tool_name: "bash",
        command: "pytest tests/test_pricing.py",
        exit_code: 0,
      }),
      // Trailing unverified experimental mutation after last passing verify (step 3)
      new ActionNode({
        step_idx: 4,
        kind: ActionKind.MUTATE,
        tool_name: "edit",
        target_file: "src/unverified_trailing.py",
        symbols_touched: ["broken_experiment"],
      }),
    ],
  });

  const pb = slicer.sliceSession(sess, false, { pruneTrailingUnverified: true });
  assert.ok(pb);
  assert.deepEqual(pb.touched_files, ["src/auth.ts", "src/pricing.py"]);
  assert.ok(
    pb.pitfalls.some((p) => p.includes("Pruned unverified trailing edit on 'src/unverified_trailing.py'"))
  );
  assert.ok(
    pb.guards.some(
      (g) =>
        g.probe_type === "signature_match" &&
        g.target === "src/pricing.py" &&
        g.expected === "PricingEngine.calculate:calculate(subtotal,tax_rate)"
    )
  );
  assert.equal(evaluateGuards(pb, ws).l0_valid, true);

  // Breaking function signature change degrades playbook from L0_FULL to L1_TOPOLOGY
  writeFileSync(
    pyFile,
    "class PricingEngine:\n    def calculate(self, subtotal: float, tax_rate: float, currency: str) -> float:\n        return subtotal\n",
    "utf-8"
  );
  const degradedEv = evaluateGuards(pb, ws);
  assert.equal(degradedEv.l0_valid, false);
  assert.equal(degradedEv.effective_tier, "L1_TOPOLOGY");
  assert.ok(
    degradedEv.failed_guards.includes(
      "drifted_signature:src/pricing.py#PricingEngine.calculate:calculate(subtotal,tax_rate)"
    )
  );

  // 3. Manual `executeCapture` archives an Episode to `.tmem/episodes/` even when trivial
  const tPath = path.join(ws, "single_file_manual.v4.jsonl");
  const events = [
    { version: 4, id: "sess_manual_ep", createdAt: 1731225600000 },
    {
      type: "user/message",
      seq: 1,
      data: {
        role: "user",
        content: [{ type: "text", text: "调整 src/auth.ts 的注释与默认超时说明" }],
        source: { kind: "user" },
      },
    },
    {
      type: "tool/call",
      seq: 2,
      data: {
        callId: "c1",
        name: "edit",
        arguments: JSON.stringify({ file_path: "src/auth.ts", new_string: "// timeout=30s" }),
      },
    },
    {
      type: "tool/result",
      seq: 3,
      data: {
        message: {
          callId: "c1",
          content: [{ type: "text", text: "Updated src/auth.ts" }],
          isError: false,
        },
      },
    },
  ];
  writeFileSync(tPath, events.map((e) => JSON.stringify(e)).join("\n"), "utf-8");

  const capOut = executeCapture({
    workspaceRoot: ws,
    agentName: "dsh",
    transcriptPath: tPath,
    sessionId: "sess_manual_ep",
  });
  assert.equal(capOut.status, "skipped_trivial");
  assert.ok(capOut.episode_id && capOut.episode_id.startsWith("ep_"));

  const store = new ProjectMemoryStore(ws);
  const manualHits = store.searchMemory({ query: "默认超时说明 auth.ts" });
  assert.ok(manualHits.some((h) => h.doc_id === capOut.episode_id));
});

test("Milestone 5: Compaction-Recall Benchmark achieves 100% progressive detail recall with >80% token savings", () => {
  const report = runCompactRecallBenchmark();
  assert.equal(report.vanilla_compact_recall_pct, 0);
  assert.equal(report.tmem_progressive_recall_pct, 100);
  assert.equal(report.multi_intent_sub_episodes_count, 2);
  assert.ok(report.session_timeline_phases_count >= 3);
  assert.equal(report.trailing_unverified_edit_pruned, true);
  assert.equal(report.ast_signature_guard_verified, true);
  assert.ok(report.token_economy.l0_token_savings_pct >= 80);
  assert.ok(report.latency.avg_search_latency_ms < 25);
  assert.ok(report.latency.avg_expand_latency_ms < 25);
});
