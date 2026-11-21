#!/usr/bin/env node
/**
 * CI Reproducible Benchmark Gate & Anti-Staleness Verifier for tmem (Node.js ESM).
 *
 * Runs all deterministic empirical benchmark suites and enforces three guarantees:
 * 1. Regression gates: core precision/recall/F1, retrieval, L2/L1 hook, and latency SLAs hold.
 * 2. Saved report sync: deterministic fields in `benchmarks/runs/*.json` match live runs.
 * 3. README sync: benchmark tables and figures in `README.md` and `README_EN.md` match live runs.
 *
 * Usage:
 *   node scripts/verify_benchmarks.mjs                  # Check-only (CI default, no file writes)
 *   node scripts/verify_benchmarks.mjs --update-reports # Refresh benchmarks/runs/*.json
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runDshE2eBenchmark } from "../benchmarks/eval_dsh_e2e.mjs";
import { verifyLlmJudgeReport } from "../benchmarks/eval_llm_judge.mjs";
import { runPluginScaleValidation } from "../benchmarks/eval_plugin_scale.mjs";
import { runRetrievalBenchmark } from "../benchmarks/eval_retrieval.mjs";
import { evaluateBaselines } from "../benchmarks/eval_slicing_fidelity.mjs";
import { BenchmarkEvaluator } from "../tmem/evaluate.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "..");

export const TIMING_KEYS = new Set([
  "avg_slice_latency_ms",
  "memories_md_sync_ms",
  "avg_hook_latency_ms",
]);

export function stripTimingFields(obj) {
  if (Array.isArray(obj)) {
    return obj.map((v) => stripTimingFields(v));
  }
  if (obj && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (!TIMING_KEYS.has(k)) {
        out[k] = stripTimingFields(v);
      }
    }
    return out;
  }
  return obj;
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== typeof b) return false;
  if (Array.isArray(a)) {
    if (!Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a === "object") {
    const keysA = Object.keys(a).sort();
    const keysB = Object.keys(b).sort();
    if (keysA.length !== keysB.length) return false;
    return keysA.every((k, i) => k === keysB[i] && deepEqual(a[k], b[k]));
  }
  return false;
}

export function checkRegressionGates(
  dshE2e,
  slicing50,
  slicing25,
  pluginScale,
  retrieval,
  crossEval
) {
  const errors = [];

  // 1. DSH E2E
  if ((dshE2e.step_reduction_pct ?? 0.0) < 37.5) {
    errors.push(`[DSH E2E] step_reduction_pct regressed: ${dshE2e.step_reduction_pct}%`);
  }
  if (!dshE2e.l1_triggered || !dshE2e.l2_triggered) {
    errors.push("[DSH E2E] l1_triggered and l2_triggered must both be true");
  }
  const tel = dshE2e.telemetry || {};
  if (tel.l2_resolution_rate !== 1.0 || tel.l1_adoption_rate !== 1.0) {
    errors.push(`[DSH E2E] telemetry rates regressed: ${JSON.stringify(tel)}`);
  }
  if ((dshE2e.negative_holdout_fp_pct ?? 100.0) !== 0.0) {
    errors.push(`[DSH E2E] negative_holdout_fp_pct != 0.0: ${dshE2e.negative_holdout_fp_pct}`);
  }
  if (dshE2e.dsh_node_runtime_available) {
    const hpMs = dshE2e.hot_path_21_steps_ms;
    if (hpMs === null || hpMs === undefined || hpMs >= 2000.0) {
      errors.push(`[DSH E2E] hot_path_21_steps_ms exceeded SLA: ${hpMs}`);
    }
  }

  // 2. Slicing Fidelity (50-trace & 25-trace)
  const b050 = slicing50.summary.B0_trivial_git_diff;
  const b150 = slicing50.summary.B1_tmem_slicer;
  if (b150.mean_f1 <= b050.mean_f1) {
    errors.push(`[Slicing-50] B1 F1 (${b150.mean_f1}) <= B0 F1 (${b050.mean_f1})`);
  }
  if (b150.mean_precision < 90.0 || b150.mean_recall < 90.0 || b150.mean_f1 < 90.0) {
    errors.push(`[Slicing-50] B1 metrics below 90% floor: ${JSON.stringify(b150)}`);
  }

  const b025 = slicing25.summary.B0_trivial_git_diff;
  const b125 = slicing25.summary.B1_tmem_slicer;
  if (b125.mean_f1 <= b025.mean_f1) {
    errors.push(`[Slicing-25] B1 F1 (${b125.mean_f1}) <= B0 F1 (${b025.mean_f1})`);
  }
  if (b125.mean_precision < 90.0 || b125.mean_f1 < 81.0) {
    errors.push(`[Slicing-25] B1 metrics below floor: ${JSON.stringify(b125)}`);
  }

  // 3. 50-Trajectory Plugin Scale
  const corpus = pluginScale.corpus;
  const ret50 = pluginScale.retrieval_50_scale;
  const hook = pluginScale.plugin_hook_validation;
  if (corpus.compiled_playbooks < 49) {
    errors.push(`[Plugin-50] compiled_playbooks < 49: ${corpus.compiled_playbooks}`);
  }
  if (corpus.step_compression_pct < 80.0 || corpus.char_compression_pct < 90.0) {
    errors.push(`[Plugin-50] compression regressed: ${JSON.stringify(corpus)}`);
  }
  if (corpus.avg_slice_latency_ms >= 50.0) {
    errors.push(`[Plugin-50] avg_slice_latency_ms >= 50ms: ${corpus.avg_slice_latency_ms}`);
  }
  if (corpus.memories_md_sync_ms >= 200.0) {
    errors.push(`[Plugin-50] memories_md_sync_ms >= 200ms: ${corpus.memories_md_sync_ms}`);
  }
  if (ret50.recall_at_1_pct < 100.0 || ret50.mrr < 1.0) {
    errors.push(`[Plugin-50] retrieval regressed: ${JSON.stringify(ret50)}`);
  }
  if (hook.pre_step_negative_fp_pct !== 0.0) {
    errors.push(`[Plugin-50] pre_step_negative_fp_pct != 0.0: ${hook.pre_step_negative_fp_pct}`);
  }
  if (hook.pre_step_anchor_hit_rate_pct < 100.0) {
    errors.push(`[Plugin-50] pre_step_anchor_hit_rate_pct < 100.0: ${hook.pre_step_anchor_hit_rate_pct}`);
  }
  if (hook.l2_interception_rate_pct < 100.0 || hook.telemetry?.l2_resolution_rate !== 1.0) {
    errors.push(`[Plugin-50] L2 interception/resolution regressed: ${JSON.stringify(hook)}`);
  }
  if (hook.avg_hook_latency_ms >= 100.0) {
    errors.push(`[Plugin-50] avg_hook_latency_ms >= 100ms: ${hook.avg_hook_latency_ms}`);
  }

  // 4. Independent Retrieval
  const rm = retrieval.metrics;
  if (
    rm.recall_at_1 < 100.0 ||
    rm.recall_at_3 < 100.0 ||
    rm.mrr < 1.0 ||
    rm.false_positive_rate !== 0.0
  ) {
    errors.push(`[Retrieval] metrics regressed: ${JSON.stringify(rm)}`);
  }

  // 5. Cross-Format Evaluation (`tmem evaluate`)
  if (!crossEval.all_passed) {
    errors.push(`[Evaluate] all_passed is false: ${JSON.stringify(crossEval)}`);
  }
  if ((crossEval.avg_compression_ratio ?? 0.0) < 50.0) {
    errors.push(`[Evaluate] avg_compression_ratio < 50%: ${crossEval.avg_compression_ratio}`);
  }
  if ((crossEval.total_l2_invariants_extracted ?? 0) < 7) {
    errors.push(
      `[Evaluate] total_l2_invariants_extracted < 7: ${crossEval.total_l2_invariants_extracted}`
    );
  }
  if ((crossEval.avg_slicing_latency_ms ?? 999.0) >= 50.0) {
    errors.push(`[Evaluate] avg_slicing_latency_ms >= 50ms: ${crossEval.avg_slicing_latency_ms}`);
  }

  return errors;
}

function displayPath(p) {
  const rel = path.relative(WORKSPACE_ROOT, p);
  return rel && !rel.startsWith("..") ? rel : p;
}

export function checkSavedReports(runsDir, slicing50, pluginScale, retrieval) {
  const errors = [];
  const reportPairs = [
    ["slicing_fidelity_report.json", slicing50, false],
    ["plugin_scale_50_report.json", pluginScale, true],
    ["retrieval_report.json", retrieval, false],
  ];
  for (const [filename, liveData, stripTiming] of reportPairs) {
    const p = path.join(runsDir, filename);
    if (!existsSync(p)) {
      errors.push(`[Reports] Missing report file: ${displayPath(p)}`);
      continue;
    }
    const savedData = JSON.parse(readFileSync(p, "utf-8"));
    const lhs = stripTiming ? stripTimingFields(savedData) : savedData;
    const rhs = stripTiming ? stripTimingFields(liveData) : liveData;
    if (!deepEqual(lhs, rhs)) {
      errors.push(
        `[Reports] ${displayPath(p)} is out of sync with live benchmark output ` +
          "(run `node scripts/verify_benchmarks.mjs --update-reports`)."
      );
    }
  }
  return errors;
}

function extractTableRow(text, prefix) {
  for (const line of text.split(/\r?\n/)) {
    const stripped = line.trim();
    if (!stripped.startsWith("|")) continue;
    const cells = stripped
      .replace(/^\|+|\|+$/g, "")
      .split("|")
      .map((c) => c.trim());
    if (cells.length > 0 && cells[0].includes(prefix)) {
      return cells;
    }
  }
  return null;
}

function requireMatch(errors, docName, label, regex, text, expectedGroups) {
  const m = text.match(regex);
  if (!m) {
    errors.push(`[${docName}] Missing or unparseable benchmark section for '${label}'`);
    return;
  }
  const actual = m.slice(1);
  if (
    actual.length !== expectedGroups.length ||
    actual.some((val, i) => val !== expectedGroups[i])
  ) {
    errors.push(
      `[${docName}] Stale '${label}': doc=${JSON.stringify(actual)} != live=${JSON.stringify(
        expectedGroups
      )}`
    );
  }
}

function fmtPct(num) {
  return Number.isInteger(num) ? `${num}.0%` : `${num}%`;
}

function fmtInt(num) {
  return Number(num).toLocaleString("en-US");
}

export function checkReadmeSync(
  readmePath,
  dshE2e,
  slicing50,
  slicing25,
  pluginScale,
  retrieval,
  crossEval,
  llmJudge = null
) {
  const errors = [];
  if (!existsSync(readmePath)) {
    return [`[${path.basename(readmePath)}] File does not exist`];
  }

  const text = readFileSync(readmePath, "utf-8");
  const docName = path.basename(readmePath);
  const isZh = docName === "README.md";

  // 1. Gold-50 Markdown Table (B0, B1, B2)
  const tableExpectations = [
    ["B0", slicing50.summary.B0_trivial_git_diff],
    ["B1", slicing50.summary.B1_tmem_slicer],
    ["B2", slicing50.summary.B2_oracle_upper_bound],
  ];
  const stepSuffix = isZh ? "步" : "steps";
  for (const [rowKey, stats] of tableExpectations) {
    const cells = extractTableRow(text, `**${rowKey}`);
    if (!cells || cells.length < 5) {
      errors.push(`[${docName}] Missing Gold-50 table row for ${rowKey}`);
      continue;
    }
    const expectedCells = [
      fmtPct(stats.mean_precision),
      fmtPct(stats.mean_recall),
      fmtPct(stats.mean_f1),
      `${stats.avg_steps.toFixed(1)} ${stepSuffix}`,
    ];
    const actualCells = cells.slice(1, 5).map((c) => c.replace(/\*\*/g, "").trim());
    if (actualCells.some((v, i) => v !== expectedCells[i])) {
      errors.push(
        `[${docName}] Gold-50 table row ${rowKey} mismatch: doc=${JSON.stringify(
          actualCells
        )} != live=${JSON.stringify(expectedCells)}`
      );
    }
    if (llmJudge && cells.length >= 6) {
      const judgeCell = cells[5].replace(/\*\*/g, "").trim();
      if (rowKey === "B0") {
        const expB0 = Number(llmJudge.mean_llm_scores.B0_baseline.overall).toFixed(2);
        if (judgeCell !== expB0) {
          errors.push(
            `[${docName}] Gold-50 LLM judge B0 mismatch: doc=${judgeCell} != report=${expB0}`
          );
        }
      } else if (rowKey === "B1") {
        const expB1 = Number(llmJudge.mean_llm_scores.B1_tmem.overall).toFixed(2);
        const pj = llmJudge.pairwise_llm_judge;
        const expWin = `${pj.B1_tmem_wins}:${pj.B0_baseline_wins} / ${fmtPct(pj.B1_win_rate_pct)}`;
        if (!judgeCell.includes(expB1) || !judgeCell.includes(expWin)) {
          errors.push(
            `[${docName}] Gold-50 LLM judge B1 mismatch: doc=${judgeCell} != expected (${expB1}, ${expWin})`
          );
        }
      }
    }
  }

  // 2. Gold-25 note
  const b025 = slicing25.summary.B0_trivial_git_diff;
  const b125 = slicing25.summary.B1_tmem_slicer;
  if (isZh) {
    requireMatch(
      errors,
      docName,
      "Gold-25 metrics",
      /B1 Precision 亦已从 63\.2% 提升至 ([0-9.]+%)（F1 ([0-9.]+%) > B0 ([0-9.]+%)）/m,
      text,
      [fmtPct(b125.mean_precision), fmtPct(b125.mean_f1), fmtPct(b025.mean_f1)]
    );
  } else {
    requireMatch(
      errors,
      docName,
      "Gold-25 metrics",
      /B1 Precision likewise improved from 63\.2% to ([0-9.]+%) \(F1 ([0-9.]+%) vs\. B0's ([0-9.]+%)\)/m,
      text,
      [fmtPct(b125.mean_precision), fmtPct(b125.mean_f1), fmtPct(b025.mean_f1)]
    );
  }

  // 3. DSH E2E
  const tel = dshE2e.telemetry;
  const l2Pct = `${Math.round(tel.l2_resolution_rate * 100)}%`;
  const l1Pct = `${Math.round(tel.l1_adoption_rate * 100)}%`;
  requireMatch(
    errors,
    docName,
    "DSH E2E step reduction",
    isZh
      ? /\*\*([0-9.]+%)\*\*（(\d+) 步试错基线 → (\d+) 步完成）/m
      : /\*\*([0-9.]+%)\*\* \((\d+) trial-and-error baseline steps → (\d+) steps with `tmem`\)/m,
    text,
    [fmtPct(dshE2e.step_reduction_pct), String(dshE2e.baseline_steps), String(dshE2e.with_tmem_steps)]
  );
  requireMatch(
    errors,
    docName,
    "DSH E2E telemetry",
    isZh
      ? /L2 报错修复转绿率 \*\*([0-9.]+%)\*\*（`(\d+\/\d+)`），L1 共变提醒采纳率 \*\*([0-9.]+%)\*\*（`(\d+\/\d+)`）/m
      : /L2 green error resolution \*\*([0-9.]+%)\*\* \(`(\d+\/\d+)`\), L1 co-change partner adoption \*\*([0-9.]+%)\*\* \(`(\d+\/\d+)`\)/m,
    text,
    [
      l2Pct,
      `${tel.l2_resolved_green}/${tel.l2_injections}`,
      l1Pct,
      `${tel.l1_adopted}/${tel.l1_hints}`,
    ]
  );
  requireMatch(
    errors,
    docName,
    "DSH E2E negative holdout FP",
    isZh
      ? /泛自然语言负例误注入率\*\*：\*\*([0-9.]+%)\*\*/m
      : /Natural-Language Holdout False Positive Injection Rate\*\*: \*\*([0-9.]+%)\*\*/m,
    text,
    [fmtPct(dshE2e.negative_holdout_fp_pct)]
  );

  // 4. Plugin Scale 50-Trace
  const corpus = pluginScale.corpus;
  const hook = pluginScale.plugin_hook_validation;
  const ret50 = pluginScale.retrieval_50_scale;
  if (isZh) {
    requireMatch(
      errors,
      docName,
      "Plugin-50 compression",
      /(\d+) 条编译为 \*\*(\d+)\*\* 条 Playbook.*?原始 \*\*(\d+) 步 → L0 (\d+) 步\*\*（步数精简 \*\*([0-9.]+%)\*\*，字符体积压缩 \*\*([0-9.]+%)\*\*）/m,
      text,
      [
        String(corpus.total_input_trajectories),
        String(corpus.compiled_playbooks),
        String(corpus.raw_steps_total),
        String(corpus.l0_steps_total),
        fmtPct(corpus.step_compression_pct),
        fmtPct(corpus.char_compression_pct),
      ]
    );
    requireMatch(
      errors,
      docName,
      "Plugin-50 hook validation",
      /`pre_step` 负例误注入 \*\*([0-9.]+%)\*\*（`\d+\/\d+`）、显式锚点召回 \*\*([0-9.]+%)\*\*（`(\d+\/\d+)`）；`post_tool_use` L2 同源报错拦截与验证转绿率 \*\*([0-9.]+%)\*\*（`(\d+\/\d+)`）/m,
      text,
      [
        fmtPct(hook.pre_step_negative_fp_pct),
        fmtPct(hook.pre_step_anchor_hit_rate_pct),
        `${hook.pre_step_anchor_hits}/${hook.pre_step_anchor_tested}`,
        fmtPct(hook.l2_interception_rate_pct),
        `${hook.l2_intercepted}/${hook.l2_cases_tested}`,
      ]
    );
  } else {
    requireMatch(
      errors,
      docName,
      "Plugin-50 compression",
      /(\d+)\/(\d+) compiled playbooks.*?reducing \*\*(\d+) → (\d+) L0 steps\*\* \(\*\*([0-9.]+%)\*\* step reduction, \*\*([0-9.]+%)\*\* RawTrace-to-L0 char compression\)/m,
      text,
      [
        String(corpus.compiled_playbooks),
        String(corpus.total_input_trajectories),
        String(corpus.raw_steps_total),
        String(corpus.l0_steps_total),
        fmtPct(corpus.step_compression_pct),
        fmtPct(corpus.char_compression_pct),
      ]
    );
    requireMatch(
      errors,
      docName,
      "Plugin-50 hook validation",
      /\*\*([0-9.]+%)\*\* `pre_step` negative FP \(`\d+\/\d+`\); \*\*([0-9.]+%)\*\* explicit anchor recall \(`(\d+\/\d+)`\); \*\*([0-9.]+%)\*\* `post_tool_use` L2 interception & green resolution \(`(\d+\/\d+)`/m,
      text,
      [
        fmtPct(hook.pre_step_negative_fp_pct),
        fmtPct(hook.pre_step_anchor_hit_rate_pct),
        `${hook.pre_step_anchor_hits}/${hook.pre_step_anchor_tested}`,
        fmtPct(hook.l2_interception_rate_pct),
        `${hook.l2_intercepted}/${hook.l2_cases_tested}`,
      ]
    );
  }

  // 5. Independent Retrieval
  const rm = retrieval.metrics;
  const mrrStr = Number.isInteger(rm.mrr) ? rm.mrr.toFixed(1) : String(rm.mrr);
  if (isZh) {
    requireMatch(
      errors,
      docName,
      "Retrieval metrics",
      /(\d+) 条含原始需求文本的样本在 `eval_retrieval\.mjs` 取得 \*\*Recall@1 = ([0-9.]+%) \/ Recall@3 = ([0-9.]+%) \/ MRR = ([0-9.]+)\*\*.*?负例误召回率降至 ([0-9.]+%)\*\*（49 条全库压力下 FTS5 负例误召回 \*\*([0-9.]+%)\*\*/m,
      text,
      [
        String(retrieval.total_eval_queries),
        fmtPct(rm.recall_at_1),
        fmtPct(rm.recall_at_3),
        mrrStr,
        fmtPct(rm.false_positive_rate),
        fmtPct(ret50.negative_fp_pct),
      ]
    );
  } else {
    requireMatch(
      errors,
      docName,
      "Retrieval metrics",
      /Across (\d+) issue-query traces in `eval_retrieval\.mjs`, \*\*Recall@1 = ([0-9.]+%) \/ Recall@3 = ([0-9.]+%) \/ MRR = ([0-9.]+)\*\*, and negative holdout FP dropped to \*\*([0-9.]+%)\*\*.*?lexical FTS5 negative FP is \*\*([0-9.]+%)\*\*/m,
      text,
      [
        String(retrieval.total_eval_queries),
        fmtPct(rm.recall_at_1),
        fmtPct(rm.recall_at_3),
        mrrStr,
        fmtPct(rm.false_positive_rate),
        fmtPct(ret50.negative_fp_pct),
      ]
    );
  }

  // 6. Cross-format `tmem evaluate`
  if (isZh) {
    requireMatch(
      errors,
      docName,
      "Cross-format trajectory count",
      /跨格式轨迹压缩与运行时开销评测（(\d+) 条真实轨迹）/m,
      text,
      [String(crossEval.total_trajectories)]
    );
    requireMatch(
      errors,
      docName,
      "Cross-format compression",
      /L0 步骤精简率\*\*：\*\*([0-9.]+%)\*\*（(\d+) 步 → (\d+) 步，([\d,]+) → ([\d,]+) 字符，~([\d,]+) tok → ~([\d,]+) tok）/m,
      text,
      [
        fmtPct(crossEval.avg_compression_ratio),
        String(crossEval.total_raw_steps),
        String(crossEval.total_sliced_steps),
        fmtInt(crossEval.total_raw_chars),
        fmtInt(crossEval.total_sliced_chars),
        fmtInt(crossEval.total_raw_tokens_est),
        fmtInt(crossEval.total_sliced_tokens_est),
      ]
    );
    requireMatch(
      errors,
      docName,
      "Cross-format L2 pairs",
      /高熵 L2 因果修复对\*\*：\*\*(\d+) 组\*\*/m,
      text,
      [String(crossEval.total_l2_invariants_extracted)]
    );
  } else {
    requireMatch(
      errors,
      docName,
      "Cross-format trajectory count",
      /Cross-Format Trajectory Compression & Runtime Footprint \((\d+) real trajectories\)/m,
      text,
      [String(crossEval.total_trajectories)]
    );
    requireMatch(
      errors,
      docName,
      "Cross-format compression",
      /Mean Step Compression\*\*: \*\*([0-9.]+%)\*\* \((\d+) → (\d+) steps; ([\d,]+) → ([\d,]+) chars; ~([\d,]+) → ~([\d,]+) est\. tokens\)/m,
      text,
      [
        fmtPct(crossEval.avg_compression_ratio),
        String(crossEval.total_raw_steps),
        String(crossEval.total_sliced_steps),
        fmtInt(crossEval.total_raw_chars),
        fmtInt(crossEval.total_sliced_chars),
        fmtInt(crossEval.total_raw_tokens_est),
        fmtInt(crossEval.total_sliced_tokens_est),
      ]
    );
    requireMatch(
      errors,
      docName,
      "Cross-format L2 pairs",
      /High-Entropy L2 Causal Pairs Extracted\*\*: \*\*(\d+) pairs\*\*/m,
      text,
      [String(crossEval.total_l2_invariants_extracted)]
    );
  }

  return errors;
}

export async function runVerification({ updateReports = false, verbose = false } = {}) {
  const dshE2e = await runDshE2eBenchmark();
  const slicing50 = evaluateBaselines(
    path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations_50.json")
  );
  const slicing25 = evaluateBaselines(
    path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations.json")
  );
  const pluginScale = runPluginScaleValidation();
  const retrieval = runRetrievalBenchmark();
  const crossEval = new BenchmarkEvaluator().runEvaluation();
  const llmJudge = verifyLlmJudgeReport();

  const runsDir = path.join(WORKSPACE_ROOT, "benchmarks", "runs");
  if (updateReports) {
    mkdirSync(runsDir, { recursive: true });
    writeFileSync(
      path.join(runsDir, "slicing_fidelity_report.json"),
      JSON.stringify(slicing50, null, 2),
      "utf-8"
    );
    writeFileSync(
      path.join(runsDir, "plugin_scale_50_report.json"),
      JSON.stringify(pluginScale, null, 2),
      "utf-8"
    );
    writeFileSync(
      path.join(runsDir, "retrieval_report.json"),
      JSON.stringify(retrieval, null, 2),
      "utf-8"
    );
  }

  if (verbose) {
    console.log("DSH E2E:", JSON.stringify(dshE2e));
    console.log("Slicing-50:", JSON.stringify(slicing50.summary));
    console.log("Slicing-25:", JSON.stringify(slicing25.summary));
    console.log("Plugin-50:", JSON.stringify(pluginScale));
    console.log("Retrieval:", JSON.stringify(retrieval.metrics));
    const { results: _r, progressive: _p, ...crossBrief } = crossEval;
    console.log("Evaluate:", JSON.stringify(crossBrief));
  }

  const errors = [
    ...checkRegressionGates(dshE2e, slicing50, slicing25, pluginScale, retrieval, crossEval),
    ...checkSavedReports(runsDir, slicing50, pluginScale, retrieval),
    ...checkReadmeSync(
      path.join(WORKSPACE_ROOT, "README.md"),
      dshE2e,
      slicing50,
      slicing25,
      pluginScale,
      retrieval,
      crossEval,
      llmJudge
    ),
    ...checkReadmeSync(
      path.join(WORKSPACE_ROOT, "README_EN.md"),
      dshE2e,
      slicing50,
      slicing25,
      pluginScale,
      retrieval,
      crossEval,
      llmJudge
    ),
  ];

  if (errors.length > 0) {
    console.error("Benchmark CI verification FAILED:");
    for (const err of errors) {
      console.error(`  - ${err}`);
    }
    return 1;
  }

  console.log(
    "All reproducible benchmark gates, benchmarks/runs/*.json reports, " +
      "and README.md / README_EN.md tables are verified and in sync."
  );
  return 0;
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  const argv = process.argv.slice(2);
  const updateReports = argv.includes("--update-reports");
  const verbose = argv.includes("--verbose");
  process.exitCode = await runVerification({ updateReports, verbose });
}
