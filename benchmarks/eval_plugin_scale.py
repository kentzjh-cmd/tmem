"""
Expanded 50-Trajectory Plugin Effectiveness & Scale Validation Benchmark.

Validates the full `tmem` plugin runtime (BackwardTraceSlicer -> ProjectMemoryStore
SQLite/FTS5 + MEMORIES.md -> `handle_dsh_hook` runtime interception & telemetry)
across the expanded 50-trajectory dataset (`benchmarks/gold/gold_annotations_50.json`).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tmem.adapters.dsh import DshAdapter
from tmem.adapters.swe_bench import SweBenchAdapter
from tmem.hooks.dsh import handle_dsh_hook
from tmem.slicer import BackwardTraceSlicer
from tmem.store import ProjectMemoryStore
from tmem.telemetry import compute_telemetry_summary

NEGATIVE_PROMPTS = [
    "How to prepare authentic Italian carbonara with guanciale and pecorino",
    "Deploy high-availability Kubernetes cluster on AWS using Terraform and EKS",
    "Resolve lifetime elision and borrow checker conflict in Rust async trait",
    "Configure Nginx reverse proxy with Let's Encrypt automated TLS certificates",
    "Train a transformer language model with DeepSpeed pipeline parallelism",
    "Docker compose configuration for PostgreSQL read-write replica pool",
    "Optimize React virtualized table scrolling with IntersectionObserver",
    "Fix Gradle Kotlin DSL Android build flavor manifest merger error",
    "请帮我看看这段代码哪里可以优化一下",
    "hi, are you ready?",
]


def run_plugin_scale_validation(
    gold_path: Path | None = None,
) -> Dict[str, Any]:
    target_gold = gold_path or (WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations_50.json")
    entries = json.loads(target_gold.read_text(encoding="utf-8"))

    swe = SweBenchAdapter()
    dsh = DshAdapter()
    slicer = BackwardTraceSlicer()

    tmp_dir = Path(tempfile.mkdtemp(prefix="tmem_plugin_scale_"))
    try:
        ws = tmp_dir / "repo"
        ws.mkdir()
        store = ProjectMemoryStore(str(ws))

        compiled = []
        raw_steps_total = 0
        l0_steps_total = 0
        raw_chars_total = 0
        l0_chars_total = 0
        slice_times_ms: List[float] = []
        l2_pairs_total = 0
        non_trivial_count = 0

        # Create stub workspace files for all touched files so guards and file-anchor checks run realistically
        for entry in entries:
            adapter = dsh if entry["agent_format"] == "dsh" else swe
            session = adapter.parse_transcript(
                str(WORKSPACE_ROOT / entry["trajectory_file"]), str(ws)
            )
            t0 = time.perf_counter()
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            slice_times_ms.append(dt_ms)

            raw_steps_total += len(session.actions)

            if not pb:
                continue

            for tf in pb.touched_files:
                fpath = ws / tf
                fpath.parent.mkdir(parents=True, exist_ok=True)
                if not fpath.exists():
                    fpath.write_text("# stub file for guard validation\n", encoding="utf-8")

            raw_trace = slicer.build_raw_trace(session, pb)
            raw_path = store.raw_store.save_raw_trace(raw_trace)
            store.save_playbook(pb, target_scope="project", merge_similar=False)

            l0_steps_total += len(pb.l0_critical_path)
            raw_chars_total += raw_path.stat().st_size
            l0_chars_total += len(json.dumps(pb.l0_critical_path, ensure_ascii=False))
            l2_pairs_total += len(pb.l2_causal_repairs)
            if not pb.is_trivial:
                non_trivial_count += 1

            compiled.append({
                "instance_id": entry["instance_id"],
                "session": session,
                "playbook": pb,
            })

        t_sync0 = time.perf_counter()
        store.sync_memories_markdown()
        mem_sync_ms = (time.perf_counter() - t_sync0) * 1000.0

        # ------------------------------------------------------------------
        # 2. 50-Trace Scale Stress Retrieval (raw issue query -> SQLite FTS5)
        # ------------------------------------------------------------------
        hits_at_1 = 0
        hits_at_3 = 0
        rrs: List[float] = []
        valid_queries = 0
        for item in compiled:
            session = item["session"]
            pb = item["playbook"]
            query_str = (session.user_intents[0] if session.user_intents else "").strip()[:220]
            if not query_str:
                continue
            valid_queries += 1
            res = store.cascading_lookup(query_text=query_str, limit=5)
            ranked = [r.playbook.trace_id for r in res]
            rank = ranked.index(pb.trace_id) + 1 if pb.trace_id in ranked else 999
            if rank == 1:
                hits_at_1 += 1
            if rank <= 3:
                hits_at_3 += 1
            rrs.append(1.0 / rank if rank <= 10 else 0.0)

        neg_fp = 0
        for nq in NEGATIVE_PROMPTS:
            res = store.cascading_lookup(query_text=nq, limit=3)
            if any(r.l0_valid or r.playbook.confidence > 0.6 for r in res):
                neg_fp += 1

        # ------------------------------------------------------------------
        # 3. `handle_dsh_hook` Runtime Interception & Telemetry Validation
        # ------------------------------------------------------------------
        hook_latencies_ms: List[float] = []

        # 3a. Negative Holdout Noise Silence Test on `pre_step`
        pre_step_neg_injected = 0
        for idx, nq in enumerate(NEGATIVE_PROMPTS):
            sid = f"neg_sess_{idx}"
            t0 = time.perf_counter()
            out = handle_dsh_hook(
                "pre_step",
                {
                    "workspaceRoot": str(ws),
                    "sessionId": sid,
                    "isNewTurn": True,
                    "userMessage": nq,
                },
            )
            hook_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            ctx = out.get("injectedContext") or ""
            if "<tmem_memory" in ctx or "<tmem_causal_repair" in ctx:
                pre_step_neg_injected += 1

        # 3b. Pre-step Explicit Anchor Recall across compiled playbooks (first 20 with clear file targets)
        anchor_tested = 0
        anchor_hits = 0
        for idx, item in enumerate(compiled):
            pb = item["playbook"]
            code_targets = [f for f in pb.touched_files if "/" in f and not f.startswith(".")]
            if not code_targets:
                continue
            anchor_tested += 1
            sid = f"anchor_sess_{idx}"
            prompt = f"Please inspect {code_targets[0]} and resolve: {pb.problem_intent[:120]}"
            t0 = time.perf_counter()
            out = handle_dsh_hook(
                "pre_step",
                {
                    "workspaceRoot": str(ws),
                    "sessionId": sid,
                    "isNewTurn": True,
                    "userMessage": prompt,
                },
            )
            hook_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            if pb.trace_id in (out.get("injectedContext") or "") or code_targets[0] in (
                out.get("injectedContext") or ""
            ):
                anchor_hits += 1

        # 3c. Post-tool L2 Causal Error Interception + Verification Pass Resolution
        l2_tested = 0
        l2_intercepted = 0
        l2_cases = [
            (item["playbook"], pair)
            for item in compiled
            for pair in item["playbook"].l2_causal_repairs
        ]
        for idx, (pb, pair) in enumerate(l2_cases):
            l2_tested += 1
            sid = f"l2_replay_{idx}"
            t0 = time.perf_counter()
            fail_out = handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": str(ws),
                    "sessionId": sid,
                    "toolName": "run_command",
                    "args": {"CommandLine": pair.failed_command},
                    "exitCode": 1,
                    "isError": True,
                    "resultText": f"FAILED: {pair.error_signature}\n[exit code: 1]",
                },
            )
            hook_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            ctx = fail_out.get("additionalContext") or fail_out.get("injectedContext") or ""
            if "<tmem_speculative_memory>" in ctx and "- L2 Causal Repair:" in ctx:
                l2_intercepted += 1
                # Replay passing verification to validate telemetry attribution
                t1 = time.perf_counter()
                handle_dsh_hook(
                    "post_tool_use",
                    {
                        "workspaceRoot": str(ws),
                        "sessionId": sid,
                        "toolName": "run_command",
                        "args": {"CommandLine": pair.verification_cmd},
                        "exitCode": 0,
                        "isError": False,
                        "resultText": "OK\n[exit code: 0]",
                    },
                )
                hook_latencies_ms.append((time.perf_counter() - t1) * 1000.0)

        # 3d. Post-tool L1 Co-Change Topology Interception on Multi-file Playbooks
        l1_tested = 0
        l1_intercepted = 0
        for idx, item in enumerate(compiled):
            pb = item["playbook"]
            if not pb.l1_cochange_topology or not pb.touched_files:
                continue
            l1_tested += 1
            sid = f"l1_replay_{idx}"
            target = pb.touched_files[0]
            t0 = time.perf_counter()
            edit_out = handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": str(ws),
                    "sessionId": sid,
                    "toolName": "edit_file",
                    "args": {"TargetFile": target},
                    "exitCode": 0,
                    "isError": False,
                    "resultText": f"Edited {target}",
                },
            )
            hook_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            if "<tmem_cochange_hint>" in (
                edit_out.get("additionalContext") or edit_out.get("injectedContext") or ""
            ):
                l1_intercepted += 1

        telemetry = compute_telemetry_summary(str(ws))

        return {
            "corpus": {
                "total_input_trajectories": len(entries),
                "compiled_playbooks": len(compiled),
                "non_trivial_playbooks": non_trivial_count,
                "l2_causal_pairs_extracted": l2_pairs_total,
                "raw_steps_total": raw_steps_total,
                "l0_steps_total": l0_steps_total,
                "step_compression_pct": round(
                    (1.0 - l0_steps_total / raw_steps_total) * 100, 1
                )
                if raw_steps_total
                else 0.0,
                "char_compression_pct": round(
                    (1.0 - l0_chars_total / raw_chars_total) * 100, 1
                )
                if raw_chars_total
                else 0.0,
                "avg_slice_latency_ms": round(
                    sum(slice_times_ms) / len(slice_times_ms), 2
                )
                if slice_times_ms
                else 0.0,
                "memories_md_sync_ms": round(mem_sync_ms, 2),
            },
            "retrieval_50_scale": {
                "queries_evaluated": valid_queries,
                "recall_at_1_pct": round(hits_at_1 / valid_queries * 100, 1)
                if valid_queries
                else 0.0,
                "recall_at_3_pct": round(hits_at_3 / valid_queries * 100, 1)
                if valid_queries
                else 0.0,
                "mrr": round(sum(rrs) / valid_queries, 3) if valid_queries else 0.0,
                "negative_queries": len(NEGATIVE_PROMPTS),
                "negative_fp_pct": round(neg_fp / len(NEGATIVE_PROMPTS) * 100, 1),
            },
            "plugin_hook_validation": {
                "pre_step_negative_fp_pct": round(
                    pre_step_neg_injected / len(NEGATIVE_PROMPTS) * 100, 1
                ),
                "pre_step_anchor_tested": anchor_tested,
                "pre_step_anchor_hits": anchor_hits,
                "pre_step_anchor_hit_rate_pct": round(
                    anchor_hits / anchor_tested * 100, 1
                )
                if anchor_tested
                else 0.0,
                "l2_cases_tested": l2_tested,
                "l2_intercepted": l2_intercepted,
                "l2_interception_rate_pct": round(
                    l2_intercepted / l2_tested * 100, 1
                )
                if l2_tested
                else 0.0,
                "l1_cases_tested": l1_tested,
                "l1_intercepted": l1_intercepted,
                "l1_interception_rate_pct": round(
                    l1_intercepted / l1_tested * 100, 1
                )
                if l1_tested
                else 0.0,
                "avg_hook_latency_ms": round(
                    sum(hook_latencies_ms) / len(hook_latencies_ms), 2
                )
                if hook_latencies_ms
                else 0.0,
                "telemetry": telemetry,
            },
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    report = run_plugin_scale_validation()
    runs_dir = WORKSPACE_ROOT / "benchmarks" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_file = runs_dir / "plugin_scale_50_report.json"
    out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
