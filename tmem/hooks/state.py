"""Runtime state persistence for tmem agent hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from tmem import logger


def default_runtime_state(conv_id: str = "default") -> Dict[str, Any]:
    return {
        "conversation_id": conv_id,
        "last_turn_idx": -1,
        "last_captured_step_idx": 0,
        "injected_trace_ids": [],
        "session_errors": [],
        "recent_errors": [],
        "recent_files": [],
        "uncommitted_mutations": [],
        "verified_since_mutation": False,
        "milestone_prompted_for_mutations": [],
    }


def load_runtime_state(workspace_root: str, conv_id: str = "default") -> Dict[str, Any]:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    if not state_file.exists():
        return default_runtime_state(conv_id)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if data.get("conversation_id") != conv_id and conv_id != "default":
            return default_runtime_state(conv_id)
        # Ensure all required keys exist
        default_state = default_runtime_state(conv_id)
        for k, v in default_state.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read runtime_state.json: %s", e)
        return default_runtime_state(conv_id)


def save_runtime_state(workspace_root: str, state: Dict[str, Any]) -> None:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        state_file.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.debug("Failed to write runtime_state.json: %s", e)
