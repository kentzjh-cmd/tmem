"""
Headless CLI, `/tmem` Slash-Command Interceptor, and Autonomous Milestone Detector for tmem.

Supports:
1. `/tmem [status|list|forget|gc|inspect|replay|diff|sync] [intent]` command interception via `PreInvocation` hook.
2. Autonomous Goal-Achievement Prompting: Detects `MUTATE -> VERIFY(exit 0)` milestones in `PostToolUse`
   and prompts the agent (via `<tmem_milestone_prompt>` + `AGENTS.md`) to ask whether to persist memory
   at Project (`.tmem/`) scope.
3. Direct CLI execution (`python3 -m tmem.cli capture`) for natural-language
   confirmations or multi-agent wrappers (`agy`, `codex`, `opencode`, `pi`, `dsh`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tmem.adapters import get_adapter
from tmem.adapters.base import extract_error_signature, normalize_rel_path
from tmem.adapters.dsh import DSH_EXIT_CODE_RE, DshAdapter, parse_apply_patch_targets
from tmem.guards import format_injected_context
from tmem.raw_storage import (
    RawTraceStore,
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore

SLASH_TMEM_RE = re.compile(
    r"^/(?:tmem|remember|trace)(?:\s+(project|global|both|status|list|forget|gc|bench|evaluate|inspect|replay|diff|sync))?(?:\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def _read_stdin_json() -> Dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _default_runtime_state(conv_id: str = "default") -> Dict[str, Any]:
    return {
        "conversation_id": conv_id,
        "recent_errors": [],
        "recent_files": [],
        "injected_trace_ids": [],
        "last_user_input": "",
        "last_captured_step_idx": 0,
        "uncommitted_mutations": [],
        "verified_since_mutation": False,
        "milestone_prompted_for_mutations": [],
    }


def _load_runtime_state(workspace_root: str, conv_id: str = "default") -> Dict[str, Any]:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("conversation_id") == conv_id or conv_id == "default":
                merged = _default_runtime_state(data.get("conversation_id", conv_id))
                merged.update(data)
                return merged
        except Exception:
            pass
    return _default_runtime_state(conv_id)


def _save_runtime_state(workspace_root: str, state: Dict[str, Any]) -> None:
    state_file = Path(workspace_root) / ".tmem" / "runtime_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def execute_capture(
    workspace_root: str,
    agent_name: str = "agy",
    transcript_path: Optional[str] = None,
    scope: str = "project",
    custom_intent: Optional[str] = None,
    full_session: bool = False,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core capture pipeline used by both `/tmem` hook interception and CLI `tmem capture`.
    Saves deterministic execution traces to project repository (.tmem/).
    """
    adapter = get_adapter(agent_name)
    if transcript_path:
        t_path: Optional[str] = transcript_path
    elif isinstance(adapter, DshAdapter):
        t_path = adapter.discover_latest_transcript(workspace_root, session_id=session_id)
    else:
        t_path = adapter.discover_latest_transcript(workspace_root)
    if not t_path or not Path(t_path).exists():
        return {"status": "skipped", "reason": "no_transcript_found"}

    state = _load_runtime_state(workspace_root)
    min_step = 0 if full_session else int(state.get("last_captured_step_idx", 0))

    session = adapter.parse_transcript(t_path, workspace_root, min_step_idx=min_step)
    if not session.actions and min_step > 0:
        # Fallback to full session if no new actions since last capture
        session = adapter.parse_transcript(t_path, workspace_root, min_step_idx=0)

    if custom_intent:
        session.user_intents.append(custom_intent.strip())
    else:
        # Filter out slash commands from user_intents so the problem_intent reflects the actual task
        clean_intents = [
            u for u in session.user_intents if not SLASH_TMEM_RE.match(u.strip())
        ]
        if clean_intents:
            session.user_intents = clean_intents

    slicer = BackwardTraceSlicer()
    pb = slicer.slice_session(session, use_git_ground_truth=True)
    if not pb:
        return {"status": "skipped", "reason": "no_effective_actions"}

    if custom_intent:
        pb.problem_intent = custom_intent.strip()[:300]
        fingerprint = f"{pb.problem_intent}|{'|'.join(pb.touched_files)}|{'|'.join(pb.error_signatures)}"
        pb.trace_id = "tr_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:10]

    # Save full-fidelity raw execution trace for forensic replay & progressive disclosure
    raw_store = RawTraceStore(workspace_root)
    raw_trace = slicer.build_raw_trace(session, pb)
    raw_store.save_raw_trace(raw_trace)
    pb.raw_trace_path = f"raw/{pb.trace_id}.raw.json"

    store = DualTierMemoryStore(workspace_root)
    proj_id, _ = store.save_playbook(pb, target_scope="project")

    # Advance incremental step cursor and reset pending milestone state
    max_step = max((a.step_idx for a in session.actions), default=min_step)
    state["last_captured_step_idx"] = max_step
    state["uncommitted_mutations"] = []
    state["verified_since_mutation"] = False
    state["milestone_prompted_for_mutations"] = []
    _save_runtime_state(workspace_root, state)

    return {
        "status": "compiled",
        "agent": agent_name,
        "scope": "project",
        "project_trace_id": proj_id,
        "raw_trace_path": pb.raw_trace_path,
        "problem_intent": pb.problem_intent,
        "touched_files": pb.touched_files,
        "raw_steps": pb.raw_steps_count,
        "sliced_steps": pb.sliced_steps_count,
        "compression_ratio": f"{pb.raw_steps_count} -> {pb.sliced_steps_count}",
    }


def parse_slash_tmem_command(user_input: str) -> Optional[Tuple[str, str]]:
    """
    Parse `/tmem [project|global|both|status|gc] [optional intent]`.
    Returns (subcommand_or_scope, custom_intent) or None if not a slash command.
    """
    stripped = (user_input or "").strip()
    m = SLASH_TMEM_RE.match(stripped)
    if not m:
        return None
    subcmd = (m.group(1) or "project").lower()
    intent = (m.group(2) or "").strip()
    return subcmd, intent


def handle_agy_hook(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute invisible agy lifecycle hook (`pre_invocation`, `post_tool_use`, `stop`).
    """
    ws_list = payload.get("workspacePaths") or [os.getcwd()]
    workspace_root = ws_list[0] if ws_list else os.getcwd()
    transcript_path = payload.get("transcriptPath") or ""
    conv_id = payload.get("conversationId") or "default"

    store = DualTierMemoryStore(workspace_root)
    state = _load_runtime_state(workspace_root, conv_id)

    if event == "pre_invocation":
        last_input = (payload.get("lastUserInput") or "").strip()
        is_new_user_turn = last_input != state.get("last_user_input", "")
        state["last_user_input"] = last_input

        # 1. Check if the user invoked `/tmem [project|global|both|status|gc]`
        slash_cmd = parse_slash_tmem_command(last_input)
        if slash_cmd and is_new_user_turn:
            action_mode, custom_intent = slash_cmd
            _save_runtime_state(workspace_root, state)

            if action_mode == "status":
                proj_cnt = len(list(store.project_traces_dir.glob("*.json"))) if store.project_traces_dir.exists() else 0
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem status` executed:\n"
                    f"- Project Execution Memory (`{store.project_dir}`): {proj_cnt} trace(s)\n"
                    "Please report these statistics concisely to the user.\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "gc":
                stats = store.run_metabolism_gc()
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem gc` executed: {json.dumps(stats)}\n"
                    "Please report the garbage collection / guard validation result to the user.\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "list":
                items = store.list_playbooks()
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem list` executed ({len(items)} trace(s)):\n"
                    f"{json.dumps(items, indent=2, ensure_ascii=False)}\n"
                    "Please summarize the stored traces concisely for the user.\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode in ("bench", "evaluate"):
                from tmem.evaluate import BenchmarkEvaluator, format_markdown_report
                evaluator = BenchmarkEvaluator()
                summary = evaluator.run_evaluation()
                msg = (
                    "<tmem_command_executed>\n"
                    f"{format_markdown_report(summary)}\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "forget":
                del_res = store.delete_playbook(custom_intent)
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem forget {custom_intent}` executed: {json.dumps(del_res, ensure_ascii=False)}\n"
                    "Please report the deletion result to the user.\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "inspect":
                parts = (custom_intent or "").split()
                tid = parts[0] if parts else ""
                level = parts[1] if len(parts) > 1 else "playbook"
                target_pb = None
                for pb in store.get_all_project_playbooks():
                    if pb.trace_id == tid or pb.trace_id.startswith(tid):
                        target_pb = pb
                        break
                if not target_pb:
                    msg = f"<tmem_command_executed>\nTrace '{tid}' not found in project memory.\n</tmem_command_executed>"
                else:
                    raw_tr = store.get_raw_trace(target_pb.trace_id)
                    view_text = format_progressive_view(target_pb, raw_tr, level=level)
                    msg = f"<tmem_command_executed>\n{view_text}\n</tmem_command_executed>"
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "replay":
                tid = (custom_intent or "").strip()
                raw_tr = store.get_raw_trace(tid)
                if not raw_tr:
                    msg = f"<tmem_command_executed>\nRaw execution trace for '{tid}' not found in .tmem/raw/.\n</tmem_command_executed>"
                else:
                    replay_text = format_raw_trace_replay(raw_tr)
                    msg = f"<tmem_command_executed>\n{replay_text}\n</tmem_command_executed>"
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "diff":
                tid = (custom_intent or "").strip()
                raw_tr = store.get_raw_trace(tid)
                if not raw_tr:
                    msg = f"<tmem_command_executed>\nRaw execution trace for '{tid}' not found in .tmem/raw/.\n</tmem_command_executed>"
                else:
                    diff_text = format_raw_trace_diff(raw_tr)
                    msg = f"<tmem_command_executed>\n```diff\n{diff_text}\n```\n</tmem_command_executed>"
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "sync":
                store.sync_project_index_if_needed()
                store.run_metabolism_gc()
                doc_path = store.sync_memories_markdown()
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem sync` completed successfully:\n"
                    f"- Project index synchronized\n"
                    f"- Living index updated: `{doc_path.name}` and `MEM.md`\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            # Otherwise action_mode is capture (or 'project' / intent)
            res = execute_capture(
                workspace_root=workspace_root,
                agent_name="agy",
                transcript_path=transcript_path or None,
                scope="project",
                custom_intent=custom_intent or None,
            )
            msg = (
                "<tmem_command_executed>\n"
                f"User invoked `/tmem`. Trace compilation completed automatically:\n"
                f"{json.dumps(res, indent=2, ensure_ascii=False)}\n"
                "Please confirm to the user that the memory trace has been persisted to the project repository (`.tmem/traces/`).\n"
                "</tmem_command_executed>"
            )
            return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

        injected_blocks: List[str] = []

        # 2. Check if a Goal Milestone (`MUTATE` + `VERIFY exit 0`) was just reached
        uncommitted = list(state.get("uncommitted_mutations", []))
        prompted_set = set(state.get("milestone_prompted_for_mutations", []))
        if (
            uncommitted
            and state.get("verified_since_mutation")
            and set(uncommitted) != prompted_set
        ):
            state["milestone_prompted_for_mutations"] = uncommitted
            injected_blocks.append(
                "<tmem_milestone_reached>\n"
                f"Milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded.\n"
                "When you finish summarizing your work to the user in this turn, add a brief, non-intrusive one-line prompt asking if they would like to persist this execution path to memory:\n"
                "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                "</tmem_milestone_reached>"
            )

        # 3. Silent Recall: Only run on a new user turn OR when a new tool error occurred
        recent_errors: List[str] = list(state.get("recent_errors", []))
        recent_files: List[str] = list(state.get("recent_files", []))

        if is_new_user_turn or recent_errors:
            matched = store.cascading_lookup(
                query_text=last_input if is_new_user_turn else "",
                file_paths=recent_files,
                error_signatures=recent_errors,
                limit=2,
            )
            already_injected = set(state.get("injected_trace_ids", []))
            fresh_matches = [
                ev
                for ev in matched
                if ev.playbook.trace_id not in already_injected or recent_errors
            ]
            if fresh_matches:
                for ev in fresh_matches:
                    already_injected.add(ev.playbook.trace_id)
                state["injected_trace_ids"] = list(already_injected)
                state["recent_errors"] = []
                mem_block = format_injected_context(fresh_matches)
                if mem_block:
                    injected_blocks.append(mem_block)

        _save_runtime_state(workspace_root, state)

        if not injected_blocks:
            return {}

        return {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": "\n\n".join(injected_blocks),
                    }
                }
            ]
        }

    elif event == "post_tool_use":
        tool_call = payload.get("toolCall") or {}
        t_name = tool_call.get("name", "")
        args = tool_call.get("args") or {}
        err_str = payload.get("error") or ""

        if err_str:
            sig = extract_error_signature(err_str) or err_str[:120]
            if sig not in state["recent_errors"]:
                state["recent_errors"].append(sig)

        raw_path = args.get("Path") or args.get("TargetFile")
        rel_path = normalize_rel_path(raw_path, workspace_root)
        if rel_path and not rel_path.startswith(".tmem/"):
            if rel_path not in state["recent_files"]:
                state["recent_files"] = (state["recent_files"] + [rel_path])[-5:]
            if t_name in ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
                if rel_path not in state["uncommitted_mutations"]:
                    state["uncommitted_mutations"].append(rel_path)
                    state["verified_since_mutation"] = False

        # Detect verification pass after file mutations
        if t_name == "run_command" and not err_str and state.get("uncommitted_mutations"):
            cmd_line = str(args.get("CommandLine") or "")
            if any(
                k in cmd_line
                for k in ("test", "pytest", "unittest", "cargo", "go test", "npm", "pnpm", "make", "tsc", "lint")
            ) and "tmem.cli" not in cmd_line:
                state["verified_since_mutation"] = True

        _save_runtime_state(workspace_root, state)
        return {}

    elif event == "stop":
        # Do not blindly auto-compile on every conversational Stop;
        # memory persistence is triggered by `/tmem`, agent milestone confirmation, or `git post-commit`.
        return {"decision": "allow"}

    return {}


def handle_dsh_hook(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute invisible DeepSeek Harness (`@deepseek-ai/dsh`) lifecycle hook:
    - `pre_invocation` / `pre_step`: Recall `<tmem_speculative_memory>` (L0/L1/L2) or milestone notice.
    - `post_tool_use`: Track `read`/`write`/`edit`/`str_replace_editor`/`apply_patch` and `bash`/`pwsh`
      exit codes; return immediate L2 error repair hints or `<tmem_milestone_reached>` via `additionalContext`.
    - `command`: Native `/tmem [project|global|both|status|gc] [intent]` command execution in `dsh-commands`.
    """
    workspace_root = str(
        payload.get("workspaceRoot")
        or (payload.get("workspacePaths") or [os.getcwd()])[0]
        or os.getcwd()
    )
    transcript_path = payload.get("transcriptPath") or ""
    session_id = str(payload.get("sessionId") or payload.get("conversationId") or "default")

    store = DualTierMemoryStore(workspace_root)
    state = _load_runtime_state(workspace_root, session_id)

    if event == "command":
        raw_args = str(payload.get("args") or "").strip()
        cmd_str = f"/tmem {raw_args}".strip()
        slash_cmd = parse_slash_tmem_command(cmd_str)
        action_mode, custom_intent = slash_cmd if slash_cmd else ("project", raw_args)

        if action_mode == "status":
            proj_cnt = len(list(store.project_traces_dir.glob("*.json"))) if store.project_traces_dir.exists() else 0
            return {
                "kind": "success",
                "text": (
                    f"[tmem status]\n"
                    f"- Project Tier ({store.project_dir}): {proj_cnt} trace(s)"
                ),
            }

        if action_mode == "gc":
            stats = store.run_metabolism_gc()
            return {
                "kind": "success",
                "text": f"[tmem gc] Guard metabolism completed: {json.dumps(stats, ensure_ascii=False)}",
            }

        if action_mode == "list":
            items = store.list_playbooks()
            return {
                "kind": "success",
                "text": f"[tmem list ({len(items)})]\n{json.dumps(items, indent=2, ensure_ascii=False)}",
            }

        if action_mode in ("bench", "evaluate"):
            from tmem.evaluate import BenchmarkEvaluator, format_markdown_report
            evaluator = BenchmarkEvaluator()
            summary = evaluator.run_evaluation()
            return {
                "kind": "success",
                "text": format_markdown_report(summary),
            }

        if action_mode == "forget":
            if not custom_intent.strip():
                return {
                    "kind": "error",
                    "text": "Usage: /tmem forget <trace_id>",
                }
            del_res = store.delete_playbook(custom_intent)
            return {
                "kind": "success",
                "text": f"[tmem forget] {json.dumps(del_res, ensure_ascii=False)}",
            }

        if action_mode == "inspect":
            parts = (custom_intent or "").split()
            tid = parts[0] if parts else ""
            level = parts[1] if len(parts) > 1 else "playbook"
            target_pb = None
            for pb in store.get_all_project_playbooks():
                if pb.trace_id == tid or pb.trace_id.startswith(tid):
                    target_pb = pb
                    break
            if not target_pb:
                return {"kind": "error", "text": f"Trace '{tid}' not found."}
            raw_tr = store.get_raw_trace(target_pb.trace_id)
            view_text = format_progressive_view(target_pb, raw_tr, level=level)
            return {"kind": "success", "text": view_text}

        if action_mode == "replay":
            tid = (custom_intent or "").strip()
            raw_tr = store.get_raw_trace(tid)
            if not raw_tr:
                return {"kind": "error", "text": f"Raw trace for '{tid}' not found in .tmem/raw/."}
            replay_text = format_raw_trace_replay(raw_tr)
            return {"kind": "success", "text": replay_text}

        if action_mode == "diff":
            tid = (custom_intent or "").strip()
            raw_tr = store.get_raw_trace(tid)
            if not raw_tr:
                return {"kind": "error", "text": f"Raw trace for '{tid}' not found in .tmem/raw/."}
            diff_text = format_raw_trace_diff(raw_tr)
            return {"kind": "success", "text": f"```diff\n{diff_text}\n```"}

        if action_mode == "sync":
            store.sync_project_index_if_needed()
            store.run_metabolism_gc()
            doc_path = store.sync_memories_markdown()
            return {
                "kind": "success",
                "text": f"[tmem sync] Synchronized indexes & updated {doc_path.name} and MEM.md",
            }

        res = execute_capture(
            workspace_root=workspace_root,
            agent_name="dsh",
            transcript_path=transcript_path or None,
            scope="project",
            custom_intent=custom_intent or None,
            session_id=session_id if session_id != "default" else None,
        )
        return {
            "kind": "success",
            "text": (
                f"[tmem {action_mode}] Execution trace memory compiled:\n"
                f"{json.dumps(res, indent=2, ensure_ascii=False)}"
            ),
        }

    if event in ("pre_invocation", "pre_step"):
        last_input = (payload.get("lastUserInput") or "").strip()
        is_new_user_turn = bool(last_input) and last_input != state.get("last_user_input", "")
        if last_input:
            state["last_user_input"] = last_input

        slash_cmd = parse_slash_tmem_command(last_input)
        if slash_cmd and is_new_user_turn:
            action_mode, custom_intent = slash_cmd
            _save_runtime_state(workspace_root, state)
            cmd_out = handle_dsh_hook(
                "command",
                {
                    "workspaceRoot": workspace_root,
                    "sessionId": session_id,
                    "transcriptPath": transcript_path,
                    "args": f"{action_mode} {custom_intent}".strip(),
                },
            )
            return {
                "injectedContext": (
                    "<tmem_command_executed>\n"
                    f"{cmd_out.get('text', '')}\n"
                    "</tmem_command_executed>"
                )
            }

        injected_blocks: List[str] = []

        # Check pending milestone prompt if not already delivered in post_tool_use
        uncommitted = list(state.get("uncommitted_mutations", []))
        prompted_set = set(state.get("milestone_prompted_for_mutations", []))
        if (
            uncommitted
            and state.get("verified_since_mutation")
            and set(uncommitted) != prompted_set
        ):
            state["milestone_prompted_for_mutations"] = uncommitted
            injected_blocks.append(
                "<tmem_milestone_reached>\n"
                f"Milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded.\n"
                "When you finish summarizing your work to the user in this turn, add a brief, non-intrusive one-line prompt asking if they would like to persist this execution path to memory:\n"
                "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                "</tmem_milestone_reached>"
            )

        recent_errors: List[str] = list(state.get("recent_errors", []))
        recent_files: List[str] = list(state.get("recent_files", []))

        if is_new_user_turn or recent_errors:
            matched = store.cascading_lookup(
                query_text=last_input if is_new_user_turn else "",
                file_paths=recent_files,
                error_signatures=recent_errors,
                limit=2,
            )
            already_injected = set(state.get("injected_trace_ids", []))
            fresh_matches = [
                ev
                for ev in matched
                if ev.playbook.trace_id not in already_injected or recent_errors
            ]
            if fresh_matches:
                for ev in fresh_matches:
                    already_injected.add(ev.playbook.trace_id)
                state["injected_trace_ids"] = list(already_injected)
                state["recent_errors"] = []
                mem_block = format_injected_context(fresh_matches)
                if mem_block:
                    injected_blocks.append(mem_block)

        _save_runtime_state(workspace_root, state)
        if not injected_blocks:
            return {}
        return {"injectedContext": "\n\n".join(injected_blocks)}

    if event == "post_tool_use":
        tool_call = payload.get("toolCall") or {}
        t_name = str(tool_call.get("name") or "").lower()
        args = tool_call.get("args") or {}
        result_text = str(payload.get("resultText") or payload.get("error") or "")
        is_error = bool(payload.get("isError"))
        raw_exit = payload.get("exitCode")

        effective_exit: Optional[int] = None
        if isinstance(raw_exit, int):
            effective_exit = raw_exit
        else:
            m_exit = DSH_EXIT_CODE_RE.search(result_text)
            if m_exit:
                effective_exit = int(m_exit.group(1))
            elif is_error:
                effective_exit = 1
            elif t_name in ("bash", "pwsh", "run_command"):
                effective_exit = 0

        additional_blocks: List[str] = []

        # 1. Error tracking + immediate L2_INVARIANT lookup
        has_failure = is_error or (effective_exit is not None and effective_exit != 0)
        if has_failure and result_text:
            sig = extract_error_signature(result_text) or result_text.strip().splitlines()[-1][:120]
            if sig and sig not in state["recent_errors"]:
                state["recent_errors"].append(sig)
            matched = store.cascading_lookup(
                query_text="",
                file_paths=list(state.get("recent_files", [])),
                error_signatures=list(state.get("recent_errors", [])),
                limit=1,
            )
            if matched:
                mem_block = format_injected_context(matched)
                if mem_block:
                    for ev in matched:
                        if ev.playbook.trace_id not in state["injected_trace_ids"]:
                            state["injected_trace_ids"].append(ev.playbook.trace_id)
                    state["recent_errors"] = []
                    additional_blocks.append(mem_block)

        # 2. Track touched files across read / write / edit / str_replace_editor / apply_patch
        touched_paths: List[str] = []
        if t_name == "apply_patch":
            patch_input = str(args.get("input") or args.get("patch") or "")
            for p_raw, _ in parse_apply_patch_targets(patch_input):
                rel = normalize_rel_path(p_raw, workspace_root)
                if rel:
                    touched_paths.append(rel)
        else:
            raw_path = (
                args.get("file_path")
                or args.get("path")
                or args.get("Path")
                or args.get("TargetFile")
            )
            rel = normalize_rel_path(raw_path, workspace_root)
            if rel:
                touched_paths.append(rel)

        is_mutate_tool = t_name in (
            "write",
            "edit",
            "str_replace_editor",
            "apply_patch",
            "write_file",
            "edit_file",
        )
        for rel_path in touched_paths:
            if rel_path.startswith(".tmem/"):
                continue
            if rel_path not in state["recent_files"]:
                state["recent_files"] = (state["recent_files"] + [rel_path])[-5:]
            if is_mutate_tool and not has_failure:
                if rel_path not in state["uncommitted_mutations"]:
                    state["uncommitted_mutations"].append(rel_path)
                    state["verified_since_mutation"] = False

        # 3. Detect `MUTATE -> VERIFY(exit 0)` milestone in dsh `bash` / `pwsh`
        if (
            t_name in ("bash", "pwsh", "run_command")
            and effective_exit == 0
            and not is_error
            and state.get("uncommitted_mutations")
        ):
            cmd_line = str(args.get("command") or args.get("CommandLine") or "")
            if (
                any(
                    k in cmd_line
                    for k in (
                        "test",
                        "pytest",
                        "unittest",
                        "cargo",
                        "go test",
                        "npm",
                        "pnpm",
                        "make",
                        "tsc",
                        "lint",
                    )
                )
                and "tmem.cli" not in cmd_line
            ):
                state["verified_since_mutation"] = True
                uncommitted = list(state.get("uncommitted_mutations", []))
                prompted_set = set(state.get("milestone_prompted_for_mutations", []))
                if set(uncommitted) != prompted_set:
                    state["milestone_prompted_for_mutations"] = uncommitted
                    additional_blocks.append(
                        "<tmem_milestone_reached>\n"
                        f"Milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded.\n"
                        "When you finish summarizing your work to the user in this turn, add a brief, non-intrusive one-line prompt asking if they would like to persist this execution path to memory:\n"
                        "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                        "</tmem_milestone_reached>"
                    )

        _save_runtime_state(workspace_root, state)
        if additional_blocks:
            return {"additionalContext": "\n\n".join(additional_blocks)}
        return {}

    return {}


def install_invisible_integration(
    workspace_root: str, agents: List[str], install_git_hook: bool = True
) -> Dict[str, Any]:
    """
    Set up invisible hooks, rules, and `/tmem` skill for target agents and Git post-commit.
    """
    ws = Path(workspace_root).resolve()
    store = DualTierMemoryStore(str(ws))
    store.ensure_initialized(init_project=True)

    installed: Dict[str, Any] = {"workspace": str(ws), "agents": [], "git_hook": False}
    cli_cmd = f"{sys.executable} -m tmem.cli"

    if "agy" in agents:
        plugin_dir = ws / ".agents" / "plugins" / "tmem"
        rules_dir = plugin_dir / "rules"
        skill_dir = plugin_dir / "skills" / "tmem"
        rules_dir.mkdir(parents=True, exist_ok=True)
        skill_dir.mkdir(parents=True, exist_ok=True)

        plugin_manifest = {
            "name": "tmem",
            "description": "Invisible Backward-Slicing Execution Trace Memory Compiler (.tmem/)",
        }
        (plugin_dir / "plugin.json").write_text(
            json.dumps(plugin_manifest, indent=2), encoding="utf-8"
        )

        hooks_cfg = {
            "tmem-invisible-memory": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f"PYTHONPATH={ws} {cli_cmd} hook agy pre_invocation",
                        "timeout": 5,
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"PYTHONPATH={ws} {cli_cmd} hook agy post_tool_use",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
            }
        }
        (plugin_dir / "hooks.json").write_text(
            json.dumps(hooks_cfg, indent=2), encoding="utf-8"
        )

        rules_md = (
            "# tmem Speculative Execution Memory & Milestone Protocol\n\n"
            "## 1. Consuming Recalled Memory (`<tmem_speculative_memory>`) & `MEMORIES.md`\n"
            "- **Living Memory Index (`MEMORIES.md`)**: When navigating or planning modifications in this repository, consult `MEMORIES.md` (or `MEM.md`) for verified execution paths, co-change rules, and known error repairs.\n"
            "- **L0_FULL (Guards Passed)**: Follow the verified `L0 Critical Path` directly instead of performing redundant exploratory `grep`/`find` scans.\n"
            "- **L1_TOPOLOGY**: Respect the co-change file contract and run the specified verification command before finishing.\n"
            "- **L2_INVARIANT**: When encountering a matching error signature, apply the known causal repair action first.\n"
            "- **Progressive Disclosure**: Detailed playbooks are in `.tmem/traces/*.json`; full forensic step replay is in `.tmem/raw/*.raw.json` (`python3 -m tmem.cli inspect <id> --level raw`).\n\n"
            "## 2. Goal Completion & `/tmem` Memory Persistence\n"
            "- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:\n"
            "  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
            "- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:\n"
            f"  `PYTHONPATH={ws} {cli_cmd} capture --workspace {ws}`\n"
        )
        (rules_dir / "AGENTS.md").write_text(rules_md, encoding="utf-8")

        skill_md = (
            "---\n"
            "name: tmem\n"
            "description: >-\n"
            "  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.\n"
            "  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.\n"
            "---\n\n"
            "# tmem Execution Trace Memory Skill\n\n"
            "## Commands\n\n"
            "1. **Save Memory Trace**:\n"
            f"   `PYTHONPATH={ws} {cli_cmd} capture --workspace {ws}`\n"
            "2. **Check Memory Status**:\n"
            f"   `PYTHONPATH={ws} {cli_cmd} status --workspace {ws}`\n"
        )
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        installed["agents"].append("agy (.agents/plugins/tmem: hooks + rules + /tmem skill)")

    if "dsh" in agents:
        tmem_pkg_root = Path(__file__).resolve().parent.parent
        plugin_src = Path(__file__).resolve().parent / "dsh_plugin.mjs"
        dsh_plugin_dir = ws / ".dsh" / "plugins"
        dsh_plugin_dir.mkdir(parents=True, exist_ok=True)
        dsh_plugin_dst = dsh_plugin_dir / "tmem-dsh-plugin.mjs"
        if plugin_src.exists():
            dsh_plugin_dst.write_text(plugin_src.read_text(encoding="utf-8"), encoding="utf-8")

        patch_yaml = (
            "# Generated by tmem init for DeepSeek Harness (@deepseek-ai/dsh)\n"
            "- insert:\n"
            "    - id: tmem\n"
            f"      name: {dsh_plugin_dst}\n"
            "      config:\n"
            f"        workspaceRoot: {ws}\n"
            f"        tmemRoot: {tmem_pkg_root}\n"
            f"        pythonBin: {sys.executable}\n"
        )
        ws_patch_path = ws / ".dsh" / "tmem.patch.yml"
        ws_patch_path.write_text(patch_yaml, encoding="utf-8")

        # Also install into $DSH_HOME/cordis.patch.yml so dsh loads tmem by default
        dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
        try:
            dsh_home.mkdir(parents=True, exist_ok=True)
            home_patch = dsh_home / "cordis.patch.yml"
            if home_patch.exists():
                existing_patch = home_patch.read_text(encoding="utf-8")
                if "id: tmem" not in existing_patch and "tmem-dsh-plugin.mjs" not in existing_patch:
                    home_patch.write_text(
                        existing_patch.rstrip() + "\n" + patch_yaml, encoding="utf-8"
                    )
            else:
                home_patch.write_text(patch_yaml, encoding="utf-8")
        except Exception:
            pass

        # Ensure dsh-skill-filesystem (.agents/skills/tmem/SKILL.md) and dsh-agent-instructions (AGENTS.md)
        dsh_skill_dir = ws / ".agents" / "skills" / "tmem"
        dsh_skill_dir.mkdir(parents=True, exist_ok=True)
        dsh_skill_md = (
            "---\n"
            "name: tmem\n"
            "description: >-\n"
            "  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.\n"
            "  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.\n"
            "---\n\n"
            "# tmem Execution Trace Memory Skill (dsh & Multi-Agent)\n\n"
            "## Commands\n\n"
            "1. **Save Memory Trace**:\n"
            f"   `PYTHONPATH={tmem_pkg_root} {cli_cmd} capture --agent dsh --workspace {ws}`\n"
            "2. **Check Memory Status**:\n"
            f"   `PYTHONPATH={tmem_pkg_root} {cli_cmd} status --workspace {ws}`\n"
        )
        (dsh_skill_dir / "SKILL.md").write_text(dsh_skill_md, encoding="utf-8")

        ws_agents_md = ws / "AGENTS.md"
        dsh_rules_section = (
            "# tmem Speculative Execution Memory & Milestone Protocol\n\n"
            "## 1. Consuming Recalled Memory (`<tmem_speculative_memory>`) & `MEMORIES.md`\n"
            "- **Living Memory Index (`MEMORIES.md`)**: When navigating or planning modifications in this repository, consult `MEMORIES.md` (or `MEM.md`) for verified execution paths, co-change rules, and known error repairs.\n"
            "- **L0_FULL (Guards Passed)**: Follow the verified `L0 Critical Path` directly instead of performing redundant exploratory `grep`/`find` scans.\n"
            "- **L1_TOPOLOGY**: Respect the co-change file contract and run the specified verification command before finishing.\n"
            "- **L2_INVARIANT**: When encountering a matching error signature, apply the known causal repair action first.\n"
            "- **Progressive Disclosure**: Detailed playbooks are in `.tmem/traces/*.json`; full forensic step replay is in `.tmem/raw/*.raw.json` (`python3 -m tmem.cli inspect <id> --level raw`).\n\n"
            "## 2. Goal Completion & `/tmem` Memory Persistence\n"
            "- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:\n"
            "  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
            "- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:\n"
            f"  `PYTHONPATH={tmem_pkg_root} {cli_cmd} capture --agent dsh --workspace {ws}`\n"
        )
        if ws_agents_md.exists():
            cur_agents = ws_agents_md.read_text(encoding="utf-8")
            if "tmem Speculative Execution Memory" not in cur_agents:
                ws_agents_md.write_text(
                    cur_agents.rstrip() + "\n\n" + dsh_rules_section, encoding="utf-8"
                )
        else:
            ws_agents_md.write_text(dsh_rules_section, encoding="utf-8")

        installed["agents"].append(
            "dsh (.dsh/plugins/tmem-dsh-plugin.mjs + .dsh/tmem.patch.yml + ~/.dsh/cordis.patch.yml + .agents/skills/tmem + AGENTS.md)"
        )

    # Initialize store, raw archive, and living MEMORIES.md index
    store = DualTierMemoryStore(str(ws))
    store.ensure_initialized(init_project=True)
    mem_file = store.sync_memories_markdown()
    installed["memories_doc"] = str(mem_file)

    if install_git_hook and (ws / ".git").exists():
        hooks_dir = ws / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        post_commit = hooks_dir / "post-commit"
        hook_line = f"PYTHONPATH={ws} {cli_cmd} capture --scope auto --quiet || true\n"
        if post_commit.exists():
            existing = post_commit.read_text(encoding="utf-8")
            if "tmem.cli capture" not in existing:
                post_commit.write_text(existing.rstrip() + "\n" + hook_line, encoding="utf-8")
        else:
            post_commit.write_text("#!/bin/sh\n" + hook_line, encoding="utf-8")
        post_commit.chmod(post_commit.stat().st_mode | stat.S_IEXEC)
        installed["git_hook"] = True

    return installed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tmem",
        description="Invisible Agent Execution Trace Compiler & Dual-Tier Memory CLI",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # 1. init
    p_init = sub.add_parser("init", help="Initialize .tmem/, ~/.tmem/, and invisible agent/git hooks")
    p_init.add_argument("--workspace", default=os.getcwd(), help="Project workspace root")
    p_init.add_argument(
        "--agents",
        default="agy,codex,opencode,pi,dsh",
        help="Comma-separated agent targets (default: agy,codex,opencode,pi,dsh)",
    )

    # 2. hook
    p_hook = sub.add_parser("hook", help="Internal lifecycle hook entrypoint (stdin/stdout JSON)")
    p_hook.add_argument("agent", choices=["agy", "codex", "opencode", "pi", "dsh"])
    p_hook.add_argument(
        "event",
        choices=["pre_invocation", "pre_step", "post_tool_use", "command", "stop"],
    )

    # 3. capture
    p_cap = sub.add_parser("capture", help="Compile a session transcript + git diff into .tmem/ and/or ~/.tmem/")
    p_cap.add_argument(
        "--agent",
        default="agy",
        choices=["agy", "codex", "opencode", "pi", "dsh", "swe_bench", "swe_agent", "atif"],
    )
    p_cap.add_argument(
        "--scope",
        default="project",
        choices=["project", "global", "both", "auto"],
        help="Target memory scope (defaults to project repository .tmem/)",
    )
    p_cap.add_argument("--intent", default=None, help="Optional custom problem intent summary")
    p_cap.add_argument("--transcript", default=None, help="Explicit path to transcript.jsonl")
    p_cap.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_cap.add_argument("--full-session", action="store_true", help="Slice entire session instead of incremental steps")
    p_cap.add_argument("--quiet", action="store_true", help="Suppress stdout output (for git hooks)")

    # 4. lookup
    p_look = sub.add_parser("lookup", help="Query Project (.tmem/) execution traces with guard evaluation")
    p_look.add_argument("query", nargs="?", default="", help="Problem intent or error query")
    p_look.add_argument("--file", action="append", default=[], help="Touched file path trigger")
    p_look.add_argument("--error", action="append", default=[], help="Error signature trigger")
    p_look.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_look.add_argument("--format", choices=["prompt", "json"], default="prompt")

    # 5. gc
    p_gc = sub.add_parser("gc", help="Run guard metabolism check & mark drifted L0 traces")
    p_gc.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 6. status
    p_stat = sub.add_parser("status", help="Show Project trace memory counts")
    p_stat.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 7. list
    p_list = sub.add_parser("list", help="List stored traces with live guard evaluation tier")
    p_list.add_argument("--scope", default="project", help="Memory scope (project)")
    p_list.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 8. forget
    p_forget = sub.add_parser("forget", help="Delete a trace by ID or prefix from project store")
    p_forget.add_argument("trace_id", help="Trace ID or prefix (e.g. tr_acd0dc0ff0)")
    p_forget.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 9. evaluate
    p_eval = sub.add_parser("evaluate", help="Run benchmark evaluation on public/local agent trajectories")
    p_eval.add_argument("--data-dir", default=None, help="Directory containing .traj / .atif.json / .jsonl files")
    p_eval.add_argument("--json", action="store_true", help="Output raw JSON summary")
    p_eval.add_argument("--quiet", action="store_true", help="Suppress output")

    # 10. inspect
    p_insp = sub.add_parser("inspect", help="Progressive disclosure: inspect at index, playbook, raw, or diff level")
    p_insp.add_argument("trace_id", help="Trace ID or prefix (e.g. tr_acd0dc0ff0)")
    p_insp.add_argument("--level", choices=["index", "playbook", "raw", "diff"], default="playbook", help="Disclosure level")
    p_insp.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_insp.add_argument("--json", action="store_true", help="Output raw JSON")

    # 11. replay
    p_rep = sub.add_parser("replay", help="Forensic chronological terminal replay of raw execution trace")
    p_rep.add_argument("trace_id", help="Trace ID or prefix")
    p_rep.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 12. diff
    p_diff = sub.add_parser("diff", help="View recorded unified git diff patch for a trace")
    p_diff.add_argument("trace_id", help="Trace ID or prefix")
    p_diff.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 13. sync
    p_sync = sub.add_parser("sync", help="Synchronize indexes, run GC, and regenerate MEMORIES.md living index")
    p_sync.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    args = parser.parse_args(argv)

    if args.subcommand == "init":
        agent_list = [a.strip() for a in args.agents.split(",") if a.strip()]
        info = install_invisible_integration(args.workspace, agent_list)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "hook":
        payload = _read_stdin_json()
        if args.agent == "agy":
            out = handle_agy_hook(args.event, payload)
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if args.agent == "dsh":
            out = handle_dsh_hook(args.event, payload)
            print(json.dumps(out, ensure_ascii=False))
            return 0
        print("{}")
        return 0

    if args.subcommand == "capture":
        res = execute_capture(
            workspace_root=args.workspace,
            agent_name=args.agent,
            transcript_path=args.transcript,
            scope=args.scope,
            custom_intent=args.intent,
            full_session=args.full_session,
        )
        if not args.quiet:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "lookup":
        store = DualTierMemoryStore(args.workspace)
        evaluated = store.cascading_lookup(
            query_text=args.query,
            file_paths=args.file,
            error_signatures=args.error,
        )
        if args.format == "json":
            print(
                json.dumps(
                    [
                        {
                            "trace_id": ev.playbook.trace_id,
                            "scope": ev.playbook.scope,
                            "effective_tier": ev.effective_tier,
                            "l0_valid": ev.l0_valid,
                            "failed_guards": ev.failed_guards,
                            "playbook": ev.playbook.to_dict(),
                        }
                        for ev in evaluated
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(format_injected_context(evaluated))
        return 0

    if args.subcommand == "gc":
        store = DualTierMemoryStore(args.workspace)
        stats = store.run_metabolism_gc()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "status":
        store = DualTierMemoryStore(args.workspace)
        proj_count = len(list(store.project_traces_dir.glob("*.json"))) if store.project_traces_dir.exists() else 0
        print(
            json.dumps(
                {
                    "project_store": str(store.project_dir),
                    "project_traces": proj_count,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.subcommand == "list":
        store = DualTierMemoryStore(args.workspace)
        items = store.list_playbooks(scope=args.scope)
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "forget":
        store = DualTierMemoryStore(args.workspace)
        res = store.delete_playbook(args.trace_id)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "evaluate":
        from tmem.evaluate import BenchmarkEvaluator, format_markdown_report
        evaluator = BenchmarkEvaluator(args.data_dir)
        summary = evaluator.run_evaluation()
        if args.json:
            from dataclasses import asdict
            print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
        elif not args.quiet:
            print(format_markdown_report(summary))
        return 0 if summary.all_passed else 1

    if args.subcommand == "inspect":
        store = DualTierMemoryStore(args.workspace)
        tid = args.trace_id.strip()
        target_pb = None
        for pb in store.get_all_project_playbooks():
            if pb.trace_id == tid or pb.trace_id.startswith(tid):
                target_pb = pb
                break
        if not target_pb:
            print(f"Error: Trace '{tid}' not found in project memory.", file=sys.stderr)
            return 1
        raw_tr = store.get_raw_trace(target_pb.trace_id)
        if args.json:
            out_data = {
                "playbook": target_pb.to_dict(),
                "raw_trace": raw_tr.to_dict() if raw_tr else None,
            }
            print(json.dumps(out_data, indent=2, ensure_ascii=False))
        else:
            print(format_progressive_view(target_pb, raw_tr, level=args.level))
        return 0

    if args.subcommand == "replay":
        store = DualTierMemoryStore(args.workspace)
        raw_tr = store.get_raw_trace(args.trace_id)
        if not raw_tr:
            print(f"Error: Raw execution trace for '{args.trace_id}' not found in .tmem/raw/.", file=sys.stderr)
            return 1
        print(format_raw_trace_replay(raw_tr))
        return 0

    if args.subcommand == "diff":
        store = DualTierMemoryStore(args.workspace)
        raw_tr = store.get_raw_trace(args.trace_id)
        if not raw_tr:
            print(f"Error: Raw execution trace for '{args.trace_id}' not found in .tmem/raw/.", file=sys.stderr)
            return 1
        print(format_raw_trace_diff(raw_tr))
        return 0

    if args.subcommand == "sync":
        store = DualTierMemoryStore(args.workspace)
        store.sync_project_index_if_needed()
        gc_stats = store.run_metabolism_gc()
        doc_path = store.sync_memories_markdown()
        print(
            json.dumps(
                {
                    "status": "synchronized",
                    "gc_stats": gc_stats,
                    "living_index": str(doc_path),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
