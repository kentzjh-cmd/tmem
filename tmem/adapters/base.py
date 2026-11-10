"""Base protocol and utilities for multi-agent transcript & hook adapters."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tmem.models import ActionNode

ERROR_PATTERNS = [
    re.compile(r"([A-Za-z0-9_.]*(?:Error|Exception|Fault|Panic|FAIL)[A-Za-z0-9_.:]*[^\n]{0,120})"),
    re.compile(r"(error\[E\d+\]:[^\n]{0,120})"),
    re.compile(r"(Cannot find module [^\n]{0,100})"),
    re.compile(r"(ModuleNotFoundError: [^\n]{0,100})"),
    re.compile(r"(ImportError: [^\n]{0,100})"),
    re.compile(r"(linker `[^`]+` not found)"),
]

SECRET_REDACT_RE = re.compile(
    r"\b(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{35})\b"
)


def scrub_secrets(text: str) -> str:
    """Redact API keys, PATs, and tokens before any trace memory is persisted."""
    if not text:
        return ""
    return SECRET_REDACT_RE.sub("[REDACTED_SECRET]", text)


@dataclass
class ParsedSession:
    """Normalized representation of an agent session across agy, codex, opencode, pi, dsh."""

    agent_name: str
    session_id: str
    workspace_root: str
    user_intents: List[str] = field(default_factory=list)
    actions: List[ActionNode] = field(default_factory=list)
    active_errors: List[str] = field(default_factory=list)


def normalize_rel_path(raw_path: Optional[str], workspace_root: str) -> Optional[str]:
    """Strip quotes and normalize path relative to workspace_root if inside it."""
    if not raw_path:
        return None
    cleaned = str(raw_path).strip().strip('"').strip("'")
    if not cleaned:
        return None
    try:
        ws = Path(workspace_root).resolve()
        target = Path(cleaned)
        if target.is_absolute():
            resolved = target.resolve()
            if resolved == ws or ws in resolved.parents:
                return str(resolved.relative_to(ws))
        return cleaned
    except Exception:
        return cleaned


def extract_error_signature(text: str) -> Optional[str]:
    """Deterministically extract canonical error signature from tool stderr/stdout."""
    if not text:
        return None
    # Prioritize the last traceback/error line rather than matching inside quoted strings
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("AssertionError:", "E   "))
    ]
    search_space = "\n".join(reversed(lines[-30:])) if lines else text
    for pat in ERROR_PATTERNS:
        m = pat.search(search_space)
        if m:
            sig = m.group(1).strip()
            if len(sig) > 6 and "=>" not in sig:
                return sig[:160]
    return None


def extract_symbols_from_text(text: str) -> List[str]:
    """Extract defined/modified function or class names from code snippets via regex."""
    if not text:
        return []
    symbols = set()
    for m in re.finditer(
        r"\b(?:def|class|fn|func|function|interface|struct)\s+([A-Za-z_][A-Za-z0-9_]*)",
        text,
    ):
        symbols.add(m.group(1))
    return sorted(symbols)


class AgentAdapter:
    """Base interface for agent-specific transcript parsers and hook bridges."""

    name: str = "base"

    def parse_transcript(
        self, transcript_path: str, workspace_root: str, min_step_idx: int = 0
    ) -> ParsedSession:
        raise NotImplementedError

    def discover_latest_transcript(self, workspace_root: str) -> Optional[str]:
        raise NotImplementedError
