import test from "node:test";
import assert from "node:assert/strict";
import { BenchmarkEvaluator, formatMarkdownReport } from "../tmem/evaluate.mjs";

test("BenchmarkEvaluator runs cleanly on benchmarks/data trajectories", () => {
  const evaluator = new BenchmarkEvaluator();
  const summary = evaluator.runEvaluation(false);

  assert.equal(summary.all_passed, true);
  assert.ok(summary.total_trajectories >= 8);
  assert.ok(summary.avg_compression_ratio >= 40.0);
  assert.ok(summary.total_raw_steps > summary.total_sliced_steps);
  assert.ok(summary.total_raw_chars > summary.total_sliced_chars);
  assert.ok(summary.total_raw_tokens_est > summary.total_sliced_tokens_est);
  assert.ok(summary.avg_slicing_latency_ms < 50.0);

  const reportMd = formatMarkdownReport(summary);
  assert.ok(reportMd.includes("轨迹切片与特征描述评测"));
  assert.ok(reportMd.includes("PASSED"));
});
