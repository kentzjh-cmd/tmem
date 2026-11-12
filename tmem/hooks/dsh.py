"""DeepSeek Harness (@deepseek-ai/dsh) Cordis hook handler for tmem."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from tmem.adapters.base import extract_error_signature, is_code_file, normalize_rel_path
from tmem.adapters.dsh import DSH_EXIT_CODE_RE, parse_apply_patch_targets
from tmem.guards import format_injected_context
from tmem.hooks.agy import _parse_inspect_args, parse_slash_tmem_command
from tmem.hooks.state import load_runtime_state, save_runtime_state
from tmem.raw_storage import (
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.store import DualTierMemoryStore


def handle_dsh_hook(
    event: str,
    payload: Dict[str, Any],
    capture_fn=None,
) -> Dict[str, Any]:
    """
    Execute invisible DeepSeek Harness (`@deepseek-ai/dsh`) lifecycle hook:
    - `pre_invocation` / `pre_step`: Recall `<tmem_speculative_memory>` (L0/L1/L2) or milestone notice.
    - `post_tool_use`: Track `read`/`write`/`edit`/`str_replace_editor`/`apply_patch` and `bash`/`pwsh`
      exit codes; return immediate L2 error repair hints or `<tmem_milestone_reached>` via `additionalContext`.
    - `command`: Native `/tmem [status|list|gc|...] [intent]` command execution in `dsh-commands`.
    """
    workspace_root = str(
        payload.get("workspaceRoot")
        or (payload.get("workspacePaths") or [os.getcwd()])[0]
        or os.getcwd()
    )
    session_id = str(payload.get("sessionId") or payload.get("conversationId") or "default")

    store = DualTierMemoryStore(workspace_root)
    state = load_runtime_state(workspace_root, session_id)

    if event == "command":
        raw_args = str(payload.get("args") or "").strip()
        cmd_str = f"/tmem {raw_args}".strip()
        slash_cmd = parse_slash_tmem_command(cmd_str)
        action_mode, custom_intent = slash_cmd if slash_cmd else ("project", raw_args)

        if action_mode == "status":
            proj_cnt = (
                len(list(store.project_traces_dir.glob("*.json")))
                if store.project_traces_dir.exists()
                else 0
            )
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
            tid, level = _parse_inspect_args(custom_intent)
            target_pb = None
            if tid:
                for pb in store.get_all_project_playbooks():
                    if pb.trace_id == tid or pb.trace_id.startswith(tid):
                        target_pb = pb
                        break
            if not target_pb:
                return {
                    "kind": "error",
                    "text": f"[tmem inspect] Trace '{tid}' not found in project memory.",
                }
            raw_tr = store.get_raw_trace(target_pb.trace_id)
            rendered = format_progressive_view(target_pb, raw_tr, level=level)
            return {
                "kind": "success",
                "text": f"[tmem inspect {target_pb.trace_id} level={level}]\n{rendered}",
            }

        if action_mode == "replay":
            tid = custom_intent.strip()
            raw_tr = store.get_raw_trace(tid)
            if not raw_tr:
                return {
                    "kind": "error",
                    "text": f"[tmem replay] Raw execution trace for '{tid}' not found in .tmem/raw/.",
                }
            return {
                "kind": "success",
                "text": f"[tmem replay {tid}]\n{format_raw_trace_replay(raw_tr)}",
            }

        if action_mode == "diff":
            tid = custom_intent.strip()
            raw_tr = store.get_raw_trace(tid)
            if not raw_tr:
                return {
                    "kind": "error",
                    "text": f"[tmem diff] Raw execution trace for '{tid}' not found in .tmem/raw/.",
                }
            return {
                "kind": "success",
                "text": f"[tmem diff {tid}]\n{format_raw_trace_diff(raw_tr)}",
            }

        if action_mode == "sync":
            store.sync_project_index_if_needed()
            gc_stats = store.run_metabolism_gc()
            doc_path = store.sync_memories_markdown()
            return {
                "kind": "success",
                "text": f"[tmem sync] Synchronized indexes & generated `{doc_path.name}`. GC: {json.dumps(gc_stats)}",
            }

        # Fallback: capture
        if capture_fn:
            res = capture_fn(
                workspace_root=workspace_root,
                agent_name="dsh",
                scope="project",
                custom_intent=custom_intent or None,
                session_id=session_id,
            )
        else:
            from tmem.cli import execute_capture

            res = execute_capture(
                workspace_root=workspace_root,
                agent_name="dsh",
                scope="project",
                custom_intent=custom_intent or None,
                session_id=session_id,
            )
        status_val = res.get("status")
        return {
            "kind": "success" if status_val == "compiled" else ("info" if status_val == "skipped_trivial" else "error"),
            "text": json.dumps(res, indent=2, ensure_ascii=False),
        }

    elif event in ("pre_step", "pre_invocation"):
        user_message = str(payload.get("userMessage") or payload.get("lastUserInput") or "").strip()
        last_turn = int(payload.get("turnIndex", -1))

        is_new_user_turn = (
            last_turn > state.get("last_turn_idx", -1)
            or (user_message and user_message != state.get("last_user_input", ""))
        )
        if last_turn >= 0:
            state["last_turn_idx"] = last_turn
        if user_message:
            state["last_user_input"] = user_message

        # Milestone check
        uncommitted = list(state.get("uncommitted_mutations", []))
        prompted_set = set(state.get("milestone_prompted_for_mutations", []))
        has_code_file = any(is_code_file(f) for f in uncommitted)

        if (
            uncommitted
            and state.get("verified_since_mutation")
            and set(uncommitted) != prompted_set
            and has_code_file
        ):
            state["milestone_prompted_for_mutations"] = uncommitted
            save_runtime_state(workspace_root, state)
            return {
                "injectedContext": (
                    "<tmem_milestone_reached>\n"
                    f"Goal milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded.\n"
                    "When concluding this turn, append a concise one-line prompt asking whether to persist the path to memory:\n"
                    "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                    "</tmem_milestone_reached>"
                )
            }

        recent_errors = list(state.get("recent_errors", []))
        recent_files = list(state.get("recent_files", []))

        if is_new_user_turn or recent_errors:
            matched = store.cascading_lookup(
                query_text=user_message if is_new_user_turn else "",
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
                save_runtime_state(workspace_root, state)
                mem_block = format_injected_context(fresh_matches)
                return {"injectedContext": mem_block}

        save_runtime_state(workspace_root, state)
        return {}

    elif event in ("post_tool_use", "post_execute"):
        tool_call = payload.get("toolCall") or payload.get("tool") or {}
        t_name = str(tool_call.get("name") or payload.get("toolName") or "")
        args = tool_call.get("args") or payload.get("args") or {}
        result_payload = payload.get("result") or {}
        result_text = str(
            payload.get("resultText")
            or payload.get("output")
            or result_payload.get("text")
            or result_payload.get("output")
            or payload.get("error")
            or ""
        )
        is_error = bool(payload.get("error") or result_payload.get("error"))
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

        # Error tracking + immediate L2_INVARIANT lookup
        has_failure = is_error or (effective_exit is not None and effective_exit != 0)
        if has_failure and result_text:
            sig = extract_error_signature(result_text) or result_text.strip().splitlines()[-1][:120]
            if sig:
                if sig not in state["recent_errors"]:
                    state["recent_errors"].append(sig)
                if sig not in state["session_errors"]:
                    state["session_errors"].append(sig)
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

        # Track touched files
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

        # Detect verification pass in bash / pwsh / run_command
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
                session_errors = list(state.get("session_errors", []))
                has_code_file = any(is_code_file(f) for f in uncommitted)
                if set(uncommitted) != prompted_set and has_code_file:
                    state["milestone_prompted_for_mutations"] = uncommitted
                    additional_blocks.append(
                        "<tmem_milestone_reached>\n"
                        f"Milestone detected: {len(uncommitted)} file(s) modified and verification succeeded (`{cmd_line[:60]}`).\n"
                        "When concluding this turn, append a concise one-line prompt asking whether to persist the path to memory:\n"
                        "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                        "</tmem_milestone_reached>"
                    )

        save_runtime_state(workspace_root, state)
        if additional_blocks:
            return {"additionalContext": "\n\n".join(additional_blocks)}
        return {}

    return {}
