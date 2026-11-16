"""Tests for CI workflow contracts and benchmark/README anti-staleness verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_benchmarks import (
    _check_readme_sync,
    _check_regression_gates,
    _check_saved_reports,
    _strip_timing_fields,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestCiAndBenchmarkSync(unittest.TestCase):
    def test_github_actions_workflow_exists_and_covers_tests_and_benchmarks(self) -> None:
        ci_file = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci_file.exists(), ".github/workflows/ci.yml must exist")
        content = ci_file.read_text(encoding="utf-8")
        self.assertIn("unittest discover -s tests", content)
        self.assertIn("scripts/verify_benchmarks.py", content)
        self.assertIn("node --check tmem/dsh_plugin.mjs", content)

    def test_strip_timing_fields_preserves_deterministic_keys(self) -> None:
        sample = {
            "corpus": {
                "compiled_playbooks": 49,
                "avg_slice_latency_ms": 0.82,
                "memories_md_sync_ms": 6.98,
            },
            "plugin_hook_validation": {
                "l2_intercepted": 7,
                "avg_hook_latency_ms": 8.79,
            },
        }
        stripped = _strip_timing_fields(sample)
        self.assertEqual(stripped["corpus"], {"compiled_playbooks": 49})
        self.assertEqual(stripped["plugin_hook_validation"], {"l2_intercepted": 7})

    def test_readme_sync_detects_stale_table_and_metrics(self) -> None:
        dsh_e2e = {
            "baseline_steps": 8,
            "with_tmem_steps": 5,
            "step_reduction_pct": 37.5,
            "l1_triggered": True,
            "l2_triggered": True,
            "telemetry": {
                "l2_resolution_rate": 1.0,
                "l2_resolved_green": 1,
                "l2_injections": 1,
                "l1_adoption_rate": 1.0,
                "l1_adopted": 1,
                "l1_hints": 1,
            },
            "negative_holdout_fp_pct": 0.0,
            "dsh_node_runtime_available": False,
            "hot_path_21_steps_ms": None,
        }
        slicing_50 = {
            "summary": {
                "B0_trivial_git_diff": {"mean_precision": 81.1, "mean_recall": 66.5, "mean_f1": 71.4, "avg_steps": 4.7},
                "B1_tmem_slicer": {"mean_precision": 93.8, "mean_recall": 95.0, "mean_f1": 93.6, "avg_steps": 5.1},
                "B2_oracle_upper_bound": {"mean_precision": 98.0, "mean_recall": 98.0, "mean_f1": 98.0, "avg_steps": 5.0},
            }
        }
        slicing_25 = {
            "summary": {
                "B0_trivial_git_diff": {"mean_precision": 97.1, "mean_recall": 70.8, "mean_f1": 81.0, "avg_steps": 4.4},
                "B1_tmem_slicer": {"mean_precision": 92.3, "mean_recall": 78.2, "mean_f1": 81.8, "avg_steps": 4.1},
            }
        }
        plugin_scale = {
            "corpus": {
                "total_input_trajectories": 50,
                "compiled_playbooks": 49,
                "raw_steps_total": 898,
                "l0_steps_total": 157,
                "step_compression_pct": 82.5,
                "char_compression_pct": 94.7,
                "avg_slice_latency_ms": 0.8,
            },
            "retrieval_50_scale": {"recall_at_1_pct": 100.0, "mrr": 1.0, "negative_fp_pct": 10.0},
            "plugin_hook_validation": {
                "pre_step_negative_fp_pct": 0.0,
                "pre_step_anchor_hit_rate_pct": 100.0,
                "pre_step_anchor_hits": 48,
                "pre_step_anchor_tested": 48,
                "l2_interception_rate_pct": 100.0,
                "l2_intercepted": 7,
                "l2_cases_tested": 7,
                "avg_hook_latency_ms": 8.0,
                "telemetry": {"l2_resolution_rate": 1.0},
            },
        }
        retrieval = {
            "total_eval_queries": 24,
            "metrics": {
                "recall_at_1": 100.0,
                "recall_at_3": 100.0,
                "mrr": 1.0,
                "false_positive_rate": 0.0,
            },
        }
        cross_eval = {
            "all_passed": True,
            "total_trajectories": 9,
            "avg_compression_ratio": 56.5,
            "total_raw_steps": 106,
            "total_sliced_steps": 43,
            "total_raw_chars": 5406,
            "total_sliced_chars": 1480,
            "total_raw_tokens_est": 1348,
            "total_sliced_tokens_est": 367,
            "total_l2_invariants_extracted": 8,
            "avg_slicing_latency_ms": 1.0,
        }

        self.assertEqual(
            _check_regression_gates(dsh_e2e, slicing_50, slicing_25, plugin_scale, retrieval, cross_eval),
            [],
        )
        self.assertEqual(
            _check_readme_sync(
                REPO_ROOT / "README.md", dsh_e2e, slicing_50, slicing_25, plugin_scale, retrieval, cross_eval
            ),
            [],
        )
        self.assertEqual(
            _check_readme_sync(
                REPO_ROOT / "README_EN.md", dsh_e2e, slicing_50, slicing_25, plugin_scale, retrieval, cross_eval
            ),
            [],
        )

        # Mutate B1 F1 in a copy of README.md and assert drift is caught
        with tempfile.TemporaryDirectory() as tmp:
            stale_readme = Path(tmp) / "README.md"
            stale_readme.write_text(
                (REPO_ROOT / "README.md").read_text(encoding="utf-8").replace("**93.6%**", "**90.0%**"),
                encoding="utf-8",
            )
            errs = _check_readme_sync(
                stale_readme, dsh_e2e, slicing_50, slicing_25, plugin_scale, retrieval, cross_eval
            )
            self.assertTrue(any("Gold-50 table row B1 mismatch" in e for e in errs))

            # Verify saved report drift detection
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            (runs_dir / "slicing_fidelity_report.json").write_text(json.dumps({"stale": True}), encoding="utf-8")
            (runs_dir / "plugin_scale_50_report.json").write_text(json.dumps(plugin_scale), encoding="utf-8")
            (runs_dir / "retrieval_report.json").write_text(json.dumps(retrieval), encoding="utf-8")
            rep_errs = _check_saved_reports(runs_dir, slicing_50, plugin_scale, retrieval)
            self.assertEqual(len(rep_errs), 1)
            self.assertIn("slicing_fidelity_report.json", rep_errs[0])


if __name__ == "__main__":
    unittest.main()
