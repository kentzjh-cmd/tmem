"""
P1.1 Slicing Fidelity Evaluation Benchmark.

Compares tmem backward trace slicing against baselines on ground truth annotated trajectories:
- B0: Trivial baseline (git diff modified files + last passing verification command)
- B1: tmem Deterministic Backward Slicer
- B2: Oracle / LLM one-shot upper bound
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

# Ensure proj root is in python path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tmem.adapters.dsh import DshAdapter
from tmem.adapters.swe_bench import SweBenchAdapter
from tmem.models import ActionKind
from tmem.slicer import BackwardTraceSlicer


def evaluate_baselines(gold_path: Path) -> Dict[str, Any]:
    with gold_path.open("r", encoding="utf-8") as f:
        gold_entries = json.load(f)

    adapter_swe = SweBenchAdapter()
    adapter_dsh = DshAdapter()
    slicer = BackwardTraceSlicer()

    b0_list, b1_list, b2_list = [], [], []
    per_case_results = []

    for entry in gold_entries:
        iid = entry["instance_id"]
        gold_steps = set(entry["gold_step_indices"])
        fmt = entry["agent_format"]
        traj_file = WORKSPACE_ROOT / entry["trajectory_file"]

        adapter = adapter_dsh if fmt == "dsh" else adapter_swe
        session = adapter.parse_transcript(str(traj_file), str(WORKSPACE_ROOT))

        # Baseline 0: Only git diff permanent mutated files + last passed test
        perm_files = set(entry.get("perm_target_files") or [])
        b0_steps: Set[int] = set()
        for a in session.actions:
            if a.kind == ActionKind.MUTATE and a.target_file in perm_files:
                b0_steps.add(a.step_idx)
        passing_tests = [
            a
            for a in session.actions
            if a.kind == ActionKind.ENV_EXEC
            and a.exit_code == 0
            and a.command
            and not any(a.command.startswith(x) for x in ("rm ", "goto", "scroll", "exit", "submit", "cd "))
        ]
        if passing_tests:
            b0_steps.add(passing_tests[-1].step_idx)

        # Baseline 1: tmem backward trace slicer
        playbook = slicer.slice_session(session, use_git_ground_truth=False)
        b1_steps = set(slicer.last_sliced_step_indices) if playbook else set()

        # Baseline 2: Oracle gold standard
        b2_steps = set(gold_steps)

        def score(s_set: Set[int], g_set: Set[int]) -> Tuple[float, float, float]:
            if not s_set:
                return 0.0, 0.0, 0.0
            p = len(s_set & g_set) / len(s_set)
            r = len(s_set & g_set) / len(g_set) if g_set else 0.0
            f1 = (2 * p * r) / (p + r) if (p + r) > 0 else 0.0
            return p, r, f1

        p0, r0, f0 = score(b0_steps, gold_steps)
        p1, r1, f1 = score(b1_steps, gold_steps)
        p2, r2, f2 = score(b2_steps, gold_steps)

        b0_list.append({"p": p0, "r": r0, "f1": f0, "steps": len(b0_steps)})
        b1_list.append({"p": p1, "r": r1, "f1": f1, "steps": len(b1_steps)})
        b2_list.append({"p": p2, "r": r2, "f1": f2, "steps": len(b2_steps)})

        per_case_results.append({
            "instance_id": iid,
            "raw_steps": len(session.actions),
            "gold_steps": len(gold_steps),
            "b0": {"p": round(p0 * 100, 1), "r": round(r0 * 100, 1), "f1": round(f0 * 100, 1), "steps": len(b0_steps)},
            "b1": {"p": round(p1 * 100, 1), "r": round(r1 * 100, 1), "f1": round(f1 * 100, 1), "steps": len(b1_steps)},
            "b2": {"p": round(p2 * 100, 1), "r": round(r2 * 100, 1), "f1": round(f2 * 100, 1), "steps": len(b2_steps)},
        })

    n = max(len(gold_entries), 1)

    def aggregate(stat_list: List[Dict[str, float]]) -> Dict[str, float]:
        return {
            "mean_precision": round(sum(x["p"] for x in stat_list) / n * 100, 1),
            "mean_recall": round(sum(x["r"] for x in stat_list) / n * 100, 1),
            "mean_f1": round(sum(x["f1"] for x in stat_list) / n * 100, 1),
            "avg_steps": round(sum(x["steps"] for x in stat_list) / n, 1),
        }

    return {
        "total_instances": n,
        "summary": {
            "B0_trivial_git_diff": aggregate(b0_list),
            "B1_tmem_slicer": aggregate(b1_list),
            "B2_oracle_upper_bound": aggregate(b2_list),
        },
        "per_case": per_case_results,
    }


def format_report_markdown(data: Dict[str, Any]) -> str:
    md = []
    md.append("# 🎯 P1.1 轨迹切片保真度评测报告 (Slicing Fidelity Benchmark)")
    md.append("")
    md.append(f"- **评测样本量**: {data['total_instances']} 条轨迹 (覆盖 SWE-bench `online_20` 及 live `dsh` 会话)")
    md.append("- **对比基线**:")
    md.append("  - **B0**: 平凡基线 (`git diff` 变更文件 + 最后一条通过测试命令)")
    md.append("  - **B1**: `tmem` 确定性后向切片器 (`l0_critical_path`)")
    md.append("  - **B2**: Oracle 上限参考 (人工标注金标准)")
    md.append("")
    md.append("### 基线对比总表")
    md.append("")
    md.append("| 基线模型 / 策略 | Precision (查准率) | Recall (查全率) | F1-Score | 平均保留步数 | 策略特性分析 |")
    md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

    b0 = data["summary"]["B0_trivial_git_diff"]
    b1 = data["summary"]["B1_tmem_slicer"]
    b2 = data["summary"]["B2_oracle_upper_bound"]

    md.append(
        f"| **B0 (git diff + last test)** | **{b0['mean_precision']}%** | {b0['mean_recall']}% | **{b0['mean_f1']}%** | {b0['avg_steps']} 步 | 高查准、低查全（丢失定位所需的关键上下文读操作与环境安装） |"
    )
    md.append(
        f"| **B1 (tmem Slicer)** | {b1['mean_precision']}% | **{b1['mean_recall']}%** | {b1['mean_f1']}% | {b1['avg_steps']} 步 | 高查全、保留了定位所需的代码阅读与工具链步骤 |"
    )
    md.append(
        f"| **B2 (Oracle Upper Bound)** | {b2['mean_precision']}% | {b2['mean_recall']}% | {b2['mean_f1']}% | {b2['avg_steps']} 步 | 人工标注理想上限 |"
    )
    md.append("")
    md.append("### 逐样本切片明细")
    md.append("")
    md.append("| 实例 ID | 原始步数 | 金标准步数 | B0 (P / R / F1) | B1 (P / R / F1) |")
    md.append("| :--- | :--- | :--- | :--- | :--- |")
    for r in data["per_case"]:
        b0_str = f"{r['b0']['p']}% / {r['b0']['r']}% / {r['b0']['f1']}%"
        b1_str = f"{r['b1']['p']}% / {r['b1']['r']}% / {r['b1']['f1']}%"
        md.append(f"| `{r['instance_id']}` | {r['raw_steps']} | {r['gold_steps']} | {b0_str} | {b1_str} |")
    md.append("")
    return "\n".join(md)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate slicing fidelity on gold annotations")
    parser.add_argument(
        "--gold-path",
        default=str(WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations.json"),
        help="Path to gold annotations JSON",
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--output", default=None, help="Save report to file")
    args = parser.parse_args(argv)

    gold_path = Path(args.gold_path)
    if not gold_path.exists():
        print(f"Error: Gold annotations not found at {gold_path}", file=sys.stderr)
        return 1

    data = evaluate_baselines(gold_path)

    # Save to benchmarks/runs/
    runs_dir = WORKSPACE_ROOT / "benchmarks" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    report_file = runs_dir / "slicing_fidelity_report.json"
    with report_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        md_text = format_report_markdown(data)
        print(md_text)
        if args.output:
            Path(args.output).write_text(md_text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
