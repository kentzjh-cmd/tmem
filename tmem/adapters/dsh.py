"""Native DeepSeek Harness (@deepseek-ai/dsh) adapter for session.v4.jsonl transcripts and Cordis hooks."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tmem.adapters.base import (
    AgentAdapter,
    ParsedSession,
    extract_assistant_conclusion,
    extract_error_signature,
    extract_symbols_from_text,
    is_filler_user_intent,
    normalize_rel_path,
    scrub_secrets,
)
from tmem.models import ActionKind, ActionNode

DSH_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)
APPLY_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:(?:Update|Add|Move to)\s+File:\s+)?([^\r\n]+)", re.MULTILINE
)


def _extract_dsh_text(content: Any) -> str:
    """Extract concatenated plain text from dsh content parts or strings."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return _extract_dsh_text(content["content"])
    return str(content)


def _ms_to_iso(ts_val: Any) -> str:
    """Convert dsh millisecond epoch timestamp to ISO-8601 string."""
    if isinstance(ts_val, (int, float)) and ts_val > 0:
        try:
            return datetime.fromtimestamp(ts_val / 1000.0, tz=timezone.utc).isoformat()
        except Exception:
            return ""
    if isinstance(ts_val, str):
        return ts_val
    return ""


def parse_apply_patch_targets(patch_text: str) -> List[Tuple[str, str]]:
    """
    Parse Codex / dsh `apply_patch` input into a list of (file_path, added_lines_text).
    """
    if not patch_text:
        return []
    results: List[Tuple[str, str]] = []
    current_file: Optional[str] = None
    added_lines: List[str] = []

    for raw_line in patch_text.splitlines():
        m = APPLY_PATCH_FILE_RE.match(raw_line.strip())
        if m:
            if current_file:
                results.append((current_file, "\n".join(added_lines)))
            current_file = m.group(1).strip()
            added_lines = []
            continue
        if current_file is not None:
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added_lines.append(raw_line[1:])

    if current_file:
        results.append((current_file, "\n".join(added_lines)))
    return results


class DshAdapter(AgentAdapter):
    """
    Native adapter for DeepSeek Harness (`@deepseek-ai/dsh`).
    Supports:
    - `$DSH_HOME/sessions/<sessionId>/session.v4.jsonl` (and v3 / v0 `session.jsonl`)
    - Generic / fallback JSONL execution logs
    """

    name = "dsh"

    def parse_transcript(
        self, transcript_path: str, workspace_root: str, min_step_idx: int = 0
    ) -> ParsedSession:
        path = Path(transcript_path)
        session_id = (
            path.parent.name
            if path.name.startswith("session.") and path.parent.name != "sessions"
            else path.stem
        )
        session = ParsedSession(
            agent_name="dsh",
            session_id=session_id,
            workspace_root=workspace_root,
        )
        if not path.exists():
            return session

        call_id_to_nodes: Dict[str, List[ActionNode]] = {}
        seq_to_nodes: Dict[int, List[ActionNode]] = {}
        pending_nodes: List[ActionNode] = []
        line_Step = 0

        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Header line in session.v4.jsonl / session.v3.jsonl
                if (
                    isinstance(entry, dict)
                    and "version" in entry
                    and "id" in entry
                    and "type" not in entry
                ):
                    if isinstance(entry.get("id"), str) and entry["id"]:
                        session.session_id = entry["id"]
                    continue

                line_Step += 1
                event_type = entry.get("type") or entry.get("role") or ""
                seq_val = entry.get("seq")
                step_idx = int(seq_val) if isinstance(seq_val, int) else line_Step
                if step_idx < min_step_idx:
                    continue

                timestamp = _ms_to_iso(entry.get("time") or entry.get("created_at") or "")
                data = entry.get("data") if isinstance(entry.get("data"), dict) else entry

                # 1. Native dsh `user/message` (v4 & v3) or generic `user` role
                if event_type in ("user/message", "user", "human", "USER_INPUT"):
                    msg_obj = data.get("message") if isinstance(data.get("message"), dict) else data
                    source = msg_obj.get("source") if isinstance(msg_obj, dict) else None
                    source_kind = source.get("kind") if isinstance(source, dict) else "user"
                    # Ignore system-injected notices like agent-instructions or tmem memory blocks
                    if source_kind not in ("user", None, ""):
                        continue
                    text = scrub_secrets(
                        _extract_dsh_text(
                            msg_obj.get("content")
                            if isinstance(msg_obj, dict) and "content" in msg_obj
                            else entry.get("content") or ""
                        )
                    ).strip()
                    if (
                        text
                        and not text.startswith("/")
                        and not text.startswith("<tmem_")
                        and text != "[REDACTED_SECRET]"
                        and not is_filler_user_intent(text)
                    ):
                        session.user_intents.append(text[:500])
                    continue

                # 1b. Native dsh `assistant/message` or generic `assistant` role
                if event_type in ("assistant/message", "assistant"):
                    msg_obj = data.get("message") if isinstance(data.get("message"), dict) else data
                    asst_text = _extract_dsh_text(
                        msg_obj.get("content")
                        if isinstance(msg_obj, dict) and "content" in msg_obj
                        else entry.get("content") or ""
                    )
                    conclusion = extract_assistant_conclusion(asst_text)
                    if conclusion:
                        session.assistant_conclusions.append(conclusion)
                    if event_type == "assistant/message":
                        continue

                # 2. Native dsh `tool/call` event
                if event_type == "tool/call":
                    call_id = str(data.get("callId") or data.get("id") or f"seq_{step_idx}")
                    tool_name = str(data.get("name") or "tool")
                    raw_args = data.get("arguments") or data.get("args") or {}
                    args = self._parse_args(raw_args)
                    nodes = self._map_dsh_tool_call(
                        step_idx, tool_name, args, workspace_root, timestamp
                    )
                    session.actions.extend(nodes)
                    call_id_to_nodes[call_id] = nodes
                    if isinstance(seq_val, int):
                        seq_to_nodes[seq_val] = nodes
                    pending_nodes.extend(nodes)
                    continue

                # 3. Native dsh `tool/result` event
                if event_type == "tool/result":
                    msg_obj = data.get("message") if isinstance(data.get("message"), dict) else {}
                    call_id = str(msg_obj.get("callId") or data.get("callId") or "")
                    target_nodes = call_id_to_nodes.get(call_id)
                    if not target_nodes:
                        src_seqs = entry.get("sourceEventSeqs") or []
                        for s in src_seqs:
                            if isinstance(s, int) and s in seq_to_nodes:
                                target_nodes = seq_to_nodes[s]
                                break
                    if not target_nodes and pending_nodes:
                        target_nodes = [pending_nodes.pop(0)]

                    if target_nodes:
                        out_text = _extract_dsh_text(msg_obj.get("content") or data.get("content") or "")
                        is_error = bool(msg_obj.get("isError") or data.get("isError") or data.get("error"))
                        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
                        exit_code = self._extract_exit_code(out_text, meta, is_error, target_nodes[0].kind)
                        err_sig = None
                        if is_error or (exit_code is not None and exit_code != 0) or "Error" in out_text or "Exception" in out_text or "FAIL" in out_text:
                            err_sig = extract_error_signature(out_text)
                            if not err_sig and is_error and isinstance(data.get("error"), dict):
                                err_sig = extract_error_signature(str(data["error"].get("message") or ""))

                        for t_node in target_nodes:
                            if exit_code is not None:
                                t_node.exit_code = exit_code
                            if err_sig and (
                                is_error
                                or (t_node.exit_code not in (None, 0))
                                or t_node.kind == ActionKind.ENV_EXEC
                            ):
                                t_node.error_signature = err_sig
                                session.active_errors.append(err_sig)
                            t_node.output_snippet = scrub_secrets(out_text[:400])
                    continue

                # 4. Fallback for generic JSONL tool_calls / inline command logs
                tool_calls = (
                    entry.get("tool_calls")
                    or entry.get("toolCalls")
                    or ([entry] if event_type in ("function_call", "tool_use", "command") else [])
                )
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    t_name = str(
                        tc.get("name")
                        or fn.get("name")
                        or tc.get("tool")
                        or tc.get("type")
                        or "tool"
                    )
                    args = self._parse_args(
                        tc.get("arguments") or fn.get("arguments") or tc.get("args") or tc.get("input") or {}
                    )
                    nodes = self._map_dsh_tool_call(step_idx, t_name, args, workspace_root, timestamp)
                    for node in nodes:
                        if "exit_code" in entry or "exitCode" in entry:
                            node.exit_code = int(entry.get("exit_code") or entry.get("exitCode") or 0)
                        out_text = str(entry.get("output") or entry.get("stdout") or entry.get("stderr") or "")
                        if out_text:
                            err_sig = extract_error_signature(out_text)
                            if err_sig and node.exit_code not in (0,):
                                node.error_signature = err_sig
                                session.active_errors.append(err_sig)
                            node.output_snippet = scrub_secrets(out_text[:400])
                        session.actions.append(node)
                        pending_nodes.append(node)

        return session

    @staticmethod
    def _parse_args(raw_args: Any) -> Dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            s = raw_args.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {"command": s}
        return {}

    @staticmethod
    def _extract_exit_code(
        out_text: str, meta: Dict[str, Any], is_error: bool, kind: ActionKind
    ) -> Optional[int]:
        if isinstance(meta.get("exitCode"), int):
            return int(meta["exitCode"])
        if isinstance(meta.get("exit_code"), int):
            return int(meta["exit_code"])
        m = DSH_EXIT_CODE_RE.search(out_text or "")
        if m:
            return int(m.group(1))
        if is_error:
            return 1
        if kind == ActionKind.ENV_EXEC:
            # dsh-tool-bash omits `[exit code: N]` when exitCode === 0
            return 0
        return None

    def _map_dsh_tool_call(
        self,
        step_idx: int,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: str,
        timestamp: str,
    ) -> List[ActionNode]:
        low = tool_name.lower()

        # 1. `apply_patch` (Codex / dsh multi-file patch format)
        if low == "apply_patch":
            patch_input = str(args.get("input") or args.get("patch") or args.get("diff") or "")
            targets = parse_apply_patch_targets(patch_input)
            if targets:
                nodes: List[ActionNode] = []
                for raw_path, added_body in targets:
                    nodes.append(
                        ActionNode(
                            step_idx=step_idx,
                            kind=ActionKind.MUTATE,
                            tool_name=tool_name,
                            target_file=normalize_rel_path(raw_path, workspace_root),
                            symbols_touched=extract_symbols_from_text(added_body, raw_path),
                            timestamp=timestamp,
                        )
                    )
                return nodes

        file_arg = (
            args.get("file_path")
            or args.get("path")
            or args.get("filePath")
            or args.get("Path")
            or args.get("TargetFile")
        )
        cmd_arg = args.get("command") or args.get("cmd") or args.get("CommandLine")
        if isinstance(cmd_arg, list):
            cmd_arg = " ".join(str(x) for x in cmd_arg)

        # 2. Read / search tools (`read`, `read_image`, `glob`, `grep`)
        if low in ("read", "read_image", "glob", "grep", "read_file", "view_file"):
            return [
                ActionNode(
                    step_idx=step_idx,
                    kind=ActionKind.READ,
                    tool_name=tool_name,
                    target_file=normalize_rel_path(file_arg, workspace_root),
                    timestamp=timestamp,
                )
            ]

        # 3. File mutation tools (`write`, `edit`, `str_replace_editor`)
        if low in ("write", "edit", "str_replace_editor", "write_file", "edit_file", "apply_patch"):
            code_body = str(
                args.get("content")
                or args.get("new_string")
                or args.get("new_str")
                or args.get("file_text")
                or args.get("replacement")
                or args.get("old_string")
                or ""
            )
            return [
                ActionNode(
                    step_idx=step_idx,
                    kind=ActionKind.MUTATE,
                    tool_name=tool_name,
                    target_file=normalize_rel_path(file_arg, workspace_root),
                    symbols_touched=extract_symbols_from_text(code_body, str(file_arg or "")),
                    timestamp=timestamp,
                )
            ]

        # 4. Shell / verification tools (`bash`, `pwsh`)
        if low in ("bash", "pwsh", "run_command", "shell", "exec") or cmd_arg:
            return [
                ActionNode(
                    step_idx=step_idx,
                    kind=ActionKind.ENV_EXEC,
                    tool_name=tool_name,
                    command=scrub_secrets(str(cmd_arg or "")),
                    timestamp=timestamp,
                )
            ]

        return [
            ActionNode(
                step_idx=step_idx,
                kind=ActionKind.OTHER,
                tool_name=tool_name,
                timestamp=timestamp,
            )
        ]

    def discover_latest_transcript(
        self, workspace_root: str, session_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Locate the most recently modified `dsh` session transcript (`session.v4.jsonl`,
        `session.v3.jsonl`, or `session.jsonl`) in `$DSH_HOME/sessions`, `~/.dsh/sessions`,
        or `<workspace>/.dsh/sessions`.
        """
        dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
        ws = Path(workspace_root).resolve()
        search_roots = [
            dsh_home / "sessions",
            ws / ".dsh" / "sessions",
            Path.home() / ".dsh" / "sessions",
            Path.home() / ".local" / "share" / "dsh",
        ]

        if session_id:
            for root in search_roots:
                sess_dir = root / session_id
                if sess_dir.is_dir():
                    for fname in ("session.v4.jsonl", "session.v3.jsonl", "session.jsonl"):
                        candidate = sess_dir / fname
                        if candidate.exists():
                            return str(candidate)

        candidates: List[Path] = []
        seen: set[Path] = set()
        for root in search_roots:
            if not root.exists():
                continue
            for p in root.rglob("*.jsonl"):
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(p)

        if not candidates:
            return None

        # Sort by mtime descending, preferring session.v4.jsonl when timestamps match
        def _sort_key(p: Path) -> Tuple[float, int]:
            prio = 2 if p.name == "session.v4.jsonl" else (1 if p.name == "session.v3.jsonl" else 0)
            return (p.stat().st_mtime, prio)

        candidates.sort(key=_sort_key, reverse=True)
        return str(candidates[0])
