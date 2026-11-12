"""Antigravity (agy) lifecycle hook handler for tmem."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tmem.adapters.base import extract_error_signature, is_code_file, normalize_rel_path
from tmem.guards import format_injected_context
from tmem.hooks.state import load_runtime_state, save_runtime_state
from tmem.raw_storage import (
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.store import DualTierMemoryStore

SLASH_TMEM_RE = re.compile(
    r"^/(?:tmem|remember|trace)(?:\s+(project|status|list|forget|gc|bench|evaluate|inspect|replay|diff|sync))?(?:\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def parse_slash_tmem_command(user_input: str) -> Optional[Tuple[str, str]]:
    m = SLASH_TMEM_RE.match(user_input.strip())
    if not m:
        return None
    action_mode = (m.group(1) or "project").lower()
    custom_intent = (m.group(2) or "").strip()
    return action_mode, custom_intent


def _parse_inspect_args(raw: str) -> Tuple[str, str]:
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


def handle_agy_hook(
    event: str,
    payload: Dict[str, Any],
    capture_fn=None,
) -> Dict[str, Any]:
    """Execute invisible agy lifecycle hook (`pre_invocation`, `post_tool_use`, `stop`)."""
    ws_list = payload.get("workspacePaths") or [os.getcwd()]
    workspace_root = ws_list[0] if ws_list else os.getcwd()
    conv_id = payload.get("conversationId") or "default"

    store = DualTierMemoryStore(workspace_root)
    state = load_runtime_state(workspace_root, conv_id)

    if event == "pre_invocation":
        last_input = (payload.get("lastUserInput") or "").strip()
        is_new_user_turn = last_input != state.get("last_user_input", "")
        state["last_user_input"] = last_input

        # 1. Check if the user invoked `/tmem [project|status|gc|list|...]`
        slash_cmd = parse_slash_tmem_command(last_input)
        if slash_cmd and is_new_user_turn:
            action_mode, custom_intent = slash_cmd
            save_runtime_state(workspace_root, state)

            if action_mode == "status":
                proj_cnt = (
                    len(list(store.project_traces_dir.glob("*.json")))
                    if store.project_traces_dir.exists()
                    else 0
                )
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
                if not custom_intent.strip():
                    msg = "<tmem_command_executed>\nError: Usage `/tmem forget <trace_id>`\n</tmem_command_executed>"
                else:
                    del_res = store.delete_playbook(custom_intent)
                    msg = (
                        "<tmem_command_executed>\n"
                        f"Deleted memory trace: {json.dumps(del_res, ensure_ascii=False)}\n"
                        "</tmem_command_executed>"
                    )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "inspect":
                tid, level = _parse_inspect_args(custom_intent)
                target_pb = None
                if tid:
                    for pb in store.get_all_project_playbooks():
                        if pb.trace_id == tid or pb.trace_id.startswith(tid):
                            target_pb = pb
                            break
                if not target_pb:
                    msg = (
                        "<tmem_command_executed>\n"
                        f"Error: Trace '{tid}' not found in project memory.\n"
                        "</tmem_command_executed>"
                    )
                else:
                    raw_tr = store.get_raw_trace(target_pb.trace_id)
                    rendered = format_progressive_view(target_pb, raw_tr, level=level)
                    msg = (
                        "<tmem_command_executed>\n"
                        f"### Forensic Trace Inspection: `{target_pb.trace_id}` (level={level})\n\n"
                        f"{rendered}\n"
                        "</tmem_command_executed>"
                    )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "replay":
                tid = custom_intent.strip()
                raw_tr = store.get_raw_trace(tid)
                if not raw_tr:
                    msg = (
                        "<tmem_command_executed>\n"
                        f"Error: Raw execution trace for '{tid}' not found in .tmem/raw/.\n"
                        "</tmem_command_executed>"
                    )
                else:
                    msg = (
                        "<tmem_command_executed>\n"
                        f"### Chronological Replay: `{tid}`\n\n"
                        f"{format_raw_trace_replay(raw_tr)}\n"
                        "</tmem_command_executed>"
                    )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "diff":
                tid = custom_intent.strip()
                raw_tr = store.get_raw_trace(tid)
                if not raw_tr:
                    msg = (
                        "<tmem_command_executed>\n"
                        f"Error: Raw execution trace for '{tid}' not found in .tmem/raw/.\n"
                        "</tmem_command_executed>"
                    )
                else:
                    msg = (
                        "<tmem_command_executed>\n"
                        f"### Recorded Git Unified Diff: `{tid}`\n\n"
                        f"{format_raw_trace_diff(raw_tr)}\n"
                        "</tmem_command_executed>"
                    )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            if action_mode == "sync":
                store.sync_project_index_if_needed()
                gc_stats = store.run_metabolism_gc()
                doc_path = store.sync_memories_markdown()
                msg = (
                    "<tmem_command_executed>\n"
                    f"Command `/tmem sync` completed successfully: synchronized indexes and generated living index (`{doc_path.name}`). GC stats: {json.dumps(gc_stats)}\n"
                    "</tmem_command_executed>"
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            # Fallback to capture
            if capture_fn:
                res = capture_fn(
                    workspace_root=workspace_root,
                    agent_name="agy",
                    scope="project",
                    custom_intent=custom_intent or None,
                )
            else:
                from tmem.cli import execute_capture

                res = execute_capture(
                    workspace_root=workspace_root,
                    agent_name="agy",
                    scope="project",
                    custom_intent=custom_intent or None,
                )
            msg = (
                "<tmem_command_executed>\n"
                f"User invoked `/tmem`. Trace compilation result:\n"
                f"{json.dumps(res, indent=2, ensure_ascii=False)}\n"
                "Please confirm the memory compilation status to the user.\n"
                "</tmem_command_executed>"
            )
            return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

        injected_blocks: List[str] = []

        # 2. Check if a Goal Milestone (`MUTATE` + `VERIFY exit 0`) was reached
        uncommitted = list(state.get("uncommitted_mutations", []))
        prompted_set = set(state.get("milestone_prompted_for_mutations", []))

        # Milestone threshold: Must involve code files (not pure docs/markdown) and verified
        has_code_file = any(is_code_file(f) for f in uncommitted)

        if (
            uncommitted
            and state.get("verified_since_mutation")
            and set(uncommitted) != prompted_set
            and has_code_file
        ):
            state["milestone_prompted_for_mutations"] = uncommitted
            injected_blocks.append(
                "<tmem_milestone_reached>\n"
                f"Milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded.\n"
                "When you finish summarizing your work to the user in this turn, append a concise one-line prompt asking if they would like to persist this execution path to memory:\n"
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

        save_runtime_state(workspace_root, state)

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
            if sig not in state["session_errors"]:
                state["session_errors"].append(sig)

        raw_path = args.get("Path") or args.get("TargetFile")
        rel_path = normalize_rel_path(raw_path, workspace_root)
        if rel_path and not rel_path.startswith(".tmem/"):
            if rel_path not in state["recent_files"]:
                state["recent_files"] = (state["recent_files"] + [rel_path])[-5:]
            if t_name in (
                "write_file",
                "edit_file",
                "replace_file_content",
                "multi_replace_file_content",
            ):
                if rel_path not in state["uncommitted_mutations"]:
                    state["uncommitted_mutations"].append(rel_path)
                    state["verified_since_mutation"] = False

        # Detect verification pass after file mutations
        if t_name == "run_command" and not err_str and state.get("uncommitted_mutations"):
            cmd_line = str(args.get("CommandLine") or "")
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

        save_runtime_state(workspace_root, state)
        return {}

    elif event == "stop":
        return {"decision": "allow"}

    return {}
