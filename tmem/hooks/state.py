"""Runtime state persistence for tmem agent hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tmem import logger


LOOP_STREAK_THRESHOLD = 3


def default_runtime_state(conv_id: str = "default") -> Dict[str, Any]:
    return {
        "conversation_id": conv_id,
        "last_turn_idx": -1,
        "last_captured_step_idx": 0,
        "injected_trace_ids": [],
        "rewarded_trace_ids": [],
        "penalized_trace_ids": [],
        "micro_skills_injected": False,
        "session_errors": [],
        "recent_errors": [],
        "recent_files": [],
        "uncommitted_mutations": [],
        "verified_since_mutation": False,
        "milestone_prompted_for_mutations": [],
        "error_streak": {"signature": "", "count": 0},
        "edit_fail_streak": {},
        "loop_intervened_keys": [],
        "pending_loop_breaker": "",
        "pending_l2_hints": [],
        "pending_l1_hints": [],
        "cochange_warned_pairs": [],
    }


MAX_TRACKED_SESSIONS = 32


def _normalize_session_state(raw_state: Dict[str, Any], conv_id: str) -> Dict[str, Any]:
    state = dict(raw_state)
    default_state = default_runtime_state(conv_id)
    for k, v in default_state.items():
        state.setdefault(k, v)
    return state


def _read_state_container(state_file: Path) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Read multi-session state container from runtime_state.json (migrating legacy flat format)."""
    if not state_file.exists():
        return {}, ""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}, ""
        if isinstance(data.get("sessions"), dict):
            sessions = {
                str(k): dict(v)
                for k, v in data["sessions"].items()
                if isinstance(v, dict)
            }
            latest = str(data.get("latest_session_id") or "")
            return sessions, latest
        # Legacy single-session flat structure migration
        legacy_id = str(data.get("conversation_id") or "default")
        return {legacy_id: dict(data)}, legacy_id
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read runtime_state.json: %s", e)
        return {}, ""


def load_runtime_state(workspace_root: str, conv_id: str = "default") -> Dict[str, Any]:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    sessions, latest_id = _read_state_container(state_file)
    target_id = (conv_id or "").strip() or "default"

    if target_id in sessions:
        return _normalize_session_state(sessions[target_id], target_id)

    # When caller omits session_id ("default"), fall back to latest active session if present
    if target_id == "default" and latest_id and latest_id in sessions:
        return _normalize_session_state(sessions[latest_id], latest_id)

    return default_runtime_state(target_id)


def save_runtime_state(workspace_root: str, state: Dict[str, Any]) -> None:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    conv_id = str(state.get("conversation_id") or "default")
    sessions, _ = _read_state_container(state_file)

    if conv_id in sessions:
        sessions.pop(conv_id, None)
    sessions[conv_id] = _normalize_session_state(state, conv_id)

    while len(sessions) > MAX_TRACKED_SESSIONS:
        oldest_key = next(iter(sessions))
        sessions.pop(oldest_key, None)

    container = {
        "latest_session_id": conv_id,
        "sessions": sessions,
    }
    try:
        state_file.write_text(
            json.dumps(container, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.debug("Failed to write runtime_state.json: %s", e)


def record_tool_outcome_for_loop(
    state: Dict[str, Any],
    *,
    has_failure: bool,
    error_sig: str = "",
    mutated_files: Optional[List[str]] = None,
    verify_passed: bool = False,
) -> Optional[str]:
    """
    Deterministic zero-LLM failure-loop detector.
    Tracks consecutive identical error signatures and edit->fail streaks on the same file.
    Returns a `<tmem_loop_breaker>` intervention block when threshold is reached, else None.
    """
    if verify_passed:
        state["error_streak"] = {"signature": "", "count": 0}
        state["edit_fail_streak"] = {}
        state["pending_loop_breaker"] = ""
        return None

    if not has_failure:
        return None

    intervened = list(state.get("loop_intervened_keys", []))
    reasons: List[str] = []
    trigger_keys: List[str] = []

    if error_sig:
        streak = state.get("error_streak") or {"signature": "", "count": 0}
        if streak.get("signature") == error_sig:
            streak["count"] = int(streak.get("count", 0)) + 1
        else:
            streak = {"signature": error_sig, "count": 1}
        state["error_streak"] = streak
        if streak["count"] >= LOOP_STREAK_THRESHOLD:
            key = f"err:{error_sig}"
            if key not in intervened:
                trigger_keys.append(key)
                reasons.append(
                    f"Same error signature repeated {streak['count']} times in a row: `{error_sig}`"
                )

    edit_streak: Dict[str, int] = dict(state.get("edit_fail_streak") or {})
    target_files = list(mutated_files or [])
    if not target_files and state.get("uncommitted_mutations"):
        target_files = [state["uncommitted_mutations"][-1]]

    for fp in target_files:
        edit_streak[fp] = int(edit_streak.get(fp, 0)) + 1
        if edit_streak[fp] >= LOOP_STREAK_THRESHOLD:
            key = f"file:{fp}"
            if key not in intervened:
                trigger_keys.append(key)
                reasons.append(
                    f"Repeated edit/failure loop ({edit_streak[fp]}x) involving `{fp}`"
                )
    state["edit_fail_streak"] = edit_streak

    if not trigger_keys:
        return None

    intervened.extend(trigger_keys)
    state["loop_intervened_keys"] = intervened
    msg = (
        "<tmem_loop_breaker>\n"
        "Failure-loop circuit breaker triggered:\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\nAction required: Stop repeating local edits or re-running the same failing command. "
        "Re-read the surrounding module/imports or switch your debugging hypothesis before editing again.\n"
        "</tmem_loop_breaker>"
    )
    return msg


def apply_session_trace_feedback(store: Any, state: Dict[str, Any], outcome: str) -> None:
    """
    Apply deterministic outcome feedback ('success' | 'contradict') to traces injected
    earlier in this conversation, ensuring each trace is rewarded/penalized at most once.
    """
    injected = list(state.get("injected_trace_ids") or [])
    if not injected:
        return

    if outcome == "success":
        already = set(state.get("rewarded_trace_ids") or [])
        targets = [tid for tid in injected if tid not in already]
        if targets:
            store.record_trace_outcome(targets, "success")
            state["rewarded_trace_ids"] = list(already | set(targets))
    elif outcome == "contradict":
        already = set(state.get("penalized_trace_ids") or [])
        targets = [tid for tid in injected if tid not in already]
        if targets:
            store.record_trace_outcome(targets, "contradict")
            state["penalized_trace_ids"] = list(already | set(targets))
