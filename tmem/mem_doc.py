"""
Living Memory Index Generator (MEMORIES.md / MEM.md).

Maintains a transparent, Git-versioned Markdown index at the workspace root,
enabling zero-token agent navigation (Cursor, Claude Code, Antigravity, DeepSeek Harness)
and human visibility into compiled execution playbooks and forensic raw traces.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from tmem.guards import evaluate_guards
from tmem.models import CausalRepairPair, TracePlaybook


def render_memories_markdown(
    playbooks: List[TracePlaybook],
    workspace_root: str,
) -> str:
    """Generate Markdown text for MEMORIES.md."""
    lines: List[str] = [
        "# 🧠 Project Execution Memories (`MEMORIES.md`)",
        "",
        "> **Maintained autonomously by [tmem](https://github.com/kentzjh-cmd/tmem)**.",
        "> This file is a git-versioned living index of verified execution playbooks, co-change topologies, and universal causal repairs.",
        "> AI agents (Antigravity, DeepSeek Harness, Cursor, Claude Code, Copilot, etc.) and human engineers can navigate this index to follow verified paths and bypass known errors.",
        "",
        "## ⚡ Progressive Memory Disclosure Tiers",
        "- **Tier 0 (Catalog Index)**: This `MEMORIES.md` file provides zero-token exploration and instant navigation.",
        "- **Tier 1 (Playbooks)**: Detailed pre-flight guarded playbooks with AST/filesystem assertions in [`.tmem/traces/`](.tmem/traces/).",
        "- **Tier 2 (Raw Execution Traces)**: Full-fidelity forensic logs, tool inputs/outputs, and diff patches in [`.tmem/raw/`](.tmem/raw/).",
        "",
        "---",
        "",
        "## 1. Active Verified Execution Playbooks (L0/L1)",
        "",
    ]

    if not playbooks:
        lines.extend(
            [
                "_No project execution traces recorded yet. Traces will be automatically compiled on `/tmem`, milestone completions, or `git commit`._",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "| Trace ID | Tier Status | Problem Intent / Summary | Key Files & Symbols | Verification Command | Confidence | Raw Trace |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )

        raw_dir = Path(workspace_root) / ".tmem" / "raw"

        for pb in playbooks:
            ev = evaluate_guards(pb, workspace_root)
            tier_badge = "🟢 `L0_FULL`"
            if getattr(pb, "is_trivial", False):
                tier_badge = "⚪ `TRIVIAL`"
            elif ev.effective_tier == "L1_TOPOLOGY":
                tier_badge = "🟡 `L1_TOPOLOGY`"
            elif ev.effective_tier == "L2_INVARIANT":
                tier_badge = "🔵 `L2_INVARIANT`"

            intent = pb.problem_intent.replace("|", "\\|").strip()
            if pb.semantic_summary:
                clean_sum = pb.semantic_summary.replace("|", "\\|").strip()
                intent = f"**{intent}**<br>_{clean_sum}_"
            if pb.root_cause:
                clean_rc = pb.root_cause.replace("|", "\\|").strip()
                intent += f"<br>🎯 <i>{clean_rc}</i>"

            files_str = "<br>".join(f"`{f}`" for f in pb.touched_files[:3]) or "`-`"
            if len(pb.touched_files) > 3:
                files_str += f"<br>_+{len(pb.touched_files) - 3} more_"

            verify_cmd = (
                f"`{pb.l1_verification_cmds[-1]}`"
                if pb.l1_verification_cmds
                else "`-`"
            )

            raw_file = raw_dir / f"{pb.trace_id}.raw.json"
            raw_link = (
                f"[`raw`](.tmem/raw/{pb.trace_id}.raw.json)"
                if raw_file.exists() or pb.raw_trace_path
                else "`-`"
            )

            conf_pct = f"{int(pb.confidence * 100)}%"

            lines.append(
                f"| [`{pb.trace_id}`](.tmem/traces/{pb.trace_id}.json) | {tier_badge} | {intent} | {files_str} | {verify_cmd} | {conf_pct} | {raw_link} |"
            )
        lines.append("")

    # Section 2: Universal Causal Repairs (L2)
    all_repairs: List[CausalRepairPair] = []
    seen_repairs = set()
    for pb in playbooks:
        for r in pb.l2_causal_repairs:
            sig_key = f"{r.error_signature}|{r.repair_action}"
            if sig_key not in seen_repairs:
                seen_repairs.add(sig_key)
                all_repairs.append(r)

    lines.extend(
        [
            "---",
            "",
            "## 2. Universal Causal Invariant Repairs (L2)",
            "",
        ]
    )

    if not all_repairs:
        lines.extend(
            [
                "_No error-to-fix invariant pairs extracted yet._",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "| Error Signature | Failed Command | Minimal Repair Action | Verification Command |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )
        for r in all_repairs:
            err = r.error_signature.replace("|", "\\|")
            failed = f"`{r.failed_command}`" if r.failed_command else "`-`"
            repair = r.repair_action.replace("|", "\\|")
            verify = f"`{r.verification_cmd}`" if r.verification_cmd else "`-`"
            lines.append(f"| `{err}` | {failed} | `{repair}` | {verify} |")
        lines.append("")

    # Section 3: Co-Change Architectural Topologies
    all_topologies: List[str] = []
    seen_topos = set()
    for pb in playbooks:
        for topo in pb.l1_cochange_topology:
            if topo not in seen_topos:
                seen_topos.add(topo)
                all_topologies.append(topo)

    lines.extend(
        [
            "---",
            "",
            "## 3. Co-Change Architectural Topologies",
            "",
        ]
    )

    if not all_topologies:
        lines.extend(
            [
                "_No multi-file co-change patterns detected yet._",
                "",
            ]
        )
    else:
        for topo in all_topologies:
            lines.append(f"- 🔗 {topo}")
        lines.append("")

    # Section 4: Forensic Replay & Inspection Commands
    lines.extend(
        [
            "---",
            "",
            "## 🛠️ Memory Commands & Forensic Inspection",
            "",
            "| Command | Description |",
            "| :--- | :--- |",
            "| `python3 -m tmem.cli inspect <trace_id> --level [index\\|playbook\\|raw\\|diff]` | Progressive disclosure: inspect at index, playbook, raw steps, or diff level |",
            "| `python3 -m tmem.cli replay <trace_id>` | Step-by-step forensic terminal playback of critical path vs pruned noise |",
            "| `python3 -m tmem.cli diff <trace_id>` | View unified git diff patch recorded during the session |",
            "| `python3 -m tmem.cli sync` | Re-index SQLite cache, validate L0 guards, and regenerate `MEMORIES.md` |",
            "| `python3 -m tmem.cli status` | Show project-level memory statistics |",
            "| `python3 -m tmem.cli gc` | Run metabolic garbage collection and prune stale paths |",
            "| `/tmem [intent]` | Capture current session execution path into project memory (`.tmem/traces/`) |",
            "",
        ]
    )

    return "\n".join(lines)


def sync_memories_file(
    workspace_root: str,
    playbooks: List[TracePlaybook],
) -> Path:
    """Atomically write MEMORIES.md and maintain MEM.md symlink in workspace root."""
    ws = Path(workspace_root).resolve()
    target_file = ws / "MEMORIES.md"
    temp_file = ws / ".MEMORIES.md.tmp"

    content = render_memories_markdown(playbooks, str(ws))
    temp_file.write_text(content, encoding="utf-8")
    temp_file.replace(target_file)

    # Maintain MEM.md symlink (or copy on platforms without symlink support)
    mem_alias = ws / "MEM.md"
    try:
        if mem_alias.is_symlink() or mem_alias.exists():
            mem_alias.unlink()
        mem_alias.symlink_to("MEMORIES.md")
    except Exception:
        try:
            mem_alias.write_text(content, encoding="utf-8")
        except Exception:
            pass

    return target_file
