"""
Living Memory Index Generator (MEMORIES.md / MEM.md).

Maintains a transparent, Git-versioned Markdown index at the workspace root,
enabling zero-token agent navigation (DeepSeek Harness)
and human visibility into compiled execution playbooks and forensic raw traces.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from tmem.cochange import compute_cochange_stats
from tmem.guards import evaluate_guards
from tmem.models import CausalRepairPair, TracePlaybook


_TRAIL_REDIRECT_RE = re.compile(r"(?:\s*(?:\|\s*(?:head|tail|grep|tee)\b.*|2>&1|>\s*\S+))+\s*$")


def normalize_command_template(cmd: str) -> str:
    """Normalize a verified shell command into a concise 1-line canonical form."""
    cleaned = re.sub(r"[\r\n\t]+", " ", (cmd or "")).strip()
    cleaned = _TRAIL_REDIRECT_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _env_facts_path(workspace_root: str) -> Path:
    return Path(workspace_root) / ".tmem" / "env_facts.json"


def _load_env_facts(workspace_root: str) -> Dict[str, List[str]]:
    path = _env_facts_path(workspace_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): [str(x) for x in v]
                for k, v in data.items()
                if isinstance(v, list)
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def record_verified_env_cmd(
    workspace_root: str, commands: List[str], source_id: str
) -> None:
    """Persist verified commands from skipped/trivial sessions so micro-skills still benefit."""
    if not workspace_root or not commands:
        return
    tmem_dir = Path(workspace_root) / ".tmem"
    if not tmem_dir.exists():
        tmem_dir.mkdir(parents=True, exist_ok=True)
    facts = _load_env_facts(workspace_root)
    changed = False
    for cmd in commands:
        norm = normalize_command_template(cmd)
        if not norm or len(norm) > 120:
            continue
        seen = facts.setdefault(norm, [])
        if source_id not in seen:
            seen.append(source_id)
            facts[norm] = seen[-20:]
            changed = True
    if changed:
        try:
            _env_facts_path(workspace_root).write_text(
                json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


def extract_micro_skills(
    playbooks: List[TracePlaybook],
    min_traces: int = 2,
    max_items: int = 6,
    workspace_root: Optional[str] = None,
) -> List[str]:
    """
    Zero-LLM extraction of cross-task 1-line operational facts (verified commands
    and env/toolchain repairs) validated across at least `min_traces` distinct traces/sessions.
    """
    cmd_traces: Dict[str, Set[str]] = {}
    if workspace_root:
        for cmd, src_ids in _load_env_facts(workspace_root).items():
            norm = normalize_command_template(cmd)
            if norm and len(norm) <= 120:
                cmd_traces.setdefault(norm, set()).update(src_ids)

    for pb in playbooks:
        if pb.status == "invalidated" or pb.confidence < 0.3:
            continue
        for cmd in pb.l1_verification_cmds:
            norm = normalize_command_template(cmd)
            if norm and len(norm) <= 120:
                cmd_traces.setdefault(norm, set()).add(pb.trace_id)
        for rep in pb.l2_causal_repairs:
            if rep.is_env_or_toolchain and rep.repair_action:
                norm = normalize_command_template(rep.repair_action)
                if norm and len(norm) <= 120:
                    cmd_traces.setdefault(norm, set()).add(pb.trace_id)

    validated = [
        (cmd, len(tids))
        for cmd, tids in cmd_traces.items()
        if len(tids) >= min_traces
    ]
    validated.sort(key=lambda x: (-x[1], len(x[0]), x[0]))
    return [cmd for cmd, _ in validated[:max_items]]


def format_micro_skills_block(skills: List[str]) -> str:
    """Format validated 1-line operational micro-skills for cold-start injection."""
    if not skills:
        return ""
    lines = [
        "<tmem_env_facts>",
        "Cross-task verified repository commands & toolchain facts:",
    ]
    for s in skills:
        lines.append(f"- `{s}`")
    lines.append("</tmem_env_facts>")
    return "\n".join(lines)


def sanitize_table_cell(
    text: Optional[str], max_chars: int = 120, as_code: bool = False
) -> str:
    """
    Sanitize text for a Markdown table cell:
    - Normalizes newlines and tabs to single spaces so table rows don't break.
    - Escapes unescaped '|' characters.
    - Truncates to max_chars (command/text max 120 chars).
    - If as_code is True and non-empty, wraps in single backticks.
    """
    if not text:
        return "`-`" if as_code else "-"
    # Normalize newlines and consecutive whitespace into single spaces
    cleaned = re.sub(r"[\r\n\t]+", " ", str(text)).strip()
    # Escape pipe characters that are not already escaped
    cleaned = re.sub(r"(?<!\\)\|", r"\|", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 3].rstrip() + "..."
    if not cleaned:
        return "`-`" if as_code else "-"
    if as_code:
        cleaned = cleaned.replace("`", "'")
        return f"`{cleaned}`"
    return cleaned


def render_memories_markdown(
    playbooks: List[TracePlaybook],
    workspace_root: str,
) -> str:
    """Generate Markdown text for MEMORIES.md."""
    lines: List[str] = [
        "# 🧠 Project Execution Memories (`MEMORIES.md`)",
        "",
        "> **Maintained autonomously by [tmem](https://github.com/kentzjh-cmd/tmem)**.",
        "> Zero-token deterministic requirement & behavior summaries compiled from sliced historical DSH sessions (with inline `session_id` lineage), co-change topologies, and causal error repairs.",
        "> Both DSH and human engineers can navigate this index to inspect verified execution paths without re-reading raw session logs or paying LLM summarization tokens.",
        "",
        "## ⚡ Progressive Memory Disclosure Tiers",
        "- **Tier 0 (Catalog Index)**: This `MEMORIES.md` file provides zero-token exploration and instant navigation.",
        "- **Tier 1 (Playbooks)**: Detailed pre-flight guarded playbooks with AST/filesystem assertions in [`.tmem/traces/`](.tmem/traces/).",
        "- **Tier 2 (Raw Execution Traces)**: Full-fidelity forensic logs, tool inputs/outputs, and diff patches in [`.tmem/raw/`](.tmem/raw/).",
        "",
        "---",
        "",
    ]

    micro_skills = extract_micro_skills(playbooks, workspace_root=workspace_root)
    active_playbooks = [
        pb
        for pb in playbooks
        if not getattr(pb, "is_trivial", False) and pb.status != "invalidated"
    ]
    if micro_skills:
        lines.extend(
            [
                "## 0. Verified Env & Test Commands (Cross-Task Micro-Skills)",
                "",
            ]
        )
        for skill in micro_skills:
            lines.append(f"- `{skill}`")
        lines.extend(["", "---", ""])

    lines.extend(
        [
            "## 1. Active Verified Execution Playbooks (L0/L1)",
            "",
        ]
    )

    if not active_playbooks:
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

        for pb in active_playbooks:
            ev = evaluate_guards(pb, workspace_root)
            tier_badge = "🟢 `L0_FULL`"
            if getattr(pb, "is_trivial", False):
                tier_badge = "⚪ `TRIVIAL`"
            elif ev.effective_tier == "L1_TOPOLOGY":
                tier_badge = "🟡 `L1_TOPOLOGY`"
            elif ev.effective_tier == "L2_INVARIANT":
                tier_badge = "🔵 `L2_INVARIANT`"

            intent_title = sanitize_table_cell(pb.problem_intent, max_chars=120, as_code=False)
            intent = f"**{intent_title}**"
            if pb.semantic_summary:
                clean_sum = sanitize_table_cell(pb.semantic_summary, max_chars=160, as_code=False)
                if clean_sum and clean_sum != "-":
                    intent += f"<br>_{clean_sum}_"
            if pb.root_cause:
                clean_rc = sanitize_table_cell(pb.root_cause, max_chars=120, as_code=False)
                if clean_rc and clean_rc != "-":
                    intent += f"<br>🎯 <i>{clean_rc}</i>"

            files_str = "<br>".join(f"`{f}`" for f in pb.touched_files[:3]) or "`-`"
            if len(pb.touched_files) > 3:
                files_str += f"<br>_+{len(pb.touched_files) - 3} more_"

            raw_cmd = pb.l1_verification_cmds[-1] if pb.l1_verification_cmds else ""
            verify_cmd = sanitize_table_cell(raw_cmd, max_chars=120, as_code=True)

            raw_file = raw_dir / f"{pb.trace_id}.raw.json"
            raw_link = (
                f"[`raw`](.tmem/raw/{pb.trace_id}.raw.json)"
                if raw_file.exists() or pb.raw_trace_path
                else "`-`"
            )

            conf_pct = f"{int(pb.confidence * 100)}%"
            sess_ids = getattr(pb, "source_session_ids", None) or []
            sess_cell = (
                "<br><sub>session: " + ", ".join(f"`{s}`" for s in sess_ids[-2:]) + "</sub>"
                if sess_ids
                else ""
            )

            lines.append(
                f"| [`{pb.trace_id}`](.tmem/traces/{pb.trace_id}.json){sess_cell} | {tier_badge} | {intent} | {files_str} | {verify_cmd} | {conf_pct} | {raw_link} |"
            )
        lines.append("")

    # Section 2: Universal Causal Repairs (L2)
    all_repairs: List[CausalRepairPair] = []
    seen_repairs = set()
    for pb in active_playbooks:
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
            err = sanitize_table_cell(r.error_signature, max_chars=120, as_code=True)
            failed = sanitize_table_cell(r.failed_command, max_chars=120, as_code=True)
            repair = sanitize_table_cell(r.repair_action, max_chars=120, as_code=True)
            verify = sanitize_table_cell(r.verification_cmd, max_chars=120, as_code=True)
            lines.append(f"| {err} | {failed} | {repair} | {verify} |")
        lines.append("")

    # Section 3: Co-Change Architectural Topologies (statistical history first)
    stat_pairs = compute_cochange_stats(workspace_root, active_playbooks)
    all_topologies: List[str] = [p.format_label() for p in stat_pairs]
    if not all_topologies:
        seen_topos = set()
        for pb in active_playbooks:
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
