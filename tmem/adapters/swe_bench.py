"""
SWE-bench & ATIF (Agent Trajectory Interchange Format) Public Trajectory Adapter.

Enables tmem to ingest and evaluate public coding agent trajectories from:
1. SWE-agent (.traj JSON format from SWE-bench/experiments & nebius/SWE-agent-trajectories)
2. ATIF (Agent Trajectory Interchange Format / Harbor trajectory.json)
3. OpenHands / Agentless public benchmark trajectories
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from tmem.adapters.base import (
    AgentAdapter,
    ParsedSession,
    extract_assistant_conclusion,
    extract_error_signature,
    extract_symbols_from_text,
    normalize_rel_path,
)
from tmem.models import ActionKind, ActionNode


class SweBenchAdapter(AgentAdapter):
    """
    Adapter for SWE-bench, SWE-agent (.traj), and ATIF standardized agent trajectories.
    """

    name: str = "swe_bench"

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

        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                return session
            # Check if JSON or JSONL
            if content.startswith("{"):
                data = json.loads(content)
                self._parse_json_dict(data, session, workspace_root, min_step_idx)
            else:
                # JSONL stream
                self._parse_jsonl_lines(content.splitlines(), session, workspace_root, min_step_idx)
        except Exception:
            pass

        return session

    def _parse_json_dict(
        self, data: Dict[str, Any], session: ParsedSession, workspace_root: str, min_step_idx: int
    ) -> None:
        # Extract instance_id & problem statement
        instance_id = data.get("instance_id") or data.get("session_id") or data.get("issue_id") or session.session_id
        session.session_id = str(instance_id)

        problem_stmt = (
            data.get("problem_statement")
            or data.get("issue_description")
            or data.get("prompt")
            or data.get("task_description")
            or ""
        )
        if problem_stmt:
            session.user_intents.append(str(problem_stmt).strip()[:1000])

        # Detect schema: SWE-agent .traj vs ATIF vs OpenHands
        if "trajectory" in data and isinstance(data["trajectory"], list):
            self._parse_swe_agent_trajectory(data["trajectory"], session, workspace_root, min_step_idx)
        elif "steps" in data and isinstance(data["steps"], list):
            self._parse_atif_steps(data["steps"], session, workspace_root, min_step_idx)
        elif "history" in data and isinstance(data["history"], list):
            self._parse_swe_agent_trajectory(data["history"], session, workspace_root, min_step_idx)
        elif "events" in data and isinstance(data["events"], list):
            self._parse_atif_steps(data["events"], session, workspace_root, min_step_idx)

    def _parse_swe_agent_trajectory(
        self,
        traj: List[Dict[str, Any]],
        session: ParsedSession,
        workspace_root: str,
        min_step_idx: int,
    ) -> None:
        last_file_opened: Optional[str] = None

        for idx, turn in enumerate(traj, start=1):
            if idx < min_step_idx:
                continue

            action_str = str(turn.get("action") or "").strip()
            obs_str = str(turn.get("observation") or "").strip()
            thought = str(turn.get("thought") or "")
            response = str(turn.get("response") or "")

            # If queries / messages exist, extract user prompt if not present
            if not session.user_intents and "query" in turn:
                queries = turn.get("query")
                if isinstance(queries, list):
                    for q in queries:
                        if isinstance(q, dict) and q.get("role") == "user":
                            session.user_intents.append(str(q.get("content", ""))[:1000])

            if response:
                conc = extract_assistant_conclusion(response)
                if conc:
                    session.assistant_conclusions.append(conc)
            elif thought:
                conc = extract_assistant_conclusion(thought)
                if conc:
                    session.assistant_conclusions.append(conc)

            node = self._normalize_swe_agent_action(idx, action_str, obs_str, last_file_opened, workspace_root)
            if node:
                if node.kind == ActionKind.READ and node.target_file:
                    last_file_opened = node.target_file
                if node.kind == ActionKind.MUTATE and node.target_file:
                    last_file_opened = node.target_file

                # Determine exit code and error signature
                exit_code = turn.get("exit_code")
                if exit_code is None:
                    # SWE-agent observations usually print exit errors or traceback
                    if any(err in obs_str for err in ("FAILED (failures=", "ERROR: ", "Traceback (most recent call last):")):
                        exit_code = 1
                    else:
                        exit_code = 0
                node.exit_code = int(exit_code)

                if node.exit_code != 0 and obs_str:
                    err_sig = extract_error_signature(obs_str)
                    if err_sig:
                        node.error_signature = err_sig
                        session.active_errors.append(err_sig)

                session.actions.append(node)

    def _normalize_swe_agent_action(
        self,
        step_idx: int,
        action_str: str,
        obs_str: str,
        last_file_opened: Optional[str],
        workspace_root: str,
    ) -> Optional[ActionNode]:
        if not action_str:
            return None

        # 1. open_file / view / cat commands
        m_open = re.match(r"^(?:open_file|view|cat|read)\s+([^\s\n]+)", action_str)
        if m_open:
            fpath = normalize_rel_path(m_open.group(1), workspace_root)
            return ActionNode(step_idx=step_idx, kind=ActionKind.READ, tool_name="open_file", target_file=fpath)

        # 2. edit_file / edit / str_replace / patch
        m_edit = re.match(r"^(?:edit_file|edit|patch|create_file|write_file)(?:\s+([^\s\n]+))?", action_str)
        if m_edit:
            target = m_edit.group(1) or last_file_opened
            fpath = normalize_rel_path(target, workspace_root)
            syms = extract_symbols_from_text(action_str, fpath)
            return ActionNode(
                step_idx=step_idx,
                kind=ActionKind.MUTATE,
                tool_name="edit_file",
                target_file=fpath,
                symbols_touched=syms,
            )

        # 3. Environment execution (pytest, python, tox, bash commands)
        return ActionNode(
            step_idx=step_idx,
            kind=ActionKind.ENV_EXEC,
            tool_name="bash",
            command=action_str,
        )

    def _parse_atif_steps(
        self,
        steps: List[Dict[str, Any]],
        session: ParsedSession,
        workspace_root: str,
        min_step_idx: int,
    ) -> None:
        for idx, step in enumerate(steps, start=1):
            if idx < min_step_idx:
                continue

            stype = step.get("type") or step.get("role") or ""
            content = step.get("content") or ""

            if stype in ("user", "human"):
                session.user_intents.append(str(content)[:1000])
                continue

            if stype in ("thought", "assistant", "model"):
                conc = extract_assistant_conclusion(str(content))
                if conc:
                    session.assistant_conclusions.append(conc)

            # Tool call
            tool_name = (step.get("tool_name") or step.get("tool") or step.get("name") or "").lower()
            tool_input = step.get("tool_input") or step.get("arguments") or step.get("input") or {}
            obs = str(step.get("observation") or step.get("output") or "")
            exit_code = int(step.get("exit_code") or step.get("exitCode") or 0)

            node: Optional[ActionNode] = None
            if any(k in tool_name for k in ("read", "view", "open", "file_reader")):
                fpath = normalize_rel_path(tool_input.get("path") or tool_input.get("file_path"), workspace_root)
                node = ActionNode(step_idx=idx, kind=ActionKind.READ, tool_name=tool_name, target_file=fpath)
            elif any(k in tool_name for k in ("edit", "write", "patch", "replace", "file_editor")):
                fpath = normalize_rel_path(tool_input.get("path") or tool_input.get("file_path"), workspace_root)
                body = str(tool_input.get("content") or tool_input.get("new_str") or tool_input.get("patch") or "")
                syms = extract_symbols_from_text(body, fpath)
                node = ActionNode(step_idx=idx, kind=ActionKind.MUTATE, tool_name=tool_name, target_file=fpath, symbols_touched=syms)
            elif tool_name in ("bash", "exec", "terminal", "shell", "cmd") or "command" in tool_input:
                cmd = tool_input.get("command") or tool_input.get("cmd") or ""
                node = ActionNode(step_idx=idx, kind=ActionKind.ENV_EXEC, tool_name="bash", command=str(cmd))
            elif tool_name:
                node = ActionNode(step_idx=idx, kind=ActionKind.OTHER, tool_name=tool_name)

            if node:
                node.exit_code = exit_code
                if exit_code != 0 and obs:
                    err_sig = extract_error_signature(obs)
                    if err_sig:
                        node.error_signature = err_sig
                        session.active_errors.append(err_sig)
                session.actions.append(node)

    def _parse_jsonl_lines(
        self,
        lines: List[str],
        session: ParsedSession,
        workspace_root: str,
        min_step_idx: int,
    ) -> None:
        steps: List[Dict[str, Any]] = []
        for ln in lines:
            if not ln.strip():
                continue
            try:
                steps.append(json.loads(ln))
            except Exception:
                pass
        self._parse_atif_steps(steps, session, workspace_root, min_step_idx)

    def discover_latest_transcript(self, workspace_root: str) -> Optional[str]:
        ws = Path(workspace_root)
        candidates: List[Path] = []
        for pat in ("*.traj", "*.traj.json", "trajectory.json", "atif.json"):
            candidates.extend(ws.rglob(pat))
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])
