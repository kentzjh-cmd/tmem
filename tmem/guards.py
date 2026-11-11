"""
Speculative Execution Guard Evaluator & 3-Tier Progressive Degradation Engine.

Validates L0 guards in <5ms with zero LLM tokens:
- Full Pass (L0 valid): Injects concrete L0 file paths + L1 co-change topology + L2 causal repairs.
- Partial/Failed Guard (Environment/Codebase drifted): Flushes stale L0 paths and degrades gracefully
  to L1 (co-change contract) + L2 (error->fix root cause invariant) so the agent is never misled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Tuple

from tmem.models import GuardProbe, TracePlaybook


@dataclass
class EvaluatedPlaybook:
    playbook: TracePlaybook
    l0_valid: bool
    effective_tier: str  # "L0_FULL" | "L1_TOPOLOGY" | "L2_INVARIANT"
    failed_guards: List[str]
    passed_files: List[str] = field(default_factory=list)


def evaluate_guards(playbook: TracePlaybook, workspace_root: str) -> EvaluatedPlaybook:
    """Run <5ms filesystem and symbol assertions for a TracePlaybook with partial degradation."""
    if playbook.scope == "global":
        return EvaluatedPlaybook(
            playbook=playbook,
            l0_valid=False,
            effective_tier="L2_INVARIANT",
            failed_guards=[],
            passed_files=[],
        )

    ws = Path(workspace_root)
    failed: List[str] = []
    checked_files: Set[str] = set()

    for g in playbook.guards:
        checked_files.add(g.target)
        target_path = ws / g.target
        if g.probe_type == "file_exists":
            if not target_path.exists():
                failed.append(f"missing_file:{g.target}")
        elif g.probe_type == "symbol_present":
            if not target_path.exists():
                failed.append(f"missing_file:{g.target}")
            else:
                try:
                    content = target_path.read_text(encoding="utf-8", errors="ignore")
                    if not re.search(rf"\b{re.escape(g.expected)}\b", content):
                        failed.append(f"missing_symbol:{g.target}#{g.expected}")
                except Exception:
                    failed.append(f"unreadable:{g.target}")
        elif g.probe_type == "file_contains":
            if not target_path.exists():
                failed.append(f"missing_file:{g.target}")
            else:
                try:
                    content = target_path.read_text(encoding="utf-8", errors="ignore")
                    if g.expected not in content:
                        failed.append(f"missing_substring:{g.target}")
                except Exception:
                    failed.append(f"unreadable:{g.target}")

    dedup_failed = list(dict.fromkeys(failed))
    failed_targets = {f.split(":", 1)[1].split("#")[0] for f in dedup_failed if ":" in f}
    passed_targets = sorted(checked_files - failed_targets)

    if not failed and playbook.guards:
        return EvaluatedPlaybook(
            playbook=playbook,
            l0_valid=True,
            effective_tier="L0_FULL",
            failed_guards=[],
            passed_files=passed_targets,
        )

    # If concrete files/symbols moved, degrade gracefully to L1/L2
    tier = "L2_INVARIANT" if playbook.l2_causal_repairs else "L1_TOPOLOGY"
    return EvaluatedPlaybook(
        playbook=playbook,
        l0_valid=False,
        effective_tier=tier,
        failed_guards=dedup_failed,
        passed_files=passed_targets,
    )


def format_injected_context(evaluated_list: List[EvaluatedPlaybook]) -> str:
    """
    Compile matched playbooks into an ultra-compact, deterministic system prompt block (<300 tokens).
    """
    if not evaluated_list:
        return ""

    lines = [
        "<tmem_speculative_memory>",
        "Deterministic execution traces matched from prior verified sessions:",
    ]

    for ev in evaluated_list[:3]:
        pb = ev.playbook
        lines.append(
            f"\n[Trace {pb.trace_id} | Scope: {pb.scope.upper()} | Tier: {ev.effective_tier} | Confidence: {pb.confidence:.2f}]"
        )
        lines.append(f"- Historical Intent: {pb.problem_intent}")
        if pb.semantic_summary:
            lines.append(f"- Semantic Summary: {pb.semantic_summary}")
        if pb.root_cause:
            lines.append(f"- Root Cause: {pb.root_cause}")

        if ev.l0_valid and pb.l0_critical_path:
            # L0 Full Concrete Path with rationales & compact diff
            steps_desc = []
            for s in pb.l0_critical_path[:6]:
                action = s.get("action")
                target = s.get("target") or s.get("command")
                diff_sum = s.get("diff_summary")
                rationale = s.get("rationale")
                extra = f" ({diff_sum})" if diff_sum else (f" — {rationale}" if rationale else "")
                steps_desc.append(f"{action}({target}){extra}")
            lines.append(f"- Verified L0 Critical Path (Guards Passed): {' -> '.join(steps_desc)}")
        else:
            if ev.failed_guards:
                lines.append(
                    f"- Note: L0 concrete paths drifted ({', '.join(ev.failed_guards)}); degraded to structural/causal invariants:"
                )
            if ev.passed_files and pb.l0_critical_path:
                surviving = [
                    s for s in pb.l0_critical_path
                    if not s.get("target") or s.get("target") in ev.passed_files
                ]
                if surviving:
                    steps_desc = [
                        f"{s['action']}({s.get('target') or s.get('command')})"
                        for s in surviving[:4]
                    ]
                    lines.append(
                        f"- Partial L0 Path (Drifted files hidden): {' -> '.join(steps_desc)}"
                    )

        if pb.l1_cochange_topology and pb.scope == "project":
            for topo in pb.l1_cochange_topology:
                lines.append(f"- L1 Topology: {topo}")
        if pb.l1_verification_cmds and ev.l0_valid:
            lines.append(f"- Verification Command: `{pb.l1_verification_cmds[-1]}`")

        for rep in pb.l2_causal_repairs[:2]:
            lines.append(
                f"- L2 Causal Repair: On `{rep.error_signature}` => `{rep.repair_action}` (verify: `{rep.verification_cmd}`)"
            )

        if pb.raw_trace_path:
            lines.append(
                f"- Forensic Audit: Raw trace archived at `{pb.raw_trace_path}` (inspect: `python3 -m tmem.cli inspect {pb.trace_id} --level raw`)"
            )

    lines.append("</tmem_speculative_memory>")
    return "\n".join(lines)
