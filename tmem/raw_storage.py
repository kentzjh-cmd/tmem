"""
Raw Execution Trajectory Storage & Forensic Replay Engine.

Preserves full-fidelity agent tool invocations, command outputs, exit codes,
and unified diffs in `.tmem/raw/<trace_id>.raw.json`. Enables progressive disclosure:
- Level 0: Catalog Index (~30 tokens)
- Level 1: Guarded Speculative Playbook (~150-300 tokens)
- Level 2: Full Forensic Raw Trace Replay & Diffs (on-demand)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tmem.adapters.base import scrub_secrets
from tmem.models import ActionKind, RawExecutionTrace, TracePlaybook


class RawTraceStore:
    """Manages raw execution trace persistence and forensic inspection."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = str(Path(workspace_root).resolve())
        self.raw_dir = Path(self.workspace_root) / ".tmem" / "raw"

    def ensure_initialized(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def save_raw_trace(self, raw_trace: RawExecutionTrace) -> Path:
        """Atomically persist a RawExecutionTrace to .tmem/raw/<trace_id>.raw.json."""
        self.ensure_initialized()
        target_path = self.raw_dir / f"{raw_trace.trace_id}.raw.json"
        temp_path = self.raw_dir / f".{raw_trace.trace_id}.raw.tmp"

        payload = raw_trace.to_dict()
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(target_path)
        return target_path

    def load_raw_trace(self, trace_id: str) -> Optional[RawExecutionTrace]:
        """Load RawExecutionTrace by ID or ID prefix."""
        tid = trace_id.strip()
        if not tid or not self.raw_dir.exists():
            return None

        exact = self.raw_dir / f"{tid}.raw.json"
        if exact.exists():
            try:
                return RawExecutionTrace.from_dict(
                    json.loads(exact.read_text(encoding="utf-8"))
                )
            except Exception:
                return None

        # Prefix search
        matches = list(self.raw_dir.glob(f"{tid}*.raw.json"))
        if matches:
            try:
                return RawExecutionTrace.from_dict(
                    json.loads(matches[0].read_text(encoding="utf-8"))
                )
            except Exception:
                return None
        return None

    def delete_raw_trace(self, trace_id: str) -> bool:
        """Delete raw trace file by ID or prefix."""
        tid = trace_id.strip()
        if not tid or not self.raw_dir.exists():
            return False

        deleted = False
        for p in list(self.raw_dir.glob(f"{tid}*.raw.json")):
            try:
                p.unlink()
                deleted = True
            except Exception:
                pass
        return deleted

    def list_raw_traces(self) -> List[Dict[str, Any]]:
        """List summary info for all stored raw traces."""
        if not self.raw_dir.exists():
            return []
        items = []
        for p in sorted(self.raw_dir.glob("*.raw.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                items.append(
                    {
                        "trace_id": data.get("trace_id", p.stem.replace(".raw", "")),
                        "agent_source": data.get("agent_source", ""),
                        "created_at": data.get("created_at", ""),
                        "total_steps": data.get("total_steps", 0),
                        "sliced_steps": len(data.get("sliced_step_indices", [])),
                        "has_diff": bool(data.get("diff_patch")),
                        "file_size": p.stat().st_size,
                    }
                )
            except Exception:
                continue
        return items


def format_raw_trace_replay(
    raw_trace: RawExecutionTrace, show_all: bool = True
) -> str:
    """Format chronological step-by-step playback distinguishing critical path vs pruned noise."""
    lines = [
        f"════════════════════════════════════════════════════════════════════════════",
        f"🎬 Forensic Trace Replay: [{raw_trace.trace_id}]",
        f"   Agent: {raw_trace.agent_source} | Commit: {raw_trace.git_commit or 'HEAD'} | Time: {raw_trace.created_at}",
        f"   Trajectory Stats: {raw_trace.total_steps} total steps -> {len(raw_trace.sliced_step_indices)} critical path steps",
    ]
    if raw_trace.user_intents:
        lines.append(f"   Task Intent: {raw_trace.user_intents[0]}")
    lines.append(
        f"────────────────────────────────────────────────────────────────────────────"
    )

    sliced_indices = set(raw_trace.sliced_step_indices)

    for i, act in enumerate(raw_trace.actions):
        step_idx = act.get("step_idx", i + 1)
        is_critical = step_idx in sliced_indices or i in sliced_indices
        tool_name = act.get("tool_name", "unknown")
        kind_str = act.get("kind", ActionKind.OTHER.value)
        target = act.get("target_file") or ""
        cmd = act.get("command") or ""
        exit_code = act.get("exit_code")
        err_sig = act.get("error_signature")
        syms = act.get("symbols_touched") or []
        output_snip = act.get("output_snippet") or ""

        if is_critical:
            badge = f"🟢 [CRITICAL PATH: {kind_str}]"
        else:
            badge = f"⚪ [NOISE PRUNED: {kind_str}]"

        lines.append(f"\nStep #{step_idx} {badge} tool: {tool_name}")

        if target:
            sym_clause = f" (symbols: {', '.join(syms[:4])})" if syms else ""
            lines.append(f"   Target: {target}{sym_clause}")

        if cmd:
            code_clause = f" [exit: {exit_code}]" if exit_code is not None else ""
            lines.append(f"   Command: {cmd}{code_clause}")

        if err_sig:
            lines.append(f"   ⚠️  Error Encountered: {err_sig}")

        if output_snip and (err_sig or is_critical or not show_all):
            clean_snip = scrub_secrets(output_snip.strip().replace("\n", " "))[:140]
            lines.append(f"   Output: {clean_snip}...")

    lines.append(
        f"\n════════════════════════════════════════════════════════════════════════════"
    )
    return "\n".join(lines)


def format_raw_trace_diff(raw_trace: RawExecutionTrace) -> str:
    """Return the unified git diff / patch recorded during the session."""
    if not raw_trace.diff_patch:
        return f"# Trace {raw_trace.trace_id}: No unified diff patch recorded."
    return raw_trace.diff_patch


def format_progressive_view(
    playbook: TracePlaybook,
    raw_trace: Optional[RawExecutionTrace] = None,
    level: str = "playbook",
) -> str:
    """
    Format a trace memory at the requested progressive disclosure level:
    - "index": ~30-50 tokens high-level catalog entry
    - "playbook": ~150-300 tokens guarded execution playbook
    - "raw": full forensic chronological action replay
    - "diff": unified git diff patch
    """
    lvl = level.lower().strip()

    if lvl == "index":
        files_str = ", ".join(playbook.touched_files[:3]) or "None"
        verify_cmd = playbook.l1_verification_cmds[-1] if playbook.l1_verification_cmds else "None"
        raw_ref = f"raw/{playbook.trace_id}.raw.json" if playbook.raw_trace_path else "None"
        return (
            f"[{playbook.trace_id}] Scope: {playbook.scope} | Status: {playbook.status} | "
            f"Intent: {playbook.problem_intent} | Files: {files_str} | "
            f"Verify: `{verify_cmd}` | Confidence: {playbook.confidence:.2f} | Raw: {raw_ref}"
        )

    if lvl == "diff":
        if raw_trace:
            return format_raw_trace_diff(raw_trace)
        return f"# Trace {playbook.trace_id}: Raw trace not loaded to display diff."

    if lvl == "raw":
        if raw_trace:
            return format_raw_trace_replay(raw_trace, show_all=True)
        return (
            f"Raw trace for {playbook.trace_id} not found in .tmem/raw/. "
            f"Playbook critical path: {len(playbook.l0_critical_path)} steps."
        )

    # Default: "playbook"
    doc_lines = [
        f"### 📋 Playbook: `{playbook.trace_id}` ({playbook.scope.upper()})",
        f"- **Intent**: {playbook.problem_intent}",
    ]
    if playbook.semantic_summary:
        doc_lines.append(f"- **Summary**: {playbook.semantic_summary}")
    if playbook.root_cause:
        doc_lines.append(f"- **Root Cause**: {playbook.root_cause}")
    if playbook.requirements:
        doc_lines.append("- **Requirements & Intent Nodes**:")
        for r in playbook.requirements:
            doc_lines.append(f"  - `[{r.req_id}]` ({r.source}): {r.text}")
    doc_lines.append(f"- **Created At**: {playbook.created_at} (Commit: {playbook.git_commit or 'HEAD'})")
    doc_lines.append(f"- **Files Touched**: {', '.join(playbook.touched_files) or 'None'}")

    if playbook.guards:
        doc_lines.append("- **Speculative Pre-flight Guards**:")
        for g in playbook.guards:
            doc_lines.append(f"  - `{g.probe_type}` on `{g.target}` (expected: `{g.expected}`)")

    if playbook.l0_critical_path:
        doc_lines.append("- **L0 Critical Path**:")
        for s in playbook.l0_critical_path:
            act = s.get("action")
            tgt = s.get("target") or s.get("command") or ""
            syms = s.get("symbols")
            sym_clause = f" (symbols: {', '.join(syms)})" if syms else ""
            diff_clause = f" [diff: {s['diff_summary']}]" if s.get("diff_summary") else ""
            rat_clause = f" — {s['rationale']}" if s.get("rationale") else ""
            doc_lines.append(f"  - `{act}`: `{tgt}`{sym_clause}{diff_clause}{rat_clause}")

    if playbook.intent_edges:
        doc_lines.append("- **Execution-Intent Graph Relations**:")
        for edge in playbook.intent_edges[:8]:
            doc_lines.append(f"  - `{edge.src}` --[{edge.relation}]--> `{edge.dst}`: {edge.why}")

    if playbook.l1_cochange_topology:
        doc_lines.append("- **L1 Co-Change Topology**:")
        for topo in playbook.l1_cochange_topology:
            doc_lines.append(f"  - {topo}")

    if playbook.l1_verification_cmds:
        doc_lines.append(f"- **Verification Command**: `{playbook.l1_verification_cmds[-1]}`")

    if playbook.l2_causal_repairs:
        doc_lines.append("- **L2 Universal Causal Repairs**:")
        for r in playbook.l2_causal_repairs:
            doc_lines.append(
                f"  - Error `{r.error_signature}` -> Repair `{r.repair_action}` -> Verify `{r.verification_cmd}`"
            )

    if playbook.raw_trace_path:
        doc_lines.append(
            f"\n> 🔍 *Raw trace available: `{playbook.raw_trace_path}`. Run `python3 -m tmem.cli inspect {playbook.trace_id} --level raw` for full replay.*"
        )

    return "\n".join(doc_lines)
