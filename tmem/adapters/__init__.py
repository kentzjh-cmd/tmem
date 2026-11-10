"""Adapters for codex, opencode, pi, and dsh agent session transcripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tmem.adapters.agy import AgyAdapter
from tmem.adapters.dsh import DshAdapter
from tmem.adapters.base import (
    AgentAdapter,
    ParsedSession,
    extract_error_signature,
    extract_symbols_from_text,
    normalize_rel_path,
)
from tmem.models import ActionKind, ActionNode


class GenericJsonlAgentAdapter(AgentAdapter):
    """
    Universal JSONL transcript adapter supporting:
    - codex (~/.codex/sessions/*.jsonl)
    - pi (~/.pi/agent/sessions/*.jsonl)
    - opencode (~/.local/share/opencode/ or .opencode/ logs)
    - dsh (shell execution / agent history logs)
    """

    def __init__(self, name: str, search_dirs: List[Path]) -> None:
        self.name = name
        self.search_dirs = search_dirs

    def parse_transcript(
        self, transcript_path: str, workspace_root: str, min_step_idx: int = 0
    ) -> ParsedSession:
        path = Path(transcript_path)
        session = ParsedSession(
            agent_name=self.name,
            session_id=path.stem,
            workspace_root=workspace_root,
        )
        if not path.exists():
            return session

        step_idx = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                step_idx += 1
                if step_idx < min_step_idx:
                    continue
                role = item.get("role") or item.get("type") or ""
                content = item.get("content") or item.get("message") or ""

                # User prompt extraction
                if role in ("user", "human", "USER_INPUT"):
                    text = content if isinstance(content, str) else json.dumps(content)
                    if text.strip():
                        session.user_intents.append(text.strip()[:500])

                # Tool calls in OpenAI/Anthropic/Codex/Pi/OpenCode schemas
                tool_calls = (
                    item.get("tool_calls")
                    or item.get("toolCalls")
                    or ([item] if item.get("type") in ("function_call", "tool_use", "command") else [])
                )
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    t_name = (
                        tc.get("name")
                        or fn.get("name")
                        or tc.get("tool")
                        or tc.get("type")
                        or "tool"
                    )
                    raw_args = tc.get("arguments") or fn.get("arguments") or tc.get("args") or tc.get("input") or {}
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except Exception:
                            args = {"command": raw_args}
                    elif isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        args = {}

                    node = self._normalize_call(step_idx, t_name, args, workspace_root)
                    # Check inline exit code or output if logged in the same event (e.g., dsh / pi)
                    if "exit_code" in item or "exitCode" in item:
                        node.exit_code = int(item.get("exit_code") or item.get("exitCode") or 0)
                    out_text = str(item.get("output") or item.get("stdout") or item.get("stderr") or "")
                    if out_text:
                        err_sig = extract_error_signature(out_text)
                        if err_sig and node.exit_code not in (0,):
                            node.error_signature = err_sig
                            session.active_errors.append(err_sig)
                    session.actions.append(node)

                # Tool result messages
                if role in ("tool", "function_call_output", "tool_result") and session.actions:
                    last_node = session.actions[-1]
                    text = content if isinstance(content, str) else json.dumps(content)
                    if "exit_code" in item:
                        last_node.exit_code = int(item["exit_code"])
                    err_sig = extract_error_signature(text)
                    if err_sig:
                        last_node.error_signature = err_sig
                        session.active_errors.append(err_sig)

        return session

    def _normalize_call(
        self, step_idx: int, t_name: str, args: Dict[str, Any], workspace_root: str
    ) -> ActionNode:
        low = t_name.lower()
        file_arg = (
            args.get("path")
            or args.get("file_path")
            or args.get("filePath")
            or args.get("Path")
            or args.get("file")
        )
        cmd_arg = (
            args.get("command")
            or args.get("cmd")
            or args.get("CommandLine")
            or ( " ".join(args[" command"]) if isinstance(args.get("command"), list) else None )
        )
        if isinstance(cmd_arg, list):
            cmd_arg = " ".join(str(x) for x in cmd_arg)

        if any(k in low for k in ("read", "view", "cat", "grep", "glob", "search")):
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.READ,
                tool_name=t_name,
                target_file=normalize_rel_path(file_arg, workspace_root),
            )
        if any(k in low for k in ("write", "edit", "patch", "replace", "apply_diff")):
            body = str(args.get("content") or args.get("diff") or args.get("new_str") or args.get("replacement") or "")
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.MUTATE,
                tool_name=t_name,
                target_file=normalize_rel_path(file_arg, workspace_root),
                symbols_touched=extract_symbols_from_text(body, str(file_arg or "")),
            )
        if cmd_arg or any(k in low for k in ("bash", "exec", "shell", "run", "command", "terminal")):
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.ENV_EXEC,
                tool_name=t_name,
                command=str(cmd_arg or ""),
            )
        return ActionNode(step_idx=step_idx, kind=ActionKind.OTHER, tool_name=t_name)

    def discover_latest_transcript(self, workspace_root: str) -> Optional[str]:
        candidates: List[Path] = []
        for d in self.search_dirs:
            if d.exists():
                candidates.extend(d.rglob("*.jsonl"))
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])


def get_adapter(agent_name: str = "agy") -> AgentAdapter:
    """Factory returning the adapter for agy, codex, opencode, pi, or dsh."""
    home = Path.home()
    adapters: Dict[str, AgentAdapter] = {
        "agy": AgyAdapter(),
        "codex": GenericJsonlAgentAdapter("codex", [home / ".codex" / "sessions"]),
        "pi": GenericJsonlAgentAdapter("pi", [home / ".pi" / "agent" / "sessions"]),
        "opencode": GenericJsonlAgentAdapter(
            "opencode", [home / ".local" / "share" / "opencode", Path(".opencode")]
        ),
        "dsh": DshAdapter(),
    }
    return adapters.get(agent_name.lower(), AgyAdapter())
