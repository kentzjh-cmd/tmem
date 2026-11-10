"""
Unit tests for tmem benchmark evaluation on public trajectories.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tmem.evaluate import BenchmarkEvaluator, format_markdown_report


class TestEvaluationBenchmark(unittest.TestCase):
    def test_benchmark_public_trajectories(self) -> None:
        evaluator = BenchmarkEvaluator()
        summary = evaluator.run_evaluation(verbose=False)

        # Assert evaluation passed
        self.assertTrue(summary.all_passed)
        self.assertGreaterEqual(summary.total_trajectories, 4)

        # Assert key performance and quality metrics
        self.assertGreaterEqual(summary.avg_compression_ratio, 50.0)
        self.assertEqual(summary.avg_noise_elimination_rate, 100.0)
        self.assertEqual(summary.top1_retrieval_accuracy, 1.0)
        self.assertEqual(summary.mean_reciprocal_rank, 1.0)
        self.assertGreaterEqual(summary.total_l2_invariants_extracted, 2)

        # Assert latency SLAs (<50ms per trajectory slicing, <10ms retrieval)
        self.assertLess(summary.avg_slicing_latency_ms, 50.0)
        self.assertLess(summary.avg_retrieval_latency_ms, 15.0)

        # Verify markdown reporting renders cleanly
        report_md = format_markdown_report(summary)
        self.assertIn("tmem Public Trajectory Dataset Benchmark", report_md)
        self.assertIn("PASSED", report_md)


if __name__ == "__main__":
    unittest.main()
