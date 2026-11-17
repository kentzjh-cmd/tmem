import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildBlindedJudgePackets, verifyLlmJudgeReport } from "../benchmarks/eval_llm_judge.mjs";
import {
  checkReadmeSync,
  checkRegressionGates,
  checkSavedReports,
  stripTimingFields,
} from "../scripts/verify_benchmarks.mjs";
import { main as cliMain } from "../tmem/cli.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("GitHub Actions workflow exists and covers tests, syntax checks, and benchmark verification", () => {
  const ciFile = path.join(REPO_ROOT, ".github", "workflows", "ci.yml");
  const content = readFileSync(ciFile, "utf-8");
  assert.ok(content.includes("main"));
  assert.ok(content.includes("node"));
  assert.ok(content.includes("npm run check"));
  assert.ok(content.includes("npm test"));
  assert.ok(content.includes("node scripts/verify_benchmarks.mjs"));
});

test("stripTimingFields preserves deterministic keys", () => {
  const sample = {
    corpus: {
      compiled_playbooks: 49,
      avg_slice_latency_ms: 0.82,
      memories_md_sync_ms: 6.98,
    },
    plugin_hook_validation: {
      l2_intercepted: 7,
      avg_hook_latency_ms: 8.79,
    },
  };
  const stripped = stripTimingFields(sample);
  assert.deepEqual(stripped.corpus, { compiled_playbooks: 49 });
  assert.deepEqual(stripped.plugin_hook_validation, { l2_intercepted: 7 });
});

test("README sync and saved report drift detection work accurately", (t) => {
  const dshE2e = {
    baseline_steps: 8,
    with_tmem_steps: 5,
    step_reduction_pct: 37.5,
    l1_triggered: true,
    l2_triggered: true,
    telemetry: {
      l2_resolution_rate: 1.0,
      l2_resolved_green: 1,
      l2_injections: 1,
      l1_adoption_rate: 1.0,
      l1_adopted: 1,
      l1_hints: 1,
    },
    negative_holdout_fp_pct: 0.0,
    dsh_node_runtime_available: false,
    hot_path_21_steps_ms: null,
  };
  const slicing50 = {
    summary: {
      B0_trivial_git_diff: {
        mean_precision: 81.1,
        mean_recall: 66.5,
        mean_f1: 71.4,
        avg_steps: 4.7,
      },
      B1_tmem_slicer: {
        mean_precision: 93.8,
        mean_recall: 95.0,
        mean_f1: 93.6,
        avg_steps: 5.1,
      },
      B2_oracle_upper_bound: {
        mean_precision: 98.0,
        mean_recall: 98.0,
        mean_f1: 98.0,
        avg_steps: 5.0,
      },
    },
  };
  const slicing25 = {
    summary: {
      B0_trivial_git_diff: {
        mean_precision: 97.1,
        mean_recall: 70.8,
        mean_f1: 81.0,
        avg_steps: 4.4,
      },
      B1_tmem_slicer: {
        mean_precision: 92.3,
        mean_recall: 78.2,
        mean_f1: 81.8,
        avg_steps: 4.1,
      },
    },
  };
  const pluginScale = {
    corpus: {
      total_input_trajectories: 50,
      compiled_playbooks: 49,
      raw_steps_total: 898,
      l0_steps_total: 157,
      step_compression_pct: 82.5,
      char_compression_pct: 94.8,
      avg_slice_latency_ms: 0.8,
    },
    retrieval_50_scale: {
      recall_at_1_pct: 100.0,
      mrr: 1.0,
      negative_fp_pct: 10.0,
    },
    plugin_hook_validation: {
      pre_step_negative_fp_pct: 0.0,
      pre_step_anchor_hit_rate_pct: 100.0,
      pre_step_anchor_hits: 48,
      pre_step_anchor_tested: 48,
      l2_interception_rate_pct: 100.0,
      l2_intercepted: 7,
      l2_cases_tested: 7,
      avg_hook_latency_ms: 8.0,
      telemetry: { l2_resolution_rate: 1.0 },
    },
  };
  const retrieval = {
    total_eval_queries: 24,
    metrics: {
      recall_at_1: 100.0,
      recall_at_3: 100.0,
      mrr: 1.0,
      false_positive_rate: 0.0,
    },
  };
  const crossEval = {
    all_passed: true,
    total_trajectories: 9,
    avg_compression_ratio: 56.5,
    total_raw_steps: 106,
    total_sliced_steps: 43,
    total_raw_chars: 5406,
    total_sliced_chars: 1480,
    total_raw_tokens_est: 1348,
    total_sliced_tokens_est: 367,
    total_l2_invariants_extracted: 8,
    avg_slicing_latency_ms: 1.0,
  };

  assert.deepEqual(
    checkRegressionGates(dshE2e, slicing50, slicing25, pluginScale, retrieval, crossEval),
    []
  );
  assert.deepEqual(
    checkReadmeSync(
      path.join(REPO_ROOT, "README.md"),
      dshE2e,
      slicing50,
      slicing25,
      pluginScale,
      retrieval,
      crossEval
    ),
    []
  );
  assert.deepEqual(
    checkReadmeSync(
      path.join(REPO_ROOT, "README_EN.md"),
      dshE2e,
      slicing50,
      slicing25,
      pluginScale,
      retrieval,
      crossEval
    ),
    []
  );

  const tmp = mkdtempSync(path.join(os.tmpdir(), "tmem_ci_sync_"));
  t.after(() => rmSync(tmp, { recursive: true, force: true }));

  const staleReadme = path.join(tmp, "README.md");
  writeFileSync(
    staleReadme,
    readFileSync(path.join(REPO_ROOT, "README.md"), "utf-8").replace("**93.6%**", "**90.0%**"),
    "utf-8"
  );
  const errs = checkReadmeSync(
    staleReadme,
    dshE2e,
    slicing50,
    slicing25,
    pluginScale,
    retrieval,
    crossEval
  );
  assert.ok(errs.some((e) => e.includes("Gold-50 table row B1 mismatch")));

  const runsDir = path.join(tmp, "runs");
  mkdirSync(runsDir, { recursive: true });
  writeFileSync(path.join(runsDir, "slicing_fidelity_report.json"), JSON.stringify({ stale: true }));
  writeFileSync(path.join(runsDir, "plugin_scale_50_report.json"), JSON.stringify(pluginScale));
  writeFileSync(path.join(runsDir, "retrieval_report.json"), JSON.stringify(retrieval));
  const repErrs = checkSavedReports(runsDir, slicing50, pluginScale, retrieval);
  assert.equal(repErrs.length, 1);
  assert.ok(repErrs[0].includes("slicing_fidelity_report.json"));
});

test("LLM judge verification and blinded packet generation succeed", () => {
  const summary = verifyLlmJudgeReport();
  assert.equal(summary.total_trajectories, 50);
  assert.equal(summary.pairwise_llm_judge.B1_tmem_wins, 49);
  assert.equal(summary.mean_llm_scores.B1_tmem.overall, 4.8);

  const packets = buildBlindedJudgePackets(null, 42);
  assert.equal(packets.length, 50);
  assert.ok("option_a" in packets[0]);
  assert.ok("option_b" in packets[0]);
});

test("CLI lookup accepts positional/flag queries and unknown flags do not swallow positional args", (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "tmem_cli_args_"));
  t.after(() => rmSync(tmp, { recursive: true, force: true }));

  const logs = [];
  const io = {
    log: (m) => logs.push(String(m)),
    err: (m) => logs.push(String(m)),
  };

  assert.equal(
    cliMain(["lookup", "TypeError", "--file", "src/validator.py", "--workspace", tmp], io),
    0
  );
  assert.equal(
    cliMain(["lookup", "--query", "TypeError", "--file", "src/validator.py", "--workspace", tmp], io),
    0
  );
  // Unknown boolean flag before subcommand must not consume "status"
  logs.length = 0;
  assert.equal(cliMain(["--unknown-flag", "status", "--workspace", tmp], io), 0);
  assert.ok(logs.join("\n").includes("project_traces"));
});
