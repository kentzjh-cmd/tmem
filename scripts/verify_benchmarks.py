#!/usr/bin/env python3
"""
CI Reproducible Benchmark Gate & Anti-Staleness Verifier for tmem.

Runs all deterministic empirical benchmark suites and enforces three guarantees:
1. Regression gates: core precision/recall/F1, retrieval, L2/L1 hook, and latency SLAs hold.
2. Saved report sync: deterministic fields in `benchmarks/runs/*.json` match live runs.
3. README sync: benchmark tables and figures in `README.md` and `README_EN.md` match live runs.

Usage:
  python3 scripts/verify_benchmarks.py                  # Check-only (CI default, no file writes)
  python3 scripts/verify_benchmarks.py --update-reports # Refresh benchmarks/runs/*.json
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from benchmarks.eval_dsh_e2e import run_dsh_e2e_benchmark
from benchmarks.eval_llm_judge import verify_llm_judge_report
from benchmarks.eval_plugin_scale import run_plugin_scale_validation
from benchmarks.eval_retrieval import run_retrieval_benchmark
from benchmarks.eval_slicing_fidelity import evaluate_baselines
from tmem.evaluate import BenchmarkEvaluator

TIMING_KEYS = {
    "avg_slice_latency_ms",
    "memories_md_sync_ms",
    "avg_hook_latency_ms",
}


def _strip_timing_fields(obj: Any) -> Any:
    """Recursively remove machine-dependent wall-clock timing keys for deterministic diffing."""
    if isinstance(obj, dict):
        return {
            k: _strip_timing_fields(v)
            for k, v in obj.items()
            if k not in TIMING_KEYS
        }
    if isinstance(obj, list):
        return [_strip_timing_fields(v) for v in obj]
    return obj


def _check_regression_gates(
    dsh_e2e: Dict[str, Any],
    slicing_50: Dict[str, Any],
    slicing_25: Dict[str, Any],
    plugin_scale: Dict[str, Any],
    retrieval: Dict[str, Any],
    cross_eval: Dict[str, Any],
) -> List[str]:
    """Verify minimum quality and latency SLAs across all benchmark suites."""
    errors: List[str] = []

    # 1. DSH E2E
    if dsh_e2e.get("step_reduction_pct", 0.0) < 37.5:
        errors.append(f"[DSH E2E] step_reduction_pct regressed: {dsh_e2e.get('step_reduction_pct')}%")
    if not dsh_e2e.get("l1_triggered") or not dsh_e2e.get("l2_triggered"):
        errors.append("[DSH E2E] l1_triggered and l2_triggered must both be True")
    tel = dsh_e2e.get("telemetry", {})
    if tel.get("l2_resolution_rate") != 1.0 or tel.get("l1_adoption_rate") != 1.0:
        errors.append(f"[DSH E2E] telemetry rates regressed: {tel}")
    if dsh_e2e.get("negative_holdout_fp_pct", 100.0) != 0.0:
        errors.append(f"[DSH E2E] negative_holdout_fp_pct != 0.0: {dsh_e2e.get('negative_holdout_fp_pct')}")
    if dsh_e2e.get("dsh_node_runtime_available"):
        hp_ms = dsh_e2e.get("hot_path_21_steps_ms")
        if hp_ms is None or hp_ms >= 2000.0:
            errors.append(f"[DSH E2E] hot_path_21_steps_ms exceeded SLA: {hp_ms}")

    # 2. Slicing Fidelity (50-trace & 25-trace)
    b0_50 = slicing_50["summary"]["B0_trivial_git_diff"]
    b1_50 = slicing_50["summary"]["B1_tmem_slicer"]
    if b1_50["mean_f1"] <= b0_50["mean_f1"]:
        errors.append(f"[Slicing-50] B1 F1 ({b1_50['mean_f1']}) <= B0 F1 ({b0_50['mean_f1']})")
    if b1_50["mean_precision"] < 90.0 or b1_50["mean_recall"] < 90.0 or b1_50["mean_f1"] < 90.0:
        errors.append(f"[Slicing-50] B1 metrics below 90% floor: {b1_50}")

    b0_25 = slicing_25["summary"]["B0_trivial_git_diff"]
    b1_25 = slicing_25["summary"]["B1_tmem_slicer"]
    if b1_25["mean_f1"] <= b0_25["mean_f1"]:
        errors.append(f"[Slicing-25] B1 F1 ({b1_25['mean_f1']}) <= B0 F1 ({b0_25['mean_f1']})")
    if b1_25["mean_precision"] < 90.0 or b1_25["mean_f1"] < 81.0:
        errors.append(f"[Slicing-25] B1 metrics below floor: {b1_25}")

    # 3. 50-Trajectory Plugin Scale
    corpus = plugin_scale["corpus"]
    ret50 = plugin_scale["retrieval_50_scale"]
    hook = plugin_scale["plugin_hook_validation"]
    if corpus["compiled_playbooks"] < 49:
        errors.append(f"[Plugin-50] compiled_playbooks < 49: {corpus['compiled_playbooks']}")
    if corpus["step_compression_pct"] < 80.0 or corpus["char_compression_pct"] < 90.0:
        errors.append(f"[Plugin-50] compression regressed: {corpus}")
    if corpus["avg_slice_latency_ms"] >= 50.0:
        errors.append(f"[Plugin-50] avg_slice_latency_ms >= 50ms: {corpus['avg_slice_latency_ms']}")
    if ret50["recall_at_1_pct"] < 100.0 or ret50["mrr"] < 1.0:
        errors.append(f"[Plugin-50] retrieval regressed: {ret50}")
    if hook["pre_step_negative_fp_pct"] != 0.0:
        errors.append(f"[Plugin-50] pre_step_negative_fp_pct != 0.0: {hook['pre_step_negative_fp_pct']}")
    if hook["pre_step_anchor_hit_rate_pct"] < 100.0:
        errors.append(f"[Plugin-50] pre_step_anchor_hit_rate_pct < 100.0: {hook['pre_step_anchor_hit_rate_pct']}")
    if hook["l2_interception_rate_pct"] < 100.0 or hook["telemetry"].get("l2_resolution_rate") != 1.0:
        errors.append(f"[Plugin-50] L2 interception/resolution regressed: {hook}")
    if hook["avg_hook_latency_ms"] >= 100.0:
        errors.append(f"[Plugin-50] avg_hook_latency_ms >= 100ms: {hook['avg_hook_latency_ms']}")

    # 4. Independent Retrieval
    rm = retrieval["metrics"]
    if rm["recall_at_1"] < 100.0 or rm["recall_at_3"] < 100.0 or rm["mrr"] < 1.0 or rm["false_positive_rate"] != 0.0:
        errors.append(f"[Retrieval] metrics regressed: {rm}")

    # 5. Cross-Format Evaluation (`tmem.evaluate`)
    if not cross_eval.get("all_passed"):
        errors.append(f"[Evaluate] all_passed is False: {cross_eval}")
    if cross_eval.get("avg_compression_ratio", 0.0) < 50.0:
        errors.append(f"[Evaluate] avg_compression_ratio < 50%: {cross_eval.get('avg_compression_ratio')}")
    if cross_eval.get("total_l2_invariants_extracted", 0) < 7:
        errors.append(f"[Evaluate] total_l2_invariants_extracted < 7: {cross_eval.get('total_l2_invariants_extracted')}")
    if cross_eval.get("avg_slicing_latency_ms", 999.0) >= 50.0:
        errors.append(f"[Evaluate] avg_slicing_latency_ms >= 50ms: {cross_eval.get('avg_slicing_latency_ms')}")

    return errors


def _display_path(path: Path) -> str:
    """Return workspace-relative path string when inside repo, otherwise full path."""
    try:
        return str(path.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(path)


def _check_saved_reports(
    runs_dir: Path,
    slicing_50: Dict[str, Any],
    plugin_scale: Dict[str, Any],
    retrieval: Dict[str, Any],
) -> List[str]:
    """Verify committed JSON reports in `benchmarks/runs/` match live deterministic outputs."""
    errors: List[str] = []
    report_pairs: List[Tuple[str, Dict[str, Any], bool]] = [
        ("slicing_fidelity_report.json", slicing_50, False),
        ("plugin_scale_50_report.json", plugin_scale, True),
        ("retrieval_report.json", retrieval, False),
    ]
    for filename, live_data, strip_timing in report_pairs:
        path = runs_dir / filename
        if not path.exists():
            errors.append(f"[Reports] Missing report file: {_display_path(path)}")
            continue
        saved_data = json.loads(path.read_text(encoding="utf-8"))
        lhs = _strip_timing_fields(saved_data) if strip_timing else saved_data
        rhs = _strip_timing_fields(live_data) if strip_timing else live_data
        if lhs != rhs:
            errors.append(
                f"[Reports] {_display_path(path)} is out of sync with live benchmark output "
                "(run `python3 scripts/verify_benchmarks.py --update-reports`)."
            )
    return errors


def _extract_table_row(text: str, prefix: str) -> Optional[List[str]]:
    """Find a markdown table row containing `prefix` in column 0 and return stripped cells."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and prefix in cells[0]:
            return cells
    return None


def _require_match(
    errors: List[str],
    doc_name: str,
    label: str,
    pattern: str,
    text: str,
    expected_groups: Tuple[str, ...],
) -> None:
    """Assert regex `pattern` appears in `text` and its captured groups match `expected_groups`."""
    m = re.search(pattern, text, re.MULTILINE)
    if not m:
        errors.append(f"[{doc_name}] Missing or unparseable benchmark section for '{label}'")
        return
    actual = tuple(m.groups())
    if actual != expected_groups:
        errors.append(f"[{doc_name}] Stale '{label}': doc={actual} != live={expected_groups}")


def _check_readme_sync(
    readme_path: Path,
    dsh_e2e: Dict[str, Any],
    slicing_50: Dict[str, Any],
    slicing_25: Dict[str, Any],
    plugin_scale: Dict[str, Any],
    retrieval: Dict[str, Any],
    cross_eval: Dict[str, Any],
    llm_judge: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Verify deterministic metrics and slicing table in README.md or README_EN.md match live outputs."""
    errors: List[str] = []
    if not readme_path.exists():
        return [f"[{readme_path.name}] File does not exist"]

    text = readme_path.read_text(encoding="utf-8")
    doc_name = readme_path.name
    is_zh = doc_name == "README.md"

    # 1. Gold-50 Markdown Table (B0, B1, B2)
    table_expectations = [
        ("B0", slicing_50["summary"]["B0_trivial_git_diff"]),
        ("B1", slicing_50["summary"]["B1_tmem_slicer"]),
        ("B2", slicing_50["summary"]["B2_oracle_upper_bound"]),
    ]
    step_suffix = "步" if is_zh else "steps"
    for row_key, stats in table_expectations:
        cells = _extract_table_row(text, f"**{row_key}")
        if not cells or len(cells) < 5:
            errors.append(f"[{doc_name}] Missing Gold-50 table row for {row_key}")
            continue
        expected_cells = (
            f"{stats['mean_precision']}%",
            f"{stats['mean_recall']}%",
            f"{stats['mean_f1']}%",
            f"{stats['avg_steps']} {step_suffix}",
        )
        actual_cells = tuple(c.replace("**", "").strip() for c in cells[1:5])
        if actual_cells != expected_cells:
            errors.append(
                f"[{doc_name}] Gold-50 table row {row_key} mismatch: doc={actual_cells} != live={expected_cells}"
            )
        if llm_judge and len(cells) >= 6:
            judge_cell = cells[5].replace("**", "").strip()
            if row_key == "B0":
                exp_b0_score = f"{llm_judge['mean_llm_scores']['B0_baseline']['overall']:.2f}"
                if judge_cell != exp_b0_score:
                    errors.append(
                        f"[{doc_name}] Gold-50 LLM judge B0 mismatch: doc={judge_cell} != report={exp_b0_score}"
                    )
            elif row_key == "B1":
                exp_b1_score = f"{llm_judge['mean_llm_scores']['B1_tmem']['overall']:.2f}"
                pj = llm_judge["pairwise_llm_judge"]
                exp_win = f"{pj['B1_tmem_wins']}:{pj['B0_baseline_wins']} / {pj['B1_win_rate_pct']}%"
                if exp_b1_score not in judge_cell or exp_win not in judge_cell:
                    errors.append(
                        f"[{doc_name}] Gold-50 LLM judge B1 mismatch: doc={judge_cell} != expected ({exp_b1_score}, {exp_win})"
                    )

    # 2. Gold-25 note
    b0_25 = slicing_25["summary"]["B0_trivial_git_diff"]
    b1_25 = slicing_25["summary"]["B1_tmem_slicer"]
    if is_zh:
        _require_match(
            errors,
            doc_name,
            "Gold-25 metrics",
            r"B1 Precision 亦已从 63\.2% 提升至 ([0-9.]+%)（F1 ([0-9.]+%) > B0 ([0-9.]+%)）",
            text,
            (f"{b1_25['mean_precision']}%", f"{b1_25['mean_f1']}%", f"{b0_25['mean_f1']}%"),
        )
    else:
        _require_match(
            errors,
            doc_name,
            "Gold-25 metrics",
            r"B1 Precision likewise improved from 63\.2% to ([0-9.]+%) \(F1 ([0-9.]+%) vs\. B0's ([0-9.]+%)\)",
            text,
            (f"{b1_25['mean_precision']}%", f"{b1_25['mean_f1']}%", f"{b0_25['mean_f1']}%"),
        )

    # 3. DSH E2E
    tel = dsh_e2e["telemetry"]
    l2_pct = f"{int(round(tel['l2_resolution_rate'] * 100))}%"
    l1_pct = f"{int(round(tel['l1_adoption_rate'] * 100))}%"
    _require_match(
        errors,
        doc_name,
        "DSH E2E step reduction",
        r"\*\*([0-9.]+%)\*\*（(\d+) 步试错基线 → (\d+) 步完成）"
        if is_zh
        else r"\*\*([0-9.]+%)\*\* \((\d+) trial-and-error baseline steps → (\d+) steps with `tmem`\)",
        text,
        (
            f"{dsh_e2e['step_reduction_pct']}%",
            str(dsh_e2e["baseline_steps"]),
            str(dsh_e2e["with_tmem_steps"]),
        ),
    )
    _require_match(
        errors,
        doc_name,
        "DSH E2E telemetry",
        r"L2 报错修复转绿率 \*\*([0-9.]+%)\*\*（`(\d+/\d+)`），L1 共变提醒采纳率 \*\*([0-9.]+%)\*\*（`(\d+/\d+)`）"
        if is_zh
        else r"L2 green error resolution \*\*([0-9.]+%)\*\* \(`(\d+/\d+)`\), L1 co-change partner adoption \*\*([0-9.]+%)\*\* \(`(\d+/\d+)`\)",
        text,
        (
            l2_pct,
            f"{tel['l2_resolved_green']}/{tel['l2_injections']}",
            l1_pct,
            f"{tel['l1_adopted']}/{tel['l1_hints']}",
        ),
    )
    _require_match(
        errors,
        doc_name,
        "DSH E2E negative holdout FP",
        r"泛自然语言负例误注入率\*\*：\*\*([0-9.]+%)\*\*"
        if is_zh
        else r"Natural-Language Holdout False Positive Injection Rate\*\*: \*\*([0-9.]+%)\*\*",
        text,
        (f"{dsh_e2e['negative_holdout_fp_pct']}%",),
    )

    # 4. Plugin Scale 50-Trace
    corpus = plugin_scale["corpus"]
    hook = plugin_scale["plugin_hook_validation"]
    ret50 = plugin_scale["retrieval_50_scale"]
    if is_zh:
        _require_match(
            errors,
            doc_name,
            "Plugin-50 compression",
            r"(\d+) 条编译为 \*\*(\d+)\*\* 条 Playbook.*?原始 \*\*(\d+) 步 → L0 (\d+) 步\*\*（步数精简 \*\*([0-9.]+%)\*\*，字符体积压缩 \*\*([0-9.]+%)\*\*）",
            text,
            (
                str(corpus["total_input_trajectories"]),
                str(corpus["compiled_playbooks"]),
                str(corpus["raw_steps_total"]),
                str(corpus["l0_steps_total"]),
                f"{corpus['step_compression_pct']}%",
                f"{corpus['char_compression_pct']}%",
            ),
        )
        _require_match(
            errors,
            doc_name,
            "Plugin-50 hook validation",
            r"`pre_step` 负例误注入 \*\*([0-9.]+%)\*\*（`\d+/\d+`）、显式锚点召回 \*\*([0-9.]+%)\*\*（`(\d+/\d+)`）；`post_tool_use` L2 同源报错拦截与验证转绿率 \*\*([0-9.]+%)\*\*（`(\d+/\d+)`）",
            text,
            (
                f"{hook['pre_step_negative_fp_pct']}%",
                f"{hook['pre_step_anchor_hit_rate_pct']}%",
                f"{hook['pre_step_anchor_hits']}/{hook['pre_step_anchor_tested']}",
                f"{hook['l2_interception_rate_pct']}%",
                f"{hook['l2_intercepted']}/{hook['l2_cases_tested']}",
            ),
        )
    else:
        _require_match(
            errors,
            doc_name,
            "Plugin-50 compression",
            r"(\d+)/(\d+) compiled playbooks.*?reducing \*\*(\d+) → (\d+) L0 steps\*\* \(\*\*([0-9.]+%)\*\* step reduction, \*\*([0-9.]+%)\*\* RawTrace-to-L0 char compression\)",
            text,
            (
                str(corpus["compiled_playbooks"]),
                str(corpus["total_input_trajectories"]),
                str(corpus["raw_steps_total"]),
                str(corpus["l0_steps_total"]),
                f"{corpus['step_compression_pct']}%",
                f"{corpus['char_compression_pct']}%",
            ),
        )
        _require_match(
            errors,
            doc_name,
            "Plugin-50 hook validation",
            r"\*\*([0-9.]+%)\*\* `pre_step` negative FP \(`\d+/\d+`\); \*\*([0-9.]+%)\*\* explicit anchor recall \(`(\d+/\d+)`\); \*\*([0-9.]+%)\*\* `post_tool_use` L2 interception & green resolution \(`(\d+/\d+)`",
            text,
            (
                f"{hook['pre_step_negative_fp_pct']}%",
                f"{hook['pre_step_anchor_hit_rate_pct']}%",
                f"{hook['pre_step_anchor_hits']}/{hook['pre_step_anchor_tested']}",
                f"{hook['l2_interception_rate_pct']}%",
                f"{hook['l2_intercepted']}/{hook['l2_cases_tested']}",
            ),
        )

    # 5. Independent Retrieval
    rm = retrieval["metrics"]
    if is_zh:
        _require_match(
            errors,
            doc_name,
            "Retrieval metrics",
            r"(\d+) 条含原始需求文本的样本在 `eval_retrieval\.py` 取得 \*\*Recall@1 = ([0-9.]+%) / Recall@3 = ([0-9.]+%) / MRR = ([0-9.]+)\*\*.*?负例误召回率降至 ([0-9.]+%)\*\*（49 条全库压力下 FTS5 负例误召回 \*\*([0-9.]+%)\*\*",
            text,
            (
                str(retrieval["total_eval_queries"]),
                f"{rm['recall_at_1']}%",
                f"{rm['recall_at_3']}%",
                str(rm["mrr"]),
                f"{rm['false_positive_rate']}%",
                f"{ret50['negative_fp_pct']}%",
            ),
        )
    else:
        _require_match(
            errors,
            doc_name,
            "Retrieval metrics",
            r"Across (\d+) issue-query traces in `eval_retrieval\.py`, \*\*Recall@1 = ([0-9.]+%) / Recall@3 = ([0-9.]+%) / MRR = ([0-9.]+)\*\*, and negative holdout FP dropped to \*\*([0-9.]+%)\*\*.*?lexical FTS5 negative FP is \*\*([0-9.]+%)\*\*",
            text,
            (
                str(retrieval["total_eval_queries"]),
                f"{rm['recall_at_1']}%",
                f"{rm['recall_at_3']}%",
                str(rm['mrr']),
                f"{rm['false_positive_rate']}%",
                f"{ret50['negative_fp_pct']}%",
            ),
        )

    # 6. Cross-format `python3 -m tmem.evaluate`
    if is_zh:
        _require_match(
            errors,
            doc_name,
            "Cross-format trajectory count",
            r"跨格式轨迹压缩与运行时开销评测（(\d+) 条真实轨迹）",
            text,
            (str(cross_eval["total_trajectories"]),),
        )
        _require_match(
            errors,
            doc_name,
            "Cross-format compression",
            r"L0 步骤精简率\*\*：\*\*([0-9.]+%)\*\*（(\d+) 步 → (\d+) 步，([\d,]+) → ([\d,]+) 字符，~([\d,]+) tok → ~([\d,]+) tok）",
            text,
            (
                f"{cross_eval['avg_compression_ratio']}%",
                str(cross_eval["total_raw_steps"]),
                str(cross_eval["total_sliced_steps"]),
                f"{cross_eval['total_raw_chars']:,}",
                f"{cross_eval['total_sliced_chars']:,}",
                f"{cross_eval['total_raw_tokens_est']:,}",
                f"{cross_eval['total_sliced_tokens_est']:,}",
            ),
        )
        _require_match(
            errors,
            doc_name,
            "Cross-format L2 pairs",
            r"高熵 L2 因果修复对\*\*：\*\*(\d+) 组\*\*",
            text,
            (str(cross_eval["total_l2_invariants_extracted"]),),
        )
    else:
        _require_match(
            errors,
            doc_name,
            "Cross-format trajectory count",
            r"Cross-Format Trajectory Compression & Runtime Footprint \((\d+) real trajectories\)",
            text,
            (str(cross_eval["total_trajectories"]),),
        )
        _require_match(
            errors,
            doc_name,
            "Cross-format compression",
            r"Mean Step Compression\*\*: \*\*([0-9.]+%)\*\* \((\d+) → (\d+) steps; ([\d,]+) → ([\d,]+) chars; ~([\d,]+) → ~([\d,]+) est\. tokens\)",
            text,
            (
                f"{cross_eval['avg_compression_ratio']}%",
                str(cross_eval["total_raw_steps"]),
                str(cross_eval["total_sliced_steps"]),
                f"{cross_eval['total_raw_chars']:,}",
                f"{cross_eval['total_sliced_chars']:,}",
                f"{cross_eval['total_raw_tokens_est']:,}",
                f"{cross_eval['total_sliced_tokens_est']:,}",
            ),
        )
        _require_match(
            errors,
            doc_name,
            "Cross-format L2 pairs",
            r"High-Entropy L2 Causal Pairs Extracted\*\*: \*\*(\d+) pairs\*\*",
            text,
            (str(cross_eval["total_l2_invariants_extracted"]),),
        )

    return errors


def run_verification(update_reports: bool = False, verbose: bool = False) -> int:
    """Execute all reproducible benchmarks and check gates, saved reports, and README sync."""
    dsh_e2e = run_dsh_e2e_benchmark()
    slicing_50 = evaluate_baselines(WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations_50.json")
    slicing_25 = evaluate_baselines(WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations.json")
    plugin_scale = run_plugin_scale_validation()
    retrieval = run_retrieval_benchmark()
    cross_eval = asdict(BenchmarkEvaluator().run_evaluation())
    llm_judge = verify_llm_judge_report()

    runs_dir = WORKSPACE_ROOT / "benchmarks" / "runs"
    if update_reports:
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "slicing_fidelity_report.json").write_text(
            json.dumps(slicing_50, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (runs_dir / "plugin_scale_50_report.json").write_text(
            json.dumps(plugin_scale, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (runs_dir / "retrieval_report.json").write_text(
            json.dumps(retrieval, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if verbose:
        print("DSH E2E:", json.dumps(dsh_e2e, ensure_ascii=False))
        print("Slicing-50:", json.dumps(slicing_50["summary"], ensure_ascii=False))
        print("Slicing-25:", json.dumps(slicing_25["summary"], ensure_ascii=False))
        print("Plugin-50:", json.dumps(plugin_scale, ensure_ascii=False))
        print("Retrieval:", json.dumps(retrieval["metrics"], ensure_ascii=False))
        cross_brief = copy.deepcopy(cross_eval)
        cross_brief.pop("results", None)
        cross_brief.pop("progressive", None)
        print("Evaluate:", json.dumps(cross_brief, ensure_ascii=False))

    errors: List[str] = []
    errors.extend(
        _check_regression_gates(dsh_e2e, slicing_50, slicing_25, plugin_scale, retrieval, cross_eval)
    )
    errors.extend(_check_saved_reports(runs_dir, slicing_50, plugin_scale, retrieval))
    for doc_name in ("README.md", "README_EN.md"):
        errors.extend(
            _check_readme_sync(
                WORKSPACE_ROOT / doc_name,
                dsh_e2e,
                slicing_50,
                slicing_25,
                plugin_scale,
                retrieval,
                cross_eval,
                llm_judge=llm_judge,
            )
        )

    if errors:
        print("Benchmark CI verification FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        "All reproducible benchmark gates, benchmarks/runs/*.json reports, "
        "and README.md / README_EN.md tables are verified and in sync."
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify tmem benchmarks, reports, and README sync")
    parser.add_argument(
        "--update-reports",
        action="store_true",
        help="Rewrite benchmarks/runs/*.json with current benchmark outputs before verifying",
    )
    parser.add_argument("--verbose", action="store_true", help="Print full benchmark summaries")
    args = parser.parse_args(argv)
    return run_verification(update_reports=args.update_reports, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
