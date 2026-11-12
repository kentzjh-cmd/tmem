"""
Unit tests for tmem benchmark descriptive evaluation on public trajectories.
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
        self.assertGreaterEqual(summary.total_trajectories, 8)

        # Assert empirical descriptive metrics
        self.assertGreaterEqual(summary.avg_compression_ratio, 40.0)
        self.assertGreater(summary.total_raw_steps, summary.total_sliced_steps)
        self.assertGreater(summary.total_raw_chars, summary.total_sliced_chars)
        self.assertGreater(summary.total_raw_tokens_est, summary.total_sliced_tokens_est)

        # Assert latency SLAs (<50ms per trajectory slicing)
        self.assertLess(summary.avg_slicing_latency_ms, 50.0)

        # Verify markdown reporting renders cleanly
        report_md = format_markdown_report(summary)
        self.assertIn("轨迹切片与特征描述评测", report_md)
        self.assertIn("PASSED", report_md)


if __name__ == "__main__":
    unittest.main()
