"""
P1.3 Retrieval Benchmark.

Evaluates deterministic memory retrieval using genuine user issue queries
(never playbook's self-generated intent titles):
- Reports Recall@1, Recall@3, and Mean Reciprocal Rank (MRR)
- Evaluates False Positive Rate (mis-retrieval rate) on unrelated tasks
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tmem.adapters.dsh import DshAdapter
from tmem.adapters.swe_bench import SweBenchAdapter
from tmem.slicer import BackwardTraceSlicer
from tmem.store import ProjectMemoryStore


NEGATIVE_HOLDOUT_QUERIES = [
    "How to prepare authentic Italian carbonara with guanciale and pecorino",
    "Deploy high-availability Kubernetes cluster on AWS using Terraform and EKS",
    "Resolve lifetime elision and borrow checker conflict in Rust async trait",
    "Configure Nginx reverse proxy with Let's Encrypt automated TLS certificates",
    "Train a transformer language model with deepspeed pipeline parallelism",
    "Docker compose configuration for PostgreSQL read-write replica pool",
]


def run_retrieval_benchmark(data_dir: Path | None = None) -> Dict[str, Any]:
    target_data_dir = data_dir or (WORKSPACE_ROOT / "benchmarks" / "data" / "online_20")
    files = sorted(target_data_dir.glob("*.traj"))

    temp_dir = tempfile.mkdtemp(prefix="tmem_retrieval_eval_")
    try:
        store = ProjectMemoryStore(workspace_root=temp_dir)
        adapter = SweBenchAdapter()
        slicer = BackwardTraceSlicer()

        indexed_playbooks = []
        test_queries = []

        for fpath in files:
            session = adapter.parse_transcript(str(fpath), temp_dir)
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            if not pb:
                continue

            store.save_playbook(pb, target_scope="project")
            indexed_playbooks.append(pb)

            # Raw issue statement (independent of compiled playbook problem_intent)
            issue_text = session.user_intents[0] if session.user_intents else ""
            test_queries.append({
                "instance_id": pb.trace_id,
                "dataset_instance": session.session_id,
                "raw_issue_query": issue_text[:200].strip(),
                "target_files": list(pb.touched_files),
            })

        # Also add DSH sessions if present
        dsh_adapter = DshAdapter()
        for dsh_f in (WORKSPACE_ROOT / "benchmarks" / "data").glob("dsh_*.jsonl"):
            session = dsh_adapter.parse_transcript(str(dsh_f), temp_dir)
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            if pb:
                store.save_playbook(pb, target_scope="project")
                indexed_playbooks.append(pb)
                issue_text = session.user_intents[0] if session.user_intents else ""
                test_queries.append({
                    "instance_id": pb.trace_id,
                    "dataset_instance": session.session_id,
                    "raw_issue_query": issue_text[:200].strip(),
                    "target_files": list(pb.touched_files),
                })

        # Evaluate Retrieval on Raw Issue Statements
        total_queries = len(test_queries)
        hits_at_1 = 0
        hits_at_3 = 0
        reciprocal_ranks = []
        per_query_results = []

        for q in test_queries:
            query_str = q["raw_issue_query"]
            target_id = q["instance_id"]

            evaluated = store.cascading_lookup(
                query_text=query_str,
                limit=5,
            )
            ranked_ids = [ev.playbook.trace_id for ev in evaluated]

            rank = 999
            if target_id in ranked_ids:
                rank = ranked_ids.index(target_id) + 1

            if rank == 1:
                hits_at_1 += 1
            if rank <= 3:
                hits_at_3 += 1

            reciprocal_ranks.append(1.0 / rank if rank <= 10 else 0.0)

            per_query_results.append({
                "dataset_instance": q["dataset_instance"],
                "target_trace_id": target_id,
                "query_snippet": query_str[:80] + "...",
                "retrieval_rank": rank if rank <= 10 else ">10",
                "top_retrieved": ranked_ids[0] if ranked_ids else "NONE",
            })

        mean_rr = sum(reciprocal_ranks) / total_queries if total_queries else 0.0
        recall_at_1 = hits_at_1 / total_queries * 100 if total_queries else 0.0
        recall_at_3 = hits_at_3 / total_queries * 100 if total_queries else 0.0

        # Evaluate False Positive / Mis-retrieval rate on unrelated negative holdouts
        false_positives = 0
        negative_results = []
        for neg_q in NEGATIVE_HOLDOUT_QUERIES:
            res = store.cascading_lookup(query_text=neg_q, limit=3)
            # A false positive is counted if L0 guards pass or if a confidence > 0.6 is claimed
            l0_passed = [r for r in res if r.l0_valid]
            high_conf = [r for r in res if r.playbook.confidence > 0.6]
            is_fp = bool(l0_passed or high_conf)
            if is_fp:
                false_positives += 1
            negative_results.append({
                "query": neg_q,
                "retrieved_count": len(res),
                "false_positive": is_fp,
            })

        fp_rate = (false_positives / len(NEGATIVE_HOLDOUT_QUERIES)) * 100

        return {
            "total_indexed_playbooks": len(indexed_playbooks),
            "total_eval_queries": total_queries,
            "metrics": {
                "recall_at_1": round(recall_at_1, 1),
                "recall_at_3": round(recall_at_3, 1),
                "mrr": round(mean_rr, 3),
                "negative_holdout_queries": len(NEGATIVE_HOLDOUT_QUERIES),
                "false_positive_rate": round(fp_rate, 1),
            },
            "queries": per_query_results,
            "negative_queries": negative_results,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def format_markdown_report(data: Dict[str, Any]) -> str:
    m = data["metrics"]
    md = []
    md.append("# 🔍 P1.3 独立任务检索评测报告 (Independent Issue Retrieval Benchmark)")
    md.append("")
    md.append("> **方法学说明**：查询完全使用下游任务的 **原始 Issue / 需求文本**，绝不使用 Playbook 自编的意图标题。")
    md.append("")
    md.append(f"- **索引记忆总量**: {data['total_indexed_playbooks']} 条真实工程 Playbook")
    md.append(f"- **测试查询总量**: {data['total_eval_queries']} 条独立任务 Issue 原文")
    md.append(f"- **Top-1 召回率 (Recall@1)**: **{m['recall_at_1']}%**")
    md.append(f"- **Top-3 召回率 (Recall@3)**: **{m['recall_at_3']}%**")
    md.append(f"- **平均倒数排名 (MRR)**: **{m['mrr']}**")
    md.append(f"- **不相关任务误召回率 (False Positive Rate)**: **{m['false_positive_rate']}%** (基于 {m['negative_holdout_queries']} 条跨领域负样本)")
    md.append("")
    md.append("### 检索结果明细")
    md.append("")
    md.append("| 评测任务实例 | 目标 Trace ID | 检索排名 | 首选召回结果 | Issue 查询摘要 |")
    md.append("| :--- | :--- | :---: | :--- | :--- |")
    for q in data["queries"]:
        status_icon = "🥇" if q["retrieval_rank"] == 1 else ("🥈" if q["retrieval_rank"] in (2, 3) else "❌")
        md.append(f"| `{q['dataset_instance']}` | `{q['target_trace_id']}` | {status_icon} `{q['retrieval_rank']}` | `{q['top_retrieved']}` | {q['query_snippet']} |")
    md.append("")
    return "\n".join(md)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate memory retrieval with raw issue queries")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--output", default=None, help="Save report to file")
    args = parser.parse_args(argv)

    data = run_retrieval_benchmark()

    # Save run artifact
    runs_dir = WORKSPACE_ROOT / "benchmarks" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    report_file = runs_dir / "retrieval_report.json"
    with report_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        md = format_markdown_report(data)
        print(md)
        if args.output:
            Path(args.output).write_text(md, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
