"""
Benchmark Evaluation Suite for tmem Deterministic Execution Memory.

Outputs purely descriptive, empirical metrics on public SWE-bench, ATIF, and live agent traces:
- Raw execution steps vs L0 critical path steps
- Measured character counts and empirical token estimates (chars // 4, zero arbitrary constants)
- Measured deterministic slicing latency (ms)
- Ground-truth L1 file co-change count and verified high-entropy L2 causal repairs
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
from tmem.raw_storage import RawTraceStore, format_raw_trace_replay
from tmem.slicer import BackwardTraceSlicer
from tmem.store import ProjectMemoryStore


@dataclass
class ProgressiveDisclosureResult:
    instance_id: str
    dataset_name: str
    raw_json_chars: int
    playbook_json_chars: int
    index_chars: int
    raw_storage_verified: bool
    replay_rendered: bool


@dataclass
class ProgressiveDisclosureSummary:
    total_instances: int
    avg_raw_json_chars: int
    avg_playbook_json_chars: int
    raw_fidelity_rate: float
    living_index_latency_ms: float
    results: List[ProgressiveDisclosureResult] = field(default_factory=list)


@dataclass
class TrajectoryEvaluationResult:
    dataset_name: str
    instance_id: str
    agent_format: str
    raw_steps: int
    sliced_steps: int
    compression_ratio: float
    raw_chars: int
    sliced_chars: int
    raw_tokens_est: int
    sliced_tokens_est: int
    slicing_latency_ms: float
    l1_files_count: int
    l2_causal_repairs_count: int
    passed_verification: bool
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkSummary:
    total_trajectories: int
    total_raw_steps: int
    total_sliced_steps: int
    max_trajectory_length: int
    min_trajectory_length: int
    avg_compression_ratio: float
    avg_slicing_latency_ms: float
    total_raw_chars: int
    total_sliced_chars: int
    total_raw_tokens_est: int
    total_sliced_tokens_est: int
    total_l2_invariants_extracted: int
    all_passed: bool
    results: List[TrajectoryEvaluationResult] = field(default_factory=list)
    progressive: Optional[ProgressiveDisclosureSummary] = None


class BenchmarkEvaluator:
    """Automated descriptive evaluator for coding agent execution trajectories."""

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

        temp_dir = tempfile.mkdtemp(prefix="tmem_bench_")
        try:
            store = ProjectMemoryStore(workspace_root=temp_dir)
            results: List[TrajectoryEvaluationResult] = []
            progressive_results: List[ProgressiveDisclosureResult] = []

            for fpath in files:
                res, prog_res = self._evaluate_single_file(fpath, temp_dir, store)
                results.append(res)
                progressive_results.append(prog_res)

            total = len(results)
            total_raw = sum(r.raw_steps for r in results)
            total_sliced = sum(r.sliced_steps for r in results)
            max_len = max((r.raw_steps for r in results), default=0)
            min_len = min((r.raw_steps for r in results), default=0)
            avg_cr = sum(r.compression_ratio for r in results) / total if total else 0.0
            avg_slice_lat = sum(r.slicing_latency_ms for r in results) / total if total else 0.0
            total_l2 = sum(r.l2_causal_repairs_count for r in results)
            total_raw_chars = sum(r.raw_chars for r in results)
            total_sliced_chars = sum(r.sliced_chars for r in results)
            total_raw_tok = sum(r.raw_tokens_est for r in results)
            total_sliced_tok = sum(r.sliced_tokens_est for r in results)

            # Descriptive sanity check: latency < 100ms and non-empty critical path
            all_passed = (
                total > 0
                and avg_slice_lat < 100.0
                and all(r.sliced_steps > 0 for r in results)
            )

            # Measure living memory index synchronization latency
            t_sync_0 = time.perf_counter()
            store.sync_memories_markdown()
            sync_latency_ms = (time.perf_counter() - t_sync_0) * 1000.0

            total_prog = len(progressive_results)
            avg_raw_chars = sum(p.raw_json_chars for p in progressive_results) / total_prog if total_prog else 0
            avg_pb_chars = sum(p.playbook_json_chars for p in progressive_results) / total_prog if total_prog else 0
            raw_fid = (
                sum(1 for p in progressive_results if p.raw_storage_verified) / total_prog * 100.0
                if total_prog
                else 100.0
            )

            prog_summary = ProgressiveDisclosureSummary(
                total_instances=total_prog,
                avg_raw_json_chars=int(round(avg_raw_chars)),
                avg_playbook_json_chars=int(round(avg_pb_chars)),
                raw_fidelity_rate=round(raw_fid, 2),
                living_index_latency_ms=round(sync_latency_ms, 2),
                results=progressive_results,
            )

            return BenchmarkSummary(
                total_trajectories=total,
                total_raw_steps=total_raw,
                total_sliced_steps=total_sliced,
                max_trajectory_length=max_len,
                min_trajectory_length=min_len,
                avg_compression_ratio=round(avg_cr, 2),
                avg_slicing_latency_ms=round(avg_slice_lat, 2),
                total_raw_chars=total_raw_chars,
                total_sliced_chars=total_sliced_chars,
                total_raw_tokens_est=total_raw_tok,
                total_sliced_tokens_est=total_sliced_tok,
                total_l2_invariants_extracted=total_l2,
                all_passed=all_passed,
                results=results,
                progressive=prog_summary,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _evaluate_single_file(
        self,
        file_path: Path,
        temp_workspace: str,
        store: ProjectMemoryStore,
    ) -> Tuple[
        TrajectoryEvaluationResult,
        ProgressiveDisclosureResult,
    ]:
        stem = file_path.stem.lower()
        if "dsh" in stem:
            agent_name = "dsh"
        elif "atif" in stem:
            agent_name = "atif"
        else:
            agent_name = "swe_bench"

        adapter = get_adapter(agent_name)
        session: ParsedSession = adapter.parse_transcript(str(file_path), temp_workspace)

        t0 = time.perf_counter()
        slicer = BackwardTraceSlicer()
        playbook = slicer.slice_session(session, use_git_ground_truth=False)
        slicing_ms = (time.perf_counter() - t0) * 1000.0

        if not playbook:
            raise RuntimeError(f"Slicer returned None for benchmark trajectory {file_path}")

        raw_count = max(len(session.actions), 1)
        sliced_count = len(playbook.l0_critical_path)
        cr = max(0.0, (1.0 - (sliced_count / raw_count)) * 100.0)

        # Measured character counts & empirical token estimates (~chars // 4, zero arbitrary constants)
        raw_chars = sum(
            len(str(a.command or "")) + len(str(a.target_file or "")) + len(str(a.output_snippet or ""))
            for a in session.actions
        )
        sliced_chars = sum(
            len(str(s.get("command") or "")) + len(str(s.get("target") or ""))
            for s in playbook.l0_critical_path
        )
        raw_tok = max(1, raw_chars // 4)
        sliced_tok = max(1, sliced_chars // 4)

        # Persist to isolated store for index sync test
        store.save_playbook(playbook, target_scope="project")

        res = TrajectoryEvaluationResult(
            dataset_name=file_path.stem,
            instance_id=session.session_id,
            agent_format=agent_name,
            raw_steps=raw_count,
            sliced_steps=sliced_count,
            compression_ratio=round(cr, 1),
            raw_chars=raw_chars,
            sliced_chars=sliced_chars,
            raw_tokens_est=raw_tok,
            sliced_tokens_est=sliced_tok,
            slicing_latency_ms=round(slicing_ms, 2),
            l1_files_count=len(playbook.touched_files),
            l2_causal_repairs_count=len(playbook.l2_causal_repairs),
            passed_verification=bool(playbook.l1_verification_cmds),
            details={
                "l0_steps": playbook.l0_critical_path,
                "l1_topology": playbook.l1_cochange_topology,
                "l2_repairs": [asdict(r) for r in playbook.l2_causal_repairs],
            },
        )

        # Progressive Disclosure evaluation
        raw_tr = slicer.build_raw_trace(session, playbook)
        raw_store = RawTraceStore(temp_workspace)
        raw_store.save_raw_trace(raw_tr)
        loaded_raw = raw_store.load_raw_trace(playbook.trace_id)
        raw_verified = loaded_raw is not None and len(loaded_raw.actions) == len(session.actions)
        replay_str = format_raw_trace_replay(raw_tr)
        replay_ok = len(replay_str) > 50

        pb_json_chars = len(json.dumps(playbook.to_dict()))
        raw_json_chars = len(json.dumps(raw_tr.to_dict()))
        index_chars = 120

        prog_res = ProgressiveDisclosureResult(
            instance_id=session.session_id,
            dataset_name=file_path.stem,
            raw_json_chars=raw_json_chars,
            playbook_json_chars=pb_json_chars,
            index_chars=index_chars,
            raw_storage_verified=raw_verified,
            replay_rendered=replay_ok,
        )

        return res, prog_res


def format_markdown_report(summary: BenchmarkSummary) -> str:
    """Format evaluation results into a descriptive, pragmatic markdown report."""
    md = []
    md.append("## 📊 轨迹切片与特征描述评测 (Trajectory Slicing & Profiling)")
    md.append("")
    md.append(f"- **评测轨迹总量**: {summary.total_trajectories} 条真实 Agent 轨迹 (原始交互总轮次: {summary.total_raw_steps} 步，长度区间: {summary.min_trajectory_length} ~ {summary.max_trajectory_length} 步)")
    md.append(f"- **切片后 L0 步骤总计**: {summary.total_sliced_steps} 步 (平均精简率: {summary.avg_compression_ratio}%)")
    md.append(f"- **实测字符数**: {summary.total_raw_chars} 字符 (原始) -> {summary.total_sliced_chars} 字符 (切片后)")
    md.append(f"- **预估 Token 数 (chars // 4)**: ~{summary.total_raw_tokens_est} tok (原始) -> ~{summary.total_sliced_tokens_est} tok (切片后)")
    md.append(f"- **提炼高熵 L2 因果修复对**: **{summary.total_l2_invariants_extracted} 组** (经堆栈交集与源码约束过滤)")
    md.append(f"- **平均切片耗时**: **{summary.avg_slicing_latency_ms} ms** (纯本地确定性运行时，零 LLM Token 耗时)")
    md.append(f"- **基准校验状态**: **{'✅ PASSED' if summary.all_passed else '❌ FAILED'}**")
    md.append("")
    md.append("> **方法学说明**：切片器以落盘变更与最终通过的测试为锚点，剪除探索性读操作与无关命令。本报告仅展示客观描述性数字（原始步数、L0 步数、实测字符数/Token 估算及切片耗时），不报告自证明的满分推断指标。")
    md.append("")
    md.append("### 轨迹切片描述性统计明细")
    md.append("")
    md.append("| 实例 / 轨迹来源 | 格式 | 原始步数 | L0 步数 | 精简率 | 原始字符数 | 切片字符数 | 预估 Token (原/切) | 协同文件 (L1) | 高熵修复对 (L2) | 验证命令提取 | 切片耗时 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|:---:|---|")
    for r in summary.results:
        md.append(
            f"| `{r.instance_id}` | `{r.agent_format}` | {r.raw_steps} | {r.sliced_steps} | "
            f"**{r.compression_ratio}%** | {r.raw_chars} | {r.sliced_chars} | "
            f"~{r.raw_tokens_est} / ~{r.sliced_tokens_est} | {r.l1_files_count} 个文件 | "
            f"{r.l2_causal_repairs_count} 组 | {'✅' if r.passed_verification else '-'} | {r.slicing_latency_ms}ms |"
        )
    md.append("")

    if summary.progressive:
        prog = summary.progressive
        md.append("## ⚡ 分级渐进披露存储体积描述 (Progressive Disclosure Footprint)")
        md.append("")
        md.append(f"- **评测记忆条目**: {prog.total_instances} 条轨迹")
        md.append(f"- **平均 Playbook JSON 体积**: ~{prog.avg_playbook_json_chars} 字符")
        md.append(f"- **平均 Raw Trace JSON 体积**: ~{prog.avg_raw_json_chars} 字符 (包含完整 tool_calls、输出与 Git Diff)")
        md.append(f"- **原始轨迹存储校验通过率**: **{prog.raw_fidelity_rate}%**")
        md.append(f"- **活文档索引原子同步耗时**: **{prog.living_index_latency_ms} ms**")
        md.append("")
        md.append("### 分级存储明细")
        md.append("")
        md.append("| 实例 / 轨迹 | Raw Trace JSON 字符 | Playbook JSON 字符 | 存储校验 | 回放渲染 |")
        md.append("|---|---|---|---|---|")
        for p in prog.results:
            md.append(
                f"| `{p.instance_id}` | {p.raw_json_chars} 字符 | {p.playbook_json_chars} 字符 | "
                f"{'✅ 通过' if p.raw_storage_verified else '❌ 失败'} | "
                f"{'✅ 通过' if p.replay_rendered else '❌ 失败'} |"
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
