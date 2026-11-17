/**
 * Expanded 50-Trajectory Plugin Effectiveness & Scale Validation Benchmark (Node.js ESM).
 *
 * Validates the full `tmem` plugin runtime (BackwardTraceSlicer -> ProjectMemoryStore
 * SQLite/FTS5 + MEMORIES.md -> `handleDshHook` runtime interception & telemetry)
 * across the expanded 50-trajectory dataset (`benchmarks/gold/gold_annotations_50.json`).
 */

import { existsSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DshAdapter, SweBenchAdapter } from "../tmem/adapters/index.mjs";
import { handleDshHook } from "../tmem/hooks/index.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";
import { computeTelemetrySummary } from "../tmem/telemetry.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "..");

export const NEGATIVE_PROMPTS = [
  "How to prepare authentic Italian carbonara with guanciale and pecorino",
  "Deploy high-availability Kubernetes cluster on AWS using Terraform and EKS",
  "Resolve lifetime elision and borrow checker conflict in Rust async trait",
  "Configure Nginx reverse proxy with Let's Encrypt automated TLS certificates",
  "Train a transformer language model with DeepSpeed pipeline parallelism",
  "Docker compose configuration for PostgreSQL read-write replica pool",
  "Optimize React virtualized table scrolling with IntersectionObserver",
  "Fix Gradle Kotlin DSL Android build flavor manifest merger error",
  "请帮我看看这段代码哪里可以优化一下",
  "hi, are you ready?",
];

export function runPluginScaleValidation(goldPath = null) {
  const targetGold = path.resolve(
    goldPath || path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations_50.json")
  );
  const entries = JSON.parse(readFileSync(targetGold, "utf-8"));

  const swe = new SweBenchAdapter();
  const dsh = new DshAdapter();
  const slicer = new BackwardTraceSlicer();

  const tmpDir = mkdtempSync(path.join(os.tmpdir(), "tmem_plugin_scale_"));
  try {
    const ws = path.join(tmpDir, "repo");
    mkdirSync(ws, { recursive: true });
    const store = new ProjectMemoryStore(ws);

    const compiled = [];
    let rawStepsTotal = 0;
    let l0StepsTotal = 0;
    let rawCharsTotal = 0;
    let l0CharsTotal = 0;
    const sliceTimesMs = [];
    let l2PairsTotal = 0;
    let nonTrivialCount = 0;

    for (const entry of entries) {
      const adapter = entry.agent_format === "dsh" ? dsh : swe;
      const session = adapter.parseTranscript(
        path.join(WORKSPACE_ROOT, entry.trajectory_file),
        ws
      );
      const t0 = performance.now();
      const pb = slicer.sliceSession(session, false);
      const dtMs = performance.now() - t0;
      sliceTimesMs.push(dtMs);

      rawStepsTotal += session.actions.length;
      if (!pb) continue;

      for (const tf of pb.touched_files) {
        const fpath = path.join(ws, tf);
        mkdirSync(path.dirname(fpath), { recursive: true });
        if (!existsSync(fpath)) {
          writeFileSync(fpath, "# stub file for guard validation\n", "utf-8");
        }
      }

      const rawTrace = slicer.buildRawTrace(session, pb);
      const rawPath = store.raw_store.saveRawTrace(rawTrace);
      store.savePlaybook(pb, { targetScope: "project", mergeSimilar: false });

      l0StepsTotal += pb.l0_critical_path.length;
      rawCharsTotal += statSync(rawPath).size;
      l0CharsTotal += JSON.stringify(pb.l0_critical_path).length;
      l2PairsTotal += pb.l2_causal_repairs.length;
      if (!pb.is_trivial) nonTrivialCount += 1;

      compiled.push({
        instance_id: entry.instance_id,
        session,
        playbook: pb,
      });
    }

    const tSync0 = performance.now();
    store.syncMemoriesMarkdown();
    const memSyncMs = performance.now() - tSync0;

    // 2. 50-Trace Scale Stress Retrieval
    let hitsAt1 = 0;
    let hitsAt3 = 0;
    const rrs = [];
    let validQueries = 0;
    for (const item of compiled) {
      const { session, playbook: pb } = item;
      const queryStr = (session.user_intents[0] || "").trim().slice(0, 220);
      if (!queryStr) continue;
      validQueries += 1;
      const res = store.cascadingLookup({ queryText: queryStr, limit: 5 });
      const ranked = res.map((r) => r.playbook.trace_id);
      const idx = ranked.indexOf(pb.trace_id);
      const rank = idx >= 0 ? idx + 1 : 999;
      if (rank === 1) hitsAt1 += 1;
      if (rank <= 3) hitsAt3 += 1;
      rrs.push(rank <= 10 ? 1.0 / rank : 0.0);
    }

    let negFp = 0;
    for (const nq of NEGATIVE_PROMPTS) {
      const res = store.cascadingLookup({ queryText: nq, limit: 3 });
      if (res.some((r) => r.l0_valid || r.playbook.confidence > 0.6)) {
        negFp += 1;
      }
    }

    // 3. `handleDshHook` Runtime Interception & Telemetry Validation
    const hookLatenciesMs = [];

    // 3a. Negative Holdout Noise Silence Test on `pre_step`
    let preStepNegInjected = 0;
    for (let idx = 0; idx < NEGATIVE_PROMPTS.length; idx++) {
      const sid = `neg_sess_${idx}`;
      const t0 = performance.now();
      const out = handleDshHook("pre_step", {
        workspaceRoot: ws,
        sessionId: sid,
        isNewTurn: true,
        userMessage: NEGATIVE_PROMPTS[idx],
      });
      hookLatenciesMs.push(performance.now() - t0);
      if ((out.injectedContext || "").includes("<tmem_speculative_memory>")) {
        preStepNegInjected += 1;
      }
    }

    // 3b. Pre-step Explicit Anchor Recall
    let anchorTested = 0;
    let anchorHits = 0;
    for (let idx = 0; idx < compiled.length; idx++) {
      const pb = compiled[idx].playbook;
      const codeTargets = pb.touched_files.filter((f) => f.includes("/") && !f.startsWith("."));
      if (codeTargets.length === 0) continue;
      anchorTested += 1;
      const sid = `anchor_sess_${idx}`;
      const prompt = `Please inspect ${codeTargets[0]} and resolve: ${pb.problem_intent.slice(0, 120)}`;
      const t0 = performance.now();
      const out = handleDshHook("pre_step", {
        workspaceRoot: ws,
        sessionId: sid,
        isNewTurn: true,
        userMessage: prompt,
      });
      hookLatenciesMs.push(performance.now() - t0);
      const ctx = out.injectedContext || "";
      if (ctx.includes(pb.trace_id) || ctx.includes(codeTargets[0])) {
        anchorHits += 1;
      }
    }

    // 3c. Post-tool L2 Causal Error Interception + Verification Pass Resolution
    let l2Tested = 0;
    let l2Intercepted = 0;
    const l2Cases = [];
    for (const item of compiled) {
      for (const pair of item.playbook.l2_causal_repairs) {
        l2Cases.push([item.playbook, pair]);
      }
    }
    for (let idx = 0; idx < l2Cases.length; idx++) {
      const [, pair] = l2Cases[idx];
      l2Tested += 1;
      const sid = `l2_replay_${idx}`;
      const t0 = performance.now();
      const failOut = handleDshHook("post_tool_use", {
        workspaceRoot: ws,
        sessionId: sid,
        toolName: "run_command",
        args: { CommandLine: pair.failed_command },
        exitCode: 1,
        isError: true,
        resultText: `FAILED: ${pair.error_signature}\n[exit code: 1]`,
      });
      hookLatenciesMs.push(performance.now() - t0);
      const ctx = failOut.additionalContext || failOut.injectedContext || "";
      if (ctx.includes("<tmem_speculative_memory>") && ctx.includes("- L2 Causal Repair:")) {
        l2Intercepted += 1;
        const t1 = performance.now();
        handleDshHook("post_tool_use", {
          workspaceRoot: ws,
          sessionId: sid,
          toolName: "run_command",
          args: { CommandLine: pair.verification_cmd },
          exitCode: 0,
          isError: false,
          resultText: "OK\n[exit code: 0]",
        });
        hookLatenciesMs.push(performance.now() - t1);
      }
    }

    // 3d. Post-tool L1 Co-Change Topology Interception on Multi-file Playbooks
    let l1Tested = 0;
    let l1Intercepted = 0;
    for (let idx = 0; idx < compiled.length; idx++) {
      const pb = compiled[idx].playbook;
      if (!pb.l1_cochange_topology?.length || !pb.touched_files?.length) continue;
      l1Tested += 1;
      const sid = `l1_replay_${idx}`;
      const target = pb.touched_files[0];
      const t0 = performance.now();
      const editOut = handleDshHook("post_tool_use", {
        workspaceRoot: ws,
        sessionId: sid,
        toolName: "edit_file",
        args: { TargetFile: target },
        exitCode: 0,
        isError: false,
        resultText: `Edited ${target}`,
      });
      hookLatenciesMs.push(performance.now() - t0);
      if (
        (editOut.additionalContext || editOut.injectedContext || "").includes(
          "<tmem_cochange_hint>"
        )
      ) {
        l1Intercepted += 1;
      }
    }

    const telemetry = computeTelemetrySummary(ws);
    const r1 = (v) => Math.round(v * 10) / 10;
    const r2 = (v) => Math.round(v * 100) / 100;
    const r3 = (v) => Math.round(v * 1000) / 1000;

    return {
      corpus: {
        total_input_trajectories: entries.length,
        compiled_playbooks: compiled.length,
        non_trivial_playbooks: nonTrivialCount,
        l2_causal_pairs_extracted: l2PairsTotal,
        raw_steps_total: rawStepsTotal,
        l0_steps_total: l0StepsTotal,
        step_compression_pct: rawStepsTotal
          ? r1((1.0 - l0StepsTotal / rawStepsTotal) * 100)
          : 0.0,
        char_compression_pct: rawCharsTotal
          ? r1((1.0 - l0CharsTotal / rawCharsTotal) * 100)
          : 0.0,
        avg_slice_latency_ms: sliceTimesMs.length
          ? r2(sliceTimesMs.reduce((a, b) => a + b, 0) / sliceTimesMs.length)
          : 0.0,
        memories_md_sync_ms: r2(memSyncMs),
      },
      retrieval_50_scale: {
        queries_evaluated: validQueries,
        recall_at_1_pct: validQueries ? r1((hitsAt1 / validQueries) * 100) : 0.0,
        recall_at_3_pct: validQueries ? r1((hitsAt3 / validQueries) * 100) : 0.0,
        mrr: validQueries ? r3(rrs.reduce((a, b) => a + b, 0) / validQueries) : 0.0,
        negative_queries: NEGATIVE_PROMPTS.length,
        negative_fp_pct: r1((negFp / NEGATIVE_PROMPTS.length) * 100),
      },
      plugin_hook_validation: {
        pre_step_negative_fp_pct: r1((preStepNegInjected / NEGATIVE_PROMPTS.length) * 100),
        pre_step_anchor_tested: anchorTested,
        pre_step_anchor_hits: anchorHits,
        pre_step_anchor_hit_rate_pct: anchorTested
          ? r1((anchorHits / anchorTested) * 100)
          : 0.0,
        l2_cases_tested: l2Tested,
        l2_intercepted: l2Intercepted,
        l2_interception_rate_pct: l2Tested ? r1((l2Intercepted / l2Tested) * 100) : 0.0,
        l1_cases_tested: l1Tested,
        l1_intercepted: l1Intercepted,
        l1_interception_rate_pct: l1Tested ? r1((l1Intercepted / l1Tested) * 100) : 0.0,
        avg_hook_latency_ms: hookLatenciesMs.length
          ? r2(hookLatenciesMs.reduce((a, b) => a + b, 0) / hookLatenciesMs.length)
          : 0.0,
        telemetry,
      },
    };
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  const report = runPluginScaleValidation();
  const runsDir = path.join(WORKSPACE_ROOT, "benchmarks", "runs");
  mkdirSync(runsDir, { recursive: true });
  writeFileSync(
    path.join(runsDir, "plugin_scale_50_report.json"),
    JSON.stringify(report, null, 2),
    "utf-8"
  );
  console.log(JSON.stringify(report, null, 2));
}
