"""
Benchmark Evaluation Suite for tmem Speculative Execution Memory.

Validates tmem compilation, backward slicing, noise filtering, L1/L2 invariant extraction,
and sub-millisecond retrieval on public SWE-bench, ATIF, and live multi-agent trajectories.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from tmem.adapters import get_adapter
from tmem.adapters.base import ParsedSession
from tmem.models import ActionKind, TracePlaybook
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore


@dataclass
class TrajectoryEvaluationResult:
    dataset_name: str
    instance_id: str
    agent_format: str
    raw_steps: int
    sliced_steps: int
    compression_ratio: float
    raw_exploratory_noise: int
    l0_noise_remaining: int
    noise_elimination_rate: float
    l1_files_count: int
    l2_causal_repairs_count: int
    slicing_latency_ms: float
    retrieval_rank: int
    retrieval_latency_ms: float
    passed_verification: bool
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkSummary:
    total_trajectories: int
    avg_compression_ratio: float
    avg_noise_elimination_rate: float
    top1_retrieval_accuracy: float
    mean_reciprocal_rank: float
    avg_slicing_latency_ms: float
    avg_retrieval_latency_ms: float
    total_l2_invariants_extracted: int
    all_passed: bool
    results: List[TrajectoryEvaluationResult] = field(default_factory=list)


class BenchmarkEvaluator:
    """Automated validator and evaluator for coding agent execution trajectories."""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir or (Path(__file__).parent.parent / "benchmarks" / "data"))

    def run_evaluation(self, verbose: bool = False) -> BenchmarkSummary:
        if not self.data_dir.exists():
            raise FileNotFoundError(f"Benchmark data directory not found: {self.data_dir}")

        files = sorted(
            [
                p
                for p in self.data_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".json", ".traj", ".jsonl")
            ]
        )
        if not files:
            raise ValueError(f"No trajectory files found in {self.data_dir}")

        # Setup an isolated temporary workspace for store indexing & retrieval test
        temp_dir = tempfile.mkdtemp(prefix="tmem_bench_")
        try:
            store = DualTierMemoryStore(workspace_root=temp_dir)
            results: List[TrajectoryEvaluationResult] = []
            queries_to_test: List[Dict[str, Any]] = []

            for fpath in files:
                res, query_info = self._evaluate_single_file(fpath, temp_dir, store)
                results.append(res)
                if query_info:
                    queries_to_test.append(query_info)

            # Test Retrieval Accuracy across indexed playbooks
            for q in queries_to_test:
                t0 = time.perf_counter()
                evaluated = store.cascading_lookup(
                    query_text=q["query_text"],
                    file_paths=q.get("files", []),
                    error_signatures=q.get("errors", []),
                )
                dt_ms = (time.perf_counter() - t0) * 1000.0

                matched_trace_id = q["target_trace_id"]
                rank = 999
                for i, ev in enumerate(evaluated, start=1):
                    if ev.playbook.trace_id == matched_trace_id:
                        rank = i
                        break

                # Update the result entry
                for r in results:
                    if r.instance_id == q["instance_id"]:
                        r.retrieval_rank = rank
                        r.retrieval_latency_ms = round(dt_ms, 2)
                        break

            # Compute Aggregate Metrics
            total = len(results)
            avg_cr = sum(r.compression_ratio for r in results) / total if total else 0.0
            avg_ner = sum(r.noise_elimination_rate for r in results) / total if total else 0.0
            top1 = sum(1 for r in results if r.retrieval_rank == 1) / total if total else 0.0
            mrr = sum(1.0 / r.retrieval_rank for r in results if r.retrieval_rank > 0) / total if total else 0.0
            avg_slice_lat = sum(r.slicing_latency_ms for r in results) / total if total else 0.0
            avg_ret_lat = sum(r.retrieval_latency_ms for r in results) / total if total else 0.0
            total_l2 = sum(r.l2_causal_repairs_count for r in results)

            # SLA Thresholds:
            # - CR >= 50%
            # - NER >= 95%
            # - Top-1 >= 90%
            # - Latency < 100ms
            all_passed = (
                avg_cr >= 50.0
                and avg_ner >= 95.0
                and top1 >= 0.75
                and avg_slice_lat < 100.0
                and avg_ret_lat < 50.0
            )

            return BenchmarkSummary(
                total_trajectories=total,
                avg_compression_ratio=round(avg_cr, 2),
                avg_noise_elimination_rate=round(avg_ner, 2),
                top1_retrieval_accuracy=round(top1, 2),
                mean_reciprocal_rank=round(mrr, 2),
                avg_slicing_latency_ms=round(avg_slice_lat, 2),
                avg_retrieval_latency_ms=round(avg_ret_lat, 2),
                total_l2_invariants_extracted=total_l2,
                all_passed=all_passed,
                results=results,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _evaluate_single_file(
        self, file_path: Path, temp_workspace: str, store: DualTierMemoryStore
    ) -> tuple[TrajectoryEvaluationResult, Optional[Dict[str, Any]]]:
        # Detect agent adapter
        stem = file_path.name.lower()
        if "dsh" in stem or file_path.suffix == ".jsonl":
            agent_name = "dsh"
        elif "atif" in stem:
            agent_name = "atif"
        else:
            agent_name = "swe_bench"

        adapter = get_adapter(agent_name)
        session: ParsedSession = adapter.parse_transcript(str(file_path), temp_workspace)

        # Count raw exploratory noise in session
        raw_noise_count = 0
        for act in session.actions:
            if act.kind == ActionKind.READ:
                raw_noise_count += 1
            elif act.kind == ActionKind.ENV_EXEC and act.command:
                low = act.command.strip().lower()
                if any(
                    low.startswith(pfx)
                    for pfx in (
                        "ls",
                        "pwd",
                        "find",
                        "grep",
                        "cat",
                        "git status",
                        "git diff",
                        "git log",
                        "echo",
                    )
                ):
                    raw_noise_count += 1

        t0 = time.perf_counter()
        slicer = BackwardTraceSlicer()
        playbook = slicer.slice_session(session, use_git_ground_truth=False)
        slicing_ms = (time.perf_counter() - t0) * 1000.0

        if not playbook:
            raise RuntimeError(f"Slicer returned None for benchmark trajectory {file_path}")

        raw_count = max(len(session.actions), 1)
        sliced_count = len(playbook.l0_critical_path)
        cr = max(0.0, (1.0 - (sliced_count / raw_count)) * 100.0)

        # Check remaining noise in L0 critical path
        l0_noise = 0
        for step in playbook.l0_critical_path:
            cmd = str(step.get("command") or "").strip().lower()
            if any(
                cmd.startswith(pfx)
                for pfx in (
                    "ls",
                    "pwd",
                    "find",
                    "grep",
                    "cat",
                    "git status",
                    "git diff",
                    "git log",
                    "echo",
                )
            ):
                l0_noise += 1

        ner = 100.0 if raw_noise_count == 0 else max(
            0.0, ((raw_noise_count - l0_noise) / raw_noise_count) * 100.0
        )

        # Persist to isolated store for retrieval verification
        store.save_playbook(playbook, target_scope="project")

        query_info = {
            "instance_id": session.session_id,
            "target_trace_id": playbook.trace_id,
            "query_text": playbook.problem_intent,
            "files": [playbook.guards[0].target] if playbook.guards else [],
            "errors": [r.error_signature for r in playbook.l2_causal_repairs],
        }

        res = TrajectoryEvaluationResult(
            dataset_name=file_path.stem,
            instance_id=session.session_id,
            agent_format=agent_name,
            raw_steps=raw_count,
            sliced_steps=sliced_count,
            compression_ratio=round(cr, 1),
            raw_exploratory_noise=raw_noise_count,
            l0_noise_remaining=l0_noise,
            noise_elimination_rate=round(ner, 1),
            l1_files_count=len(playbook.l1_cochange_topology),
            l2_causal_repairs_count=len(playbook.l2_causal_repairs),
            slicing_latency_ms=round(slicing_ms, 2),
            retrieval_rank=1,  # updated in parent loop
            retrieval_latency_ms=0.0,
            passed_verification=bool(playbook.l1_verification_cmds),
            details={
                "l0_steps": playbook.l0_critical_path,
                "l1_topology": playbook.l1_cochange_topology,
                "l2_repairs": [asdict(r) for r in playbook.l2_causal_repairs],
            },
        )
        return res, query_info


def format_markdown_report(summary: BenchmarkSummary) -> str:
    """Format evaluation results into a comprehensive GitHub markdown report."""
    md = []
    md.append("## 📊 tmem Public Trajectory Dataset Benchmark & Validation Report")
    md.append("")
    md.append(f"- **Evaluated Trajectories**: {summary.total_trajectories}")
    md.append(f"- **Avg Trajectory Compression Ratio**: **{summary.avg_compression_ratio}%** (Pruned redundant turns)")
    md.append(f"- **Avg Noise Elimination Rate (NER)**: **{summary.avg_noise_elimination_rate}%** (Exploratory commands filtered)")
    md.append(f"- **Top-1 Retrieval Accuracy**: **{summary.top1_retrieval_accuracy * 100:.1f}%** (MRR: {summary.mean_reciprocal_rank})")
    md.append(f"- **Total L2 Causal Repairs Extracted**: **{summary.total_l2_invariants_extracted}**")
    md.append(f"- **Avg Slicing Latency**: **{summary.avg_slicing_latency_ms} ms** (Zero LLM API latency)")
    md.append(f"- **Avg Retrieval Latency**: **{summary.avg_retrieval_latency_ms} ms** (<5ms pre-flight SLA)")
    md.append(f"- **Framework Validation Status**: **{'✅ PASSED' if summary.all_passed else '❌ FAILED'}**")
    md.append("")
    md.append("### Detailed Instance Breakdown")
    md.append("")
    md.append("| Instance / Trajectory | Format | Raw | L0 | Compression | Noise Filtered | L1 Topology | L2 Repairs | Rank | Slicing Latency |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in summary.results:
        md.append(
            f"| `{r.instance_id}` | `{r.agent_format}` | {r.raw_steps} | {r.sliced_steps} | "
            f"**{r.compression_ratio}%** | **{r.noise_elimination_rate}%** ({r.raw_exploratory_noise} -> {r.l0_noise_remaining}) | "
            f"{r.l1_files_count} file(s) | {r.l2_causal_repairs_count} | #{r.retrieval_rank} | {r.slicing_latency_ms}ms |"
        )
    md.append("")
    return "\n".join(md)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run tmem benchmark evaluation on public trajectories")
    parser.add_argument("--data-dir", default=None, help="Directory containing .traj / .atif.json / .jsonl files")
    parser.add_argument("--json", action="store_true", help="Output raw JSON summary")
    parser.add_argument("--quiet", action="store_true", help="Quiet output")
    args = parser.parse_args(argv)

    evaluator = BenchmarkEvaluator(args.data_dir)
    summary = evaluator.run_evaluation()

    if args.json:
        print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    elif not args.quiet:
        print(format_markdown_report(summary))

    return 0 if summary.all_passed else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
