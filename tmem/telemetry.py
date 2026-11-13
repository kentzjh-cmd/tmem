"""Runtime benefit telemetry (.tmem/telemetry.jsonl) for measuring real DSH effectiveness."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from tmem import logger


TELEMETRY_FILENAME = "telemetry.jsonl"


def _telemetry_path(workspace_root: str) -> Path:
    return Path(workspace_root).resolve() / ".tmem" / TELEMETRY_FILENAME


def record_telemetry_event(
    workspace_root: str, event_type: str, data: Dict[str, Any]
) -> None:
    """Append a structured runtime telemetry event to `.tmem/telemetry.jsonl`."""
    path = _telemetry_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.debug("Failed to write telemetry event: %s", e)


def load_telemetry_events(workspace_root: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """Load recent telemetry events from `.tmem/telemetry.jsonl`."""
    path = _telemetry_path(workspace_root)
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    events.append(json.loads(s))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit > 0 and len(events) > limit:
        return events[-limit:]
    return events


def compute_telemetry_summary(workspace_root: str) -> Dict[str, Any]:
    """Compute runtime adoption and resolution statistics from `.tmem/telemetry.jsonl`."""
    events = load_telemetry_events(workspace_root)
    l2_injections = 0
    l2_resolved_green = 0
    l2_unresolved = 0
    l1_hints = 0
    l1_adopted = 0
    l1_ignored = 0

    for ev in events:
        etype = ev.get("event")
        if etype == "l2_injected":
            l2_injections += 1
        elif etype == "l2_outcome":
            if ev.get("resolved"):
                l2_resolved_green += 1
            else:
                l2_unresolved += 1
        elif etype == "l1_cochange_hinted":
            l1_hints += 1
        elif etype == "l1_outcome":
            if ev.get("adopted"):
                l1_adopted += 1
            else:
                l1_ignored += 1

    l2_total_outcomes = l2_resolved_green + l2_unresolved
    l1_total_outcomes = l1_adopted + l1_ignored
    return {
        "total_events": len(events),
        "l2_injections": l2_injections,
        "l2_resolved_green": l2_resolved_green,
        "l2_unresolved": l2_unresolved,
        "l2_resolution_rate": round(l2_resolved_green / l2_total_outcomes, 3)
        if l2_total_outcomes > 0
        else None,
        "l1_hints": l1_hints,
        "l1_adopted": l1_adopted,
        "l1_ignored": l1_ignored,
        "l1_adoption_rate": round(l1_adopted / l1_total_outcomes, 3)
        if l1_total_outcomes > 0
        else None,
    }
