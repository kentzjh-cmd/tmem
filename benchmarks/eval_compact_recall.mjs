#!/usr/bin/env node
/**
 * Milestone 5: Context-Compaction + Inverted-Index Progressive Recall Benchmark (Node.js ESM).
 *
 * Quantifies detail recovery accuracy, token savings, and retrieval latency comparing:
 * - Baseline (Vanilla Compaction Summary + Tool-Result Middle Pruning)
 * - `tmem` Integrated Compaction Slicing + Multi-Dimensional Inverted Index (L0 -> L1 -> L2 -> L3)
 *
 * Scenarios evaluated:
 * 1. Scenario A: Early architectural decision & constraint recall after compaction
 * 2. Scenario B: Middle-pruned 18KB tool log root-cause recovery (`compaction/prune`)
 * 3. Scenario C: Multi-compaction session timeline chaining & multi-intent sub-episode segmentation
 * 4. Scenario D: Exact code diff recovery + post-verify trailing edit pruning & `signature_match` guard
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expandMemoryTarget } from "../tmem/dsh_tools.mjs";
import { evaluateGuards } from "../tmem/guards.mjs";
import { handleDshHook, handleDshSessionEvent } from "../tmem/hooks/index.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

function createMockLiveSession(sessionId, workspaceRoot, initialEvents = []) {
  const events = [];
  const session = {
    id: sessionId,
    header: { id: sessionId, cwd: workspaceRoot, version: 4 },
    get seq() {
      return events.length;
    },
    eventAt(seq) {
      return events[seq];
    },
    append(type, data, opts = {}) {
      const ev = {
        seq: events.length,
        time: Date.now(),
        type,
        data,
        ...(opts.surfaceOp ? { surfaceOp: opts.surfaceOp } : {}),
        ...(opts.sourceEventSeqs ? { sourceEventSeqs: opts.sourceEventSeqs } : {}),
      };
      events.push(ev);
      return ev;
    },
  };
  for (const item of initialEvents) {
    session.append(item.type, item.data, item);
  }
  return session;
}

function estimateTokens(text) {
  return Math.max(1, Math.ceil(String(text || "").length / 4));
}

export function runCompactRecallBenchmark() {
  const tmpRoot = mkdtempSync(path.join(os.tmpdir(), "tmem_compact_recall_"));
  try {
    const ws = path.join(tmpRoot, "repo");
    mkdirSync(path.join(ws, "src", "auth"), { recursive: true });
    mkdirSync(path.join(ws, "src", "billing"), { recursive: true });
    mkdirSync(path.join(ws, "src", "consensus"), { recursive: true });
    mkdirSync(path.join(ws, "tests"), { recursive: true });

    writeFileSync(
      path.join(ws, "src", "auth", "jwt_guard.ts"),
      "export function verifyJwtToken(token: string, leewaySec: number = 30): boolean {\n  return Boolean(token) && leewaySec >= 0;\n}\n",
      "utf-8"
    );
    writeFileSync(
      path.join(ws, "src", "billing", "invoice.ts"),
      "export function computeInvoiceTotal(cents: number, taxRate: number): number {\n  return Math.round(cents * (1 + taxRate));\n}\n",
      "utf-8"
    );
    writeFileSync(
      path.join(ws, "src", "consensus", "raft_node.ts"),
      "export function advanceEpoch(epoch: number): number {\n  return epoch + 1;\n}\n",
      "utf-8"
    );
    writeFileSync(
      path.join(ws, "tests", "test_suite.ts"),
      "export function runSuite(): boolean { return true; }\n",
      "utf-8"
    );

    const sessId = "sess_compact_bench";
    const session = createMockLiveSession(sessId, ws);

    // =========================================================================
    // Phase 1 (Turns 1..4): Multi-intent span (Auth JWT + Billing Invoice)
    // =========================================================================
    session.append("turn/start", { turn: 1 });
    session.append("user/message", {
      role: "user",
      content: [
        {
          type: "text",
          text: "重构 src/auth/jwt_guard.ts：决定统一采用 leewaySec=30 秒时钟偏移容忍，禁止引入外部 redis 会话缓存",
        },
      ],
      source: { kind: "user" },
    });
    session.append("tool/call", {
      callId: "c1",
      name: "read",
      arguments: JSON.stringify({ file_path: "src/auth/jwt_guard.ts" }),
    });
    session.append("tool/result", {
      message: {
        callId: "c1",
        content: [{ type: "text", text: "export function verifyJwtToken(token: string) {}" }],
        isError: false,
      },
    });
    session.append("tool/call", {
      callId: "c2",
      name: "edit",
      arguments: JSON.stringify({
        file_path: "src/auth/jwt_guard.ts",
        old_string: "export function verifyJwtToken(token: string)",
        new_string: "export function verifyJwtToken(token: string, leewaySec: number = 30)",
      }),
    });
    session.append("tool/result", {
      message: {
        callId: "c2",
        content: [{ type: "text", text: "Updated src/auth/jwt_guard.ts" }],
        isError: false,
      },
    });
    session.append("turn/end", { turn: 1 });

    session.append("turn/start", { turn: 2 });
    session.append("user/message", {
      role: "user",
      content: [{ type: "text", text: "运行鉴权测试并验证 leewaySec" }],
      source: { kind: "user" },
    });
    session.append("tool/call", {
      callId: "c3",
      name: "bash",
      arguments: JSON.stringify({ command: "npm test tests/test_suite.ts" }),
    });
    session.append("tool/result", {
      message: {
        callId: "c3",
        content: [{ type: "text", text: "PASS tests/test_suite.ts\n[exit code: 0]" }],
        isError: false,
      },
    });
    session.append("assistant/message", {
      turn: 2,
      message: {
        role: "assistant",
        content: [
          {
            type: "text",
            text: "已完成鉴权模块改造，必须保持 leewaySec=30 且不使用外部 redis。",
          },
        ],
      },
    });
    session.append("turn/end", { turn: 2 });

    // Turn 3..4: Disjoint file cluster (src/billing/invoice.ts) in the same compaction span
    session.append("turn/start", { turn: 3 });
    session.append("user/message", {
      role: "user",
      content: [
        {
          type: "text",
          text: "现在修复 src/billing/invoice.ts 的税率舍入误差，约定金额必须以美分整数 cents 计算",
        },
      ],
      source: { kind: "user" },
    });
    session.append("tool/call", {
      callId: "c4",
      name: "edit",
      arguments: JSON.stringify({
        file_path: "src/billing/invoice.ts",
        old_string: "return cents * (1 + taxRate);",
        new_string: "return Math.round(cents * (1 + taxRate));",
      }),
    });
    session.append("tool/result", {
      message: {
        callId: "c4",
        content: [{ type: "text", text: "Updated src/billing/invoice.ts" }],
        isError: false,
      },
    });
    session.append("turn/end", { turn: 3 });

    session.append("turn/start", { turn: 4 });
    session.append("user/message", {
      role: "user",
      content: [{ type: "text", text: "验证账单模块测试" }],
      source: { kind: "user" },
    });
    session.append("tool/call", {
      callId: "c5",
      name: "bash",
      arguments: JSON.stringify({ command: "npm test tests/test_suite.ts" }),
    });
    session.append("tool/result", {
      message: {
        callId: "c5",
        content: [{ type: "text", text: "PASS billing tests\n[exit code: 0]" }],
        isError: false,
      },
    });
    session.append("turn/end", { turn: 4 });

    const phase1EndSeq = session.seq - 1;
    const phase1Seqs = Array.from({ length: phase1EndSeq + 1 }, (_, i) => i);

    // Coarse Vanilla Compaction Summary #1 (loses leewaySec=30, redis prohibition, and exact diff)
    const vanillaSummary1 =
      "User and assistant updated authentication and billing modules and ran tests successfully.";
    const sumEv1 = session.append("compaction/summary", {
      compactionId: "cmp_bench_01",
      summary: [{ type: "text", text: vanillaSummary1 }],
      shadowedRange: { start: 0, end: phase1EndSeq },
      shadowedSeqs: phase1Seqs,
    });
    const endEv1 = session.append("compaction/end", {
      compactionId: "cmp_bench_01",
    });

    const compactRes1 = handleDshSessionEvent("compaction_committed", {
      session,
      summaryEvent: sumEv1,
      endEvent: endEv1,
      workspaceRoot: ws,
    });

    // =========================================================================
    // Phase 2 (Turn 5): 18KB Pruned Tool Output (`compaction/prune`)
    // =========================================================================
    session.append("turn/start", { turn: 5 });
    session.append("user/message", {
      role: "user",
      content: [{ type: "text", text: "排查 Raft 节点共识状态同步异常" }],
      source: { kind: "user" },
    });
    const callPrune = session.append("tool/call", {
      callId: "c_raft_log",
      name: "bash",
      arguments: JSON.stringify({ command: "npm test src/consensus/raft_node.ts" }),
    });
    const prefixLog = "INFO: consensus tick ok\n".repeat(380);
    const hiddenRootCause =
      "ConsensusFault: shard_epoch_drift=4096 in src/consensus/raft_node.ts:218 (quorum_lease_expired)\n";
    const suffixLog = "INFO: shutting down test harness\n".repeat(380) + "[exit code: 1]";
    const full18kOutput = prefixLog + hiddenRootCause + suffixLog;
    const prunedSurfaceOutput =
      prefixLog.slice(0, 2000) +
      "\n[... tool result middle pruned ...]\n" +
      suffixLog.slice(-2000);

    const resPrune = session.append(
      "tool/result",
      {
        message: {
          id: "tr_raft",
          callId: "c_raft_log",
          content: [{ type: "text", text: full18kOutput }],
          isError: true,
        },
      },
      { sourceEventSeqs: [callPrune.seq] }
    );
    const pruneEv = session.append("compaction/prune", {
      shadowedRange: { start: resPrune.seq, end: resPrune.seq },
      shadowedSeqs: [resPrune.seq],
    });
    handleDshSessionEvent("compaction_prune", {
      session,
      pruneEvent: pruneEv,
      workspaceRoot: ws,
    });

    // Follow-up fix in Turn 5 + passing test + trailing unverified edit
    writeFileSync(
      path.join(ws, "src", "consensus", "raft_node.ts"),
      "export function advanceEpoch(epoch: number): number {\n  return Math.max(0, epoch) + 1;\n}\n",
      "utf-8"
    );
    session.append("tool/call", {
      callId: "c_raft_fix",
      name: "edit",
      arguments: JSON.stringify({
        file_path: "src/consensus/raft_node.ts",
        old_string: "return epoch + 1;",
        new_string: "return Math.max(0, epoch) + 1;",
      }),
    });
    session.append("tool/result", {
      message: {
        callId: "c_raft_fix",
        content: [{ type: "text", text: "Edited src/consensus/raft_node.ts" }],
        isError: false,
      },
    });
    session.append("tool/call", {
      callId: "c_raft_verify",
      name: "bash",
      arguments: JSON.stringify({ command: "npm test src/consensus/raft_node.ts" }),
    });
    session.append("tool/result", {
      message: {
        callId: "c_raft_verify",
        content: [{ type: "text", text: "PASS consensus suite\n[exit code: 0]" }],
        isError: false,
      },
    });
    // Trailing unverified experimental edit AFTER the last passing verify
    session.append("tool/call", {
      callId: "c_dangling_exp",
      name: "edit",
      arguments: JSON.stringify({
        file_path: "src/consensus/experimental_scratch.ts",
        new_string: "export const UNVERIFIED_FLAG = true;",
      }),
    });
    session.append("tool/result", {
      message: {
        callId: "c_dangling_exp",
        content: [{ type: "text", text: "Edited experimental_scratch.ts" }],
        isError: false,
      },
    });
    session.append("turn/end", { turn: 5 });

    const phase2StartSeq = endEv1.seq + 1;
    const phase2EndSeq = session.seq - 1;
    const phase2Seqs = [];
    for (let s = phase2StartSeq; s <= phase2EndSeq; s++) phase2Seqs.push(s);

    const vanillaSummary2 = "Investigated and fixed consensus test issue.";
    const sumEv2 = session.append("compaction/summary", {
      compactionId: "cmp_bench_02",
      summary: [{ type: "text", text: vanillaSummary2 }],
      shadowedRange: { start: phase2StartSeq, end: phase2EndSeq },
      shadowedSeqs: phase2Seqs,
    });
    const endEv2 = session.append("compaction/end", {
      compactionId: "cmp_bench_02",
    });

    const compactRes2 = handleDshSessionEvent("compaction_committed", {
      session,
      summaryEvent: sumEv2,
      endEvent: endEv2,
      workspaceRoot: ws,
    });

    // =========================================================================
    // Evaluate Recall, Latency, Timeline Chaining, Slicer Pruning & Guards
    // =========================================================================
    const store = new ProjectMemoryStore(ws);
    const searchLatencies = [];
    const expandLatencies = [];

    const timedSearch = (params) => {
      const t0 = performance.now();
      const hits = store.searchMemory(params);
      searchLatencies.push(performance.now() - t0);
      return hits;
    };

    const timedExpand = (params) => {
      const t0 = performance.now();
      const out = expandMemoryTarget(ws, params);
      expandLatencies.push(performance.now() - t0);
      return out;
    };

    const vanillaAvailableSurface = `${vanillaSummary1}\n${vanillaSummary2}\n${prunedSurfaceOutput}`;

    // Probe 1: Early constraint recall ("leewaySec=30" & "禁止引入外部 redis")
    const hitsA = timedSearch({ query: "禁止引入外部 redis leewaySec" });
    const expandA = hitsA.length > 0 ? timedExpand({ id: hitsA[0].doc_id, level: "slice" }) : "";
    const vanillaHasA =
      vanillaAvailableSurface.includes("leewaySec=30") &&
      vanillaAvailableSurface.includes("redis");
    const tmemHasA =
      expandA.includes("leewaySec=30") && expandA.includes("redis");

    // Probe 2: Pruned middle log root-cause recall ("shard_epoch_drift=4096")
    const hitsB = timedSearch({ query: "shard_epoch_drift quorum_lease_expired" });
    const expandB =
      hitsB.length > 0
        ? timedExpand({ id: hitsB[0].doc_id, level: "raw", query: "shard_epoch_drift" })
        : "";
    const vanillaHasB = vanillaAvailableSurface.includes("shard_epoch_drift=4096");
    const tmemHasB = expandB.includes("shard_epoch_drift=4096");

    // Probe 3: Multi-intent sub-episode segmentation & timeline chaining
    const timeline = store.getSessionTimeline(sessId);
    const segmentedCount = (compactRes1.episode_ids || []).length;
    const hitsBilling = timedSearch({ query: "美分整数 cents computeInvoiceTotal" });
    const expandBilling =
      hitsBilling.length > 0 ? timedExpand({ id: hitsBilling[0].doc_id, level: "turn" }) : "";
    const vanillaHasC = vanillaAvailableSurface.includes("cents");
    const tmemHasC =
      segmentedCount === 2 &&
      timeline.length >= 3 &&
      Boolean(timeline[timeline.length - 1].prev_episode_id) &&
      expandBilling.includes("cents");

    // Probe 4: Exact diff recovery + post-verify dangling edit pruning + signature_match guard
    const expandDiff =
      hitsA.length > 0
        ? timedExpand({ id: hitsA[0].doc_id, level: "diff", file: "jwt_guard.ts" })
        : "";
    const vanillaHasD = vanillaAvailableSurface.includes("leewaySec: number = 30");
    const playbooks = store.getAllProjectPlaybooks();
    const raftPb = playbooks.find((p) => p.touched_files.includes("src/consensus/raft_node.ts"));
    const trailingPrunedFromL0 =
      raftPb &&
      !raftPb.touched_files.includes("src/consensus/experimental_scratch.ts") &&
      raftPb.pitfalls.some((p) => p.includes("experimental_scratch.ts"));
    const hasSigGuard =
      raftPb && raftPb.guards.some((g) => g.probe_type === "signature_match");
    const guardPass = raftPb ? evaluateGuards(raftPb, ws).l0_valid : false;
    const tmemHasD =
      expandDiff.includes("leewaySec: number = 30") &&
      Boolean(trailingPrunedFromL0) &&
      Boolean(hasSigGuard) &&
      guardPass;

    // Pre-step L0 notice token footprint vs full uncompacted history
    const preStepHook = handleDshHook("pre_invocation", {
      workspaceRoot: ws,
      sessionId: sessId,
      step: 1,
      lastUserInput: "继续检查共识模块与鉴权配置",
      compactedCheckpoints: ["cmp_bench_01", "cmp_bench_02"],
    });
    const l0InjectedText = preStepHook.injectedContext || compactRes2.noticeText || "";

    const allEventsChars = Array.from({ length: session.seq }, (_, i) =>
      JSON.stringify(session.eventAt(i))
    ).join("\n").length;
    const uncompactedTokensEst = Math.ceil(allEventsChars / 4);
    const l0InjectedTokensEst = estimateTokens(l0InjectedText);
    const onDemandExpandedTokensEst =
      l0InjectedTokensEst + estimateTokens(expandA) + estimateTokens(expandDiff);

    const probes = [
      {
        id: "A_early_decision_constraint",
        vanilla_recalled: vanillaHasA,
        tmem_recalled: tmemHasA,
      },
      {
        id: "B_middle_pruned_18kb_log_root_cause",
        vanilla_recalled: vanillaHasB,
        tmem_recalled: tmemHasB,
      },
      {
        id: "C_multi_intent_segmentation_and_timeline",
        vanilla_recalled: vanillaHasC,
        tmem_recalled: tmemHasC,
      },
      {
        id: "D_exact_diff_and_slicer_guard_verification",
        vanilla_recalled: vanillaHasD,
        tmem_recalled: tmemHasD,
      },
    ];

    const vanillaScore =
      (probes.filter((p) => p.vanilla_recalled).length / probes.length) * 100;
    const tmemScore =
      (probes.filter((p) => p.tmem_recalled).length / probes.length) * 100;

    const avgSearchMs =
      Math.round(
        (searchLatencies.reduce((a, b) => a + b, 0) / Math.max(1, searchLatencies.length)) * 100
      ) / 100;
    const avgExpandMs =
      Math.round(
        (expandLatencies.reduce((a, b) => a + b, 0) / Math.max(1, expandLatencies.length)) * 100
      ) / 100;

    return {
      total_probes: probes.length,
      vanilla_compact_recall_pct: Math.round(vanillaScore * 10) / 10,
      tmem_progressive_recall_pct: Math.round(tmemScore * 10) / 10,
      multi_intent_sub_episodes_count: segmentedCount,
      session_timeline_phases_count: timeline.length,
      trailing_unverified_edit_pruned: Boolean(trailingPrunedFromL0),
      ast_signature_guard_verified: Boolean(hasSigGuard && guardPass),
      token_economy: {
        full_uncompacted_tokens_est: uncompactedTokensEst,
        l0_index_injected_tokens_est: l0InjectedTokensEst,
        l0_plus_targeted_expand_tokens_est: onDemandExpandedTokensEst,
        l0_token_savings_pct:
          Math.round((1 - l0InjectedTokensEst / uncompactedTokensEst) * 1000) / 10,
        on_demand_token_savings_pct:
          Math.round((1 - onDemandExpandedTokensEst / uncompactedTokensEst) * 1000) / 10,
      },
      latency: {
        avg_search_latency_ms: avgSearchMs,
        avg_expand_latency_ms: avgExpandMs,
      },
      probes,
    };
  } finally {
    rmSync(tmpRoot, { recursive: true, force: true });
  }
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  const report = runCompactRecallBenchmark();
  console.log(JSON.stringify(report, null, 2));
}
