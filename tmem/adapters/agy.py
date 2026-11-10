"""Native Antigravity (agy) adapter for transcript parsing and lifecycle hooks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from tmem.adapters.base import (
    AgentAdapter,
    ParsedSession,
    extract_error_signature,
    extract_symbols_from_text,
    normalize_rel_path,
    scrub_secrets,
)
from tmem.models import ActionKind, ActionNode

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
EXIT_CODE_RE = re.compile(r"Command exited with code (\d+)")


def _clean_agy_arg(val: Any) -> str:
    """Clean stringified JSON argument values from agy transcript.jsonl."""
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
            try:
                return str(json.loads(s))
            except Exception:
                return s.strip('"')
        return s
    return str(val)


class AgyAdapter(AgentAdapter):
    """Adapter for Antigravity CLI / IDE (agy)."""

    name = "agy"

    def parse_transcript(
        self, transcript_path: str, workspace_root: str, min_step_idx: int = 0
    ) -> ParsedSession:
        path = Path(transcript_path)
        session_id = path.parent.parent.parent.name if ".system_generated" in str(path) else path.stem
        session = ParsedSession(
            agent_name="agy",
            session_id=session_id,
            workspace_root=workspace_root,
        )
        if not path.exists():
            return session

        pending_nodes: List[ActionNode] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                step_type = entry.get("type", "")
                step_idx = int(entry.get("step_index", 0))
                if step_idx < min_step_idx:
                    continue
                created_at = entry.get("created_at", "")
                content = entry.get("content", "") or ""

                if step_type == "USER_INPUT":
                    m = USER_REQ_RE.search(content)
                    user_text = scrub_secrets(m.group(1).strip() if m else content.strip())
                    if user_text and not user_text.startswith("/") and user_text != "[REDACTED_SECRET]":
                        session.user_intents.append(user_text)

                elif step_type == "PLANNER_RESPONSE":
                    pending_nodes = []
                    for tc in entry.get("tool_calls", []) or []:
                        t_name = tc.get("name", "")
                        args = tc.get("args", {}) or {}
                        node = self._map_tool_call(
                            step_idx, t_name, args, workspace_root, created_at
                        )
                        session.actions.append(node)
                        pending_nodes.append(node)

                elif step_type == "GENERIC" and pending_nodes:
                    # Match tool output back to the pending tool call node
                    target_node = pending_nodes.pop(0)
                    exit_match = EXIT_CODE_RE.search(content)
                    if exit_match:
                        target_node.exit_code = int(exit_match.group(1))
                    if target_node.exit_code not in (None, 0) or "Error" in content or "Exception" in content or "FAIL" in content:
                        err_sig = extract_error_signature(content)
                        if err_sig and (target_node.exit_code not in (None, 0) or target_node.kind == ActionKind.ENV_EXEC):
                            target_node.error_signature = err_sig
                            session.active_errors.append(err_sig)
                    target_node.output_snippet = content[:400]

        return session

    def _map_tool_call(
        self,
        step_idx: int,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: str,
        timestamp: str,
    ) -> ActionNode:
        if tool_name in ("read_file", "view_file", "grep_search", "find_by_name"):
            raw_path = _clean_agy_arg(args.get("Path") or args.get("SearchPath"))
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.READ,
                tool_name=tool_name,
                target_file=normalize_rel_path(raw_path, workspace_root),
                timestamp=timestamp,
            )
        if tool_name in ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
            raw_path = _clean_agy_arg(args.get("Path") or args.get("TargetFile"))
            code_body = _clean_agy_arg(
                args.get("Content") or args.get("Replacement") or args.get("Target") or ""
            )
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.MUTATE,
                tool_name=tool_name,
                target_file=normalize_rel_path(raw_path, workspace_root),
                symbols_touched=extract_symbols_from_text(code_body),
                timestamp=timestamp,
            )
        if tool_name in ("run_command", "bash"):
            cmd = scrub_secrets(_clean_agy_arg(args.get("CommandLine") or args.get("command")))
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.ENV_EXEC,
                tool_name=tool_name,
                command=cmd,
                timestamp=timestamp,
            )
        return ActionNode(
            step_idx=step_idx,
            kind=ActionKind.OTHER,
            tool_name=tool_name,
            timestamp=timestamp,
        )

    def discover_latest_transcript(self, workspace_root: str) -> Optional[str]:
        """Locate the most recently modified agy transcript.jsonl on the machine."""
        candidates: List[Path] = []
        home = Path.home()
        for app_dir in [
            home / ".gemini" / "antigravity-cli" / "brain",
            home / ".gemini" / "antigravity" / "brain",
            home / ".gemini" / "antigravity-ide" / "brain",
        ]:
            if app_dir.exists():
                candidates.extend(app_dir.glob("*/.system_generated/logs/transcript.jsonl"))
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])
