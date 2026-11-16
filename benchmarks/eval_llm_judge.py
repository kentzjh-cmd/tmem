"""
Blinded 50-Trajectory Pairwise LLM Judge Reproduction & Verification Harness.

Provides two reproducible workflows with zero external dependencies:
1. Verification / Aggregation (default, offline):
   Validates `benchmarks/runs/llm_judge_50_report.json` against `gold_annotations_50.json`,
   recomputes pairwise win counts and mean 1-5 rubric scores from the 50 per-case records,
   and confirms consistency with the committed report.
2. Live Blinded A/B Judge Execution (`--run-judge`, requires `OPENAI_API_KEY`):
   Deterministically compiles live B0 vs B1 slices via `eval_slicing_fidelity`, randomly
   shuffles Option A / Option B per case (seeded), calls an OpenAI-compatible LLM judge
   across 5 shards, and aggregates a fresh report.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tmem.adapters.dsh import DshAdapter
from tmem.adapters.swe_bench import SweBenchAdapter
from tmem.models import ActionKind
from tmem.slicer import BackwardTraceSlicer

JUDGE_SYSTEM_PROMPT = (
    "You are a strict engineering trajectory evaluator. Given a user problem intent, "
    "the raw step count, and two anonymized sliced step lists (Option A and Option B), "
    "score each option from 1 to 5 on completeness, precision_denoising, temporal_causality, "
    "and overall, and pick a winner ('Option A' or 'Option B'). Respond with strict JSON."
)


def _format_step_brief(action: Any) -> Dict[str, Any]:
    return {
        "step": action.step_idx,
        "kind": action.kind.value if hasattr(action.kind, "value") else str(action.kind),
        "target_file": action.target_file,
        "command": action.command,
        "exit_code": action.exit_code,
    }


def build_blinded_judge_packets(
    gold_path: Optional[Path] = None,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """Build deterministic, blinded Option A / Option B comparison packets for all 50 traces."""
    target_gold = gold_path or (WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations_50.json")
    entries = json.loads(target_gold.read_text(encoding="utf-8"))

    rng = random.Random(seed)
    swe = SweBenchAdapter()
    dsh = DshAdapter()
    slicer = BackwardTraceSlicer()
    packets: List[Dict[str, Any]] = []

    for entry in entries:
        traj_path = WORKSPACE_ROOT / entry["trajectory_file"]
        adapter = dsh if entry["agent_format"] == "dsh" else swe
        session = adapter.parse_transcript(str(traj_path), str(WORKSPACE_ROOT))

        perm_files = set(entry.get("perm_target_files") or [])
        b0_indices: Set[int] = set()
        for a in session.actions:
            if a.kind == ActionKind.MUTATE and a.target_file in perm_files:
                b0_indices.add(a.step_idx)
        passing_tests = [
            a
            for a in session.actions
            if a.kind == ActionKind.ENV_EXEC
            and a.exit_code == 0
            and a.command
            and not any(a.command.startswith(x) for x in ("rm ", "goto", "scroll", "exit", "submit", "cd "))
        ]
        if passing_tests:
            b0_indices.add(passing_tests[-1].step_idx)

        pb = slicer.slice_session(session, use_git_ground_truth=False)
        b1_indices = set(slicer.last_sliced_step_indices) if pb else set()

        action_by_step = {a.step_idx: a for a in session.actions}
        b0_steps = [_format_step_brief(action_by_step[i]) for i in sorted(b0_indices) if i in action_by_step]
        b1_steps = [_format_step_brief(action_by_step[i]) for i in sorted(b1_indices) if i in action_by_step]

        b1_is_option_a = rng.choice([True, False])
        option_a, option_b = (b1_steps, b0_steps) if b1_is_option_a else (b0_steps, b1_steps)
        packets.append(
            {
                "instance_id": entry["instance_id"],
                "raw_steps": len(session.actions),
                "intent": (session.user_intents[0] if session.user_intents else "")[:300],
                "option_a_label": "B1_tmem" if b1_is_option_a else "B0_baseline",
                "option_b_label": "B0_baseline" if b1_is_option_a else "B1_tmem",
                "option_a": option_a,
                "option_b": option_b,
                "b0_step_indices": sorted(b0_indices),
                "b1_step_indices": sorted(b1_indices),
            }
        )
    return packets


def aggregate_judge_cases(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Recompute pairwise win counts and mean 1-5 scores from per-case judge records."""
    n = len(cases)
    if n == 0:
        raise ValueError("No judge cases to aggregate")

    b1_wins = sum(1 for c in cases if c.get("winner") == "B1_tmem")
    b0_wins = sum(1 for c in cases if c.get("winner") == "B0_baseline")
    ties = n - b1_wins - b0_wins

    def _mean_group(key: str) -> Dict[str, float]:
        dims = ("completeness", "precision_denoising", "temporal_causality", "overall")
        return {
            d: round(sum(float(c[key]["llm_score"][d]) for c in cases) / n, 2)
            for d in dims
        }

    return {
        "total_trajectories": n,
        "pairwise_llm_judge": {
            "B1_tmem_wins": b1_wins,
            "B0_baseline_wins": b0_wins,
            "ties": ties,
            "B1_win_rate_pct": round((b1_wins / n) * 100.0, 1),
        },
        "mean_llm_scores": {
            "B0_baseline": _mean_group("b0"),
            "B1_tmem": _mean_group("b1"),
        },
    }


def verify_llm_judge_report(
    report_path: Optional[Path] = None,
    gold_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify saved `llm_judge_50_report.json` against `gold_annotations_50.json` and recompute totals."""
    target_report = report_path or (WORKSPACE_ROOT / "benchmarks" / "runs" / "llm_judge_50_report.json")
    target_gold = gold_path or (WORKSPACE_ROOT / "benchmarks" / "gold" / "gold_annotations_50.json")

    report = json.loads(target_report.read_text(encoding="utf-8"))
    gold_entries = json.loads(target_gold.read_text(encoding="utf-8"))
    gold_ids = [e["instance_id"] for e in gold_entries]

    cases = report.get("cases") or []
    case_ids = [c.get("instance_id") for c in cases]
    if len(cases) != len(gold_entries) or set(case_ids) != set(gold_ids):
        raise ValueError(
            f"Case mismatch between {target_report.name} ({len(cases)}) and {target_gold.name} ({len(gold_entries)})"
        )

    recomputed = aggregate_judge_cases(cases)
    if report.get("total_trajectories") != recomputed["total_trajectories"]:
        raise ValueError("total_trajectories mismatch in llm_judge_50_report.json")
    if report.get("pairwise_llm_judge") != recomputed["pairwise_llm_judge"]:
        raise ValueError(
            f"pairwise_llm_judge mismatch: saved={report.get('pairwise_llm_judge')} != recomputed={recomputed['pairwise_llm_judge']}"
        )
    if report.get("mean_llm_scores") != recomputed["mean_llm_scores"]:
        raise ValueError(
            f"mean_llm_scores mismatch: saved={report.get('mean_llm_scores')} != recomputed={recomputed['mean_llm_scores']}"
        )

    return recomputed


def _call_openai_judge(packet: Dict[str, Any], api_key: str, base_url: str, model: str) -> Dict[str, Any]:
    user_payload = {
        "instance_id": packet["instance_id"],
        "raw_steps": packet["raw_steps"],
        "intent": packet["intent"],
        "Option A": packet["option_a"],
        "Option B": packet["option_b"],
        "required_schema": {
            "option_a_score": {"completeness": 1, "precision_denoising": 1, "temporal_causality": 1, "overall": 1},
            "option_b_score": {"completeness": 1, "precision_denoising": 1, "temporal_causality": 1, "overall": 1},
            "winner": "Option A | Option B",
            "judge_reason": "concise rationale",
        },
    }
    req_body = json.dumps(
        {
            "model": model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def run_live_judge(
    output_path: Path,
    model: str,
    base_url: str,
    api_key: str,
    seed: int = 42,
) -> Dict[str, Any]:
    packets = build_blinded_judge_packets(seed=seed)
    cases: List[Dict[str, Any]] = []
    for pkt in packets:
        verdict = _call_openai_judge(pkt, api_key=api_key, base_url=base_url, model=model)
        a_is_b1 = pkt["option_a_label"] == "B1_tmem"
        b1_score = verdict["option_a_score"] if a_is_b1 else verdict["option_b_score"]
        b0_score = verdict["option_b_score"] if a_is_b1 else verdict["option_a_score"]
        raw_winner = str(verdict.get("winner", "")).strip()
        if raw_winner == "Option A":
            winner = pkt["option_a_label"]
        elif raw_winner == "Option B":
            winner = pkt["option_b_label"]
        else:
            winner = "B1_tmem" if b1_score["overall"] >= b0_score["overall"] else "B0_baseline"
        cases.append(
            {
                "instance_id": pkt["instance_id"],
                "raw_steps": pkt["raw_steps"],
                "b0": {"steps": pkt["b0_step_indices"], "llm_score": b0_score},
                "b1": {"steps": pkt["b1_step_indices"], "llm_score": b1_score},
                "winner": winner,
                "judge_reason": verdict.get("judge_reason", ""),
            }
        )

    summary = aggregate_judge_cases(cases)
    report = {**summary, "cases": cases}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or run 50-trajectory blinded LLM judge")
    parser.add_argument(
        "--generate-packets",
        action="store_true",
        help="Build blinded A/B packets from current slicing output and print summary",
    )
    parser.add_argument(
        "--run-judge",
        action="store_true",
        help="Execute live OpenAI-compatible blinded LLM judge (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--output",
        default=str(WORKSPACE_ROOT / "benchmarks" / "runs" / "llm_judge_50_report.json"),
        help="Output report path when --run-judge is set",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for A/B blinding")
    args = parser.parse_args(argv)

    if args.generate_packets:
        packets = build_blinded_judge_packets(seed=args.seed)
        print(json.dumps({"total_packets": len(packets), "sample_instance": packets[0]["instance_id"]}, indent=2))
        return 0

    if args.run_judge:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            print("Error: OPENAI_API_KEY environment variable is required for --run-judge", file=sys.stderr)
            return 1
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("TMEM_JUDGE_MODEL", "gpt-4o-mini")
        summary = run_live_judge(Path(args.output), model=model, base_url=base_url, api_key=api_key, seed=args.seed)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    try:
        verified = verify_llm_judge_report()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"LLM judge report verification FAILED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(verified, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
