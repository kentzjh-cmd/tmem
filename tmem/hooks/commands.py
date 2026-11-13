"""Shared slash-command and argument parsers for tmem DSH hooks."""

from __future__ import annotations

import re
from typing import Optional, Tuple

SLASH_TMEM_RE = re.compile(
    r"^/(?:tmem|remember|trace)(?:\s+(project|status|list|forget|gc|bench|evaluate|inspect|replay|diff|sync|telemetry))?(?:\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def parse_slash_tmem_command(user_input: str) -> Optional[Tuple[str, str]]:
    """Parse `/tmem <subcommand> [args]` or `/remember [intent]`."""
    m = SLASH_TMEM_RE.match((user_input or "").strip())
    if not m:
        return None
    action_mode = (m.group(1) or "project").lower()
    custom_intent = (m.group(2) or "").strip()
    return action_mode, custom_intent


def _parse_inspect_args(raw: str) -> Tuple[str, str]:
    """Parse `<trace_id> [--level index|playbook|raw|diff]` arguments."""
    parts = (raw or "").strip().split()
    tid = ""
    level = "playbook"
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("--level", "-l") and i + 1 < len(parts):
            level = parts[i + 1].lower()
            i += 2
        elif p in ("index", "playbook", "raw", "diff"):
            level = p
            i += 1
        elif not tid and not p.startswith("-"):
            tid = p
            i += 1
        else:
            i += 1
    return tid, level
