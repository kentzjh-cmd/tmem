"""DeepSeek Harness (@deepseek-ai/dsh) Cordis hook handler for tmem."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tmem.adapters.base import extract_error_signature, is_code_file, normalize_rel_path
from tmem.adapters.dsh import DSH_EXIT_CODE_RE, parse_apply_patch_targets
from tmem.cochange import find_cochange_partners_for_files
from tmem.guards import format_injected_context
from tmem.hooks.commands import _parse_inspect_args, parse_slash_tmem_command
from tmem.hooks.state import (
    apply_session_trace_feedback,
    load_runtime_state,
    record_tool_outcome_for_loop,
    save_runtime_state,
)
from tmem.mem_doc import extract_micro_skills, format_micro_skills_block
from tmem.raw_storage import (
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.slicer import _commands_are_same_origin
from tmem.store import DualTierMemoryStore
from tmem.telemetry import compute_telemetry_summary, record_telemetry_event

FILE_PATH_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yml|yaml|json|toml|sh))(?![A-Za-z0-9_.-])"
)
ERROR_NAME_MENTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Fault|Panic))\b")


def _extract_explicit_anchors_from_text(
    text: str, workspace_root: str
) -> Tuple[List[str], List[str]]:
    """
    Extract deterministic file-path and error-signature anchors from user input.
    Avoids loose natural-language BM25 guessing in pre-step recall.
    """
    if not text:
        return [], []
    ws = Path(workspace_root)
    files: List[str] = []
    for raw_fp in FILE_PATH_MENTION_RE.findall(text):
        rel = normalize_rel_path(raw_fp, workspace_root)
        if rel and ((ws / rel).exists() or "/" in rel) and rel not in files:
            files.append(rel)

    errors: List[str] = []
    sig = extract_error_signature(text)
    if sig:
        errors.append(sig)
    for err_tok in ERROR_NAME_MENTION_RE.findall(text):
        if err_tok not in errors and len(err_tok) >= 6:
            errors.append(err_tok)
    return files, errors


def handle_dsh_hook(
    event: str,
    payload: Dict[str, Any],
    capture_fn=None,
) -> Dict[str, Any]:
    """
    Execute DeepSeek Harness (`@deepseek-ai/dsh`) lifecycle hook:
    - `command`: Native `/tmem [status|list|gc|telemetry|inspect|replay|diff|sync|forget|bench]` in `dsh-commands`.
    - `pre_invocation` / `pre_step`: Session-start env facts, milestone notice, and high-anchor-only recall.
    - `post_tool_use`: Strong-signal runtime interception (L2 immediate error repair, L1 co-change hint on edit,
      failure-loop breaker, verification milestone, and deterministic telemetry resolution).
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
            tel = compute_telemetry_summary(workspace_root)
            return {
                "kind": "success",
                "text": (
                    f"[tmem status]\n"
                    f"- Project Tier ({store.project_dir}): {proj_cnt} trace(s)\n"
                    f"- Telemetry: L2 {tel['l2_injections']} hint(s) (resolved={tel['l2_resolved_green']}/{tel['l2_resolved_green'] + tel['l2_unresolved']}), "
                    f"L1 {tel['l1_hints']} hint(s) (adopted={tel['l1_adopted']}/{tel['l1_adopted'] + tel['l1_ignored']})"
                ),
            }

        if action_mode == "telemetry":
            tel = compute_telemetry_summary(workspace_root)
            return {
                "kind": "success",
                "text": f"[tmem telemetry]\n{json.dumps(tel, indent=2, ensure_ascii=False)}",
            }

        if action_mode == "gc":
            prune_stale = "--prune" in custom_intent
            stats = store.run_metabolism_gc(prune_stale=prune_stale)
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
                    if (
                        pb.trace_id == tid
                        or pb.trace_id.startswith(tid)
                        or any(s == tid or s.startswith(tid) for s in pb.source_session_ids)
                    ):
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

        pre_blocks: List[str] = []
        if is_new_user_turn and not state.get("micro_skills_injected"):
            skills = extract_micro_skills(
                store.get_all_project_playbooks(), workspace_root=workspace_root
            )
            skills_block = format_micro_skills_block(skills)
            if skills_block:
                pre_blocks.append(skills_block)
            state["micro_skills_injected"] = True

        recent_errors = list(state.get("recent_errors", []))
        recent_files = list(state.get("recent_files", []))

        # Strong-signal policy: only recall in pre-step when explicit file/error anchors exist
        # or unresolved recent errors are present (never noisy NL BM25 guessing).
        explicit_files, explicit_errors = _extract_explicit_anchors_from_text(
            user_message, workspace_root
        )
        anchor_files = list(dict.fromkeys(recent_files + explicit_files)) if (is_new_user_turn or recent_errors) else []
        anchor_errors = list(dict.fromkeys(recent_errors + explicit_errors)) if (is_new_user_turn or recent_errors) else []

        if anchor_errors or explicit_files:
            matched = store.cascading_lookup(
                query_text="",
                file_paths=anchor_files,
                error_signatures=anchor_errors,
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
                    pre_blocks.append(mem_block)

        save_runtime_state(workspace_root, state)
        if pre_blocks:
            return {"injectedContext": "\n\n".join(pre_blocks)}
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
        is_error = bool(payload.get("isError") or payload.get("error") or result_payload.get("error"))
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
        has_failure = is_error or (effective_exit is not None and effective_exit != 0)
        is_shell_tool = t_name in ("bash", "pwsh", "run_command")
        cmd_line = str(args.get("command") or args.get("CommandLine") or "") if is_shell_tool else ""

        sig = ""
        if has_failure and result_text:
            sig = extract_error_signature(result_text) or result_text.strip().splitlines()[-1][:120]

        # 1. Resolve pending L2 telemetry on subsequent same-origin shell execution
        pending_l2: List[Dict[str, str]] = list(state.get("pending_l2_hints") or [])
        if is_shell_tool and pending_l2 and cmd_line:
            remaining_l2: List[Dict[str, str]] = []
            for hint in pending_l2:
                h_cmd = hint.get("failed_command", "")
                h_sig = hint.get("error_signature", "")
                h_tid = hint.get("trace_id", "")
                same_origin = (not h_cmd) or _commands_are_same_origin(h_cmd, cmd_line)
                if not has_failure and same_origin:
                    record_telemetry_event(
                        workspace_root,
                        "l2_outcome",
                        {
                            "session_id": session_id,
                            "trace_id": h_tid,
                            "error_signature": h_sig,
                            "resolved": True,
                            "verify_command": cmd_line,
                        },
                    )
                    if h_tid:
                        store.record_trace_outcome([h_tid], "success")
                        rewarded = set(state.get("rewarded_trace_ids") or [])
                        rewarded.add(h_tid)
                        state["rewarded_trace_ids"] = list(rewarded)
                elif has_failure and sig and (sig.lower() in h_sig.lower() or h_sig.lower() in sig.lower()):
                    record_telemetry_event(
                        workspace_root,
                        "l2_outcome",
                        {
                            "session_id": session_id,
                            "trace_id": h_tid,
                            "error_signature": h_sig,
                            "resolved": False,
                            "verify_command": cmd_line,
                        },
                    )
                else:
                    remaining_l2.append(hint)
            state["pending_l2_hints"] = remaining_l2

        # 2. Immediate deterministic L2_INVARIANT lookup on failing command
        if has_failure and sig:
            if sig not in state["recent_errors"]:
                state["recent_errors"].append(sig)
            if sig not in state["session_errors"]:
                state["session_errors"].append(sig)

            matched = store.lookup_l2_repair(
                error_signatures=[sig],
                file_paths=list(state.get("recent_files", [])),
                limit=1,
            )
            if matched:
                mem_block = format_injected_context(matched)
                if mem_block:
                    for ev in matched:
                        tid = ev.playbook.trace_id
                        if tid not in state["injected_trace_ids"]:
                            state["injected_trace_ids"].append(tid)
                        if not any(h.get("trace_id") == tid and h.get("error_signature") == sig for h in state["pending_l2_hints"]):
                            state["pending_l2_hints"].append(
                                {
                                    "trace_id": tid,
                                    "error_signature": sig,
                                    "failed_command": cmd_line,
                                }
                            )
                            rep_act = ev.playbook.l2_causal_repairs[0].repair_action if ev.playbook.l2_causal_repairs else ""
                            record_telemetry_event(
                                workspace_root,
                                "l2_injected",
                                {
                                    "session_id": session_id,
                                    "trace_id": tid,
                                    "error_signature": sig,
                                    "failed_command": cmd_line,
                                    "repair_action": rep_act,
                                },
                            )
                    state["recent_errors"] = []
                    additional_blocks.append(mem_block)

        # 3. Track touched files & resolve/emit L1 co-change hints
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

        # Resolve pending L1 co-change hints when agent touches any suggested partner file
        pending_l1: List[Dict[str, Any]] = list(state.get("pending_l1_hints") or [])
        if touched_paths and pending_l1:
            touched_set = set(touched_paths)
            still_pending_l1: List[Dict[str, Any]] = []
            for hint in pending_l1:
                sugg = set(hint.get("suggested_files") or [])
                adopted = sorted(touched_set & sugg)
                if adopted:
                    record_telemetry_event(
                        workspace_root,
                        "l1_outcome",
                        {
                            "session_id": session_id,
                            "edited_file": hint.get("edited_file", ""),
                            "adopted_files": adopted,
                            "adopted": True,
                        },
                    )
                else:
                    still_pending_l1.append(hint)
            state["pending_l1_hints"] = still_pending_l1

        is_mutate_tool = t_name in (
            "write",
            "edit",
            "str_replace_editor",
            "apply_patch",
            "write_file",
            "edit_file",
        )
        mutated_this_step: List[str] = []
        for rel_path in touched_paths:
            if rel_path.startswith(".tmem/"):
                continue
            if rel_path not in state["recent_files"]:
                state["recent_files"] = (state["recent_files"] + [rel_path])[-5:]
            if is_mutate_tool and not has_failure:
                mutated_this_step.append(rel_path)
                if rel_path not in state["uncommitted_mutations"]:
                    state["uncommitted_mutations"].append(rel_path)
                    state["verified_since_mutation"] = False

        # Emit proactive L1 co-change hint when editing code files with known strong partners
        if mutated_this_step:
            code_mutated = [f for f in mutated_this_step if is_code_file(f)]
            if code_mutated:
                partners = find_cochange_partners_for_files(
                    workspace_root,
                    code_mutated,
                    playbooks=store.get_all_project_playbooks(),
                    limit=2,
                )
                warned_pairs = set(state.get("cochange_warned_pairs") or [])
                uncommitted_set = set(state.get("uncommitted_mutations") or [])
                fresh_hints: List[Tuple[str, str, int, float]] = []
                for src_f, partner_f, cnt, conf in partners:
                    pair_key = f"{src_f}->{partner_f}"
                    if partner_f in uncommitted_set or pair_key in warned_pairs:
                        continue
                    warned_pairs.add(pair_key)
                    fresh_hints.append((src_f, partner_f, cnt, conf))
                if fresh_hints:
                    state["cochange_warned_pairs"] = list(warned_pairs)
                    hint_lines = [
                        f"- `{src_f}` historically co-changes with `{partner_f}` ({cnt}x, {int(round(conf * 100))}%)"
                        for src_f, partner_f, cnt, conf in fresh_hints
                    ]
                    additional_blocks.append(
                        "<tmem_cochange_hint>\n"
                        "Co-change partners detected from repository history/traces (verify if corresponding updates are needed):\n"
                        + "\n".join(hint_lines)
                        + "\n</tmem_cochange_hint>"
                    )
                    suggested_files = [p for _, p, _, _ in fresh_hints]
                    state["pending_l1_hints"].append(
                        {"edited_file": fresh_hints[0][0], "suggested_files": suggested_files}
                    )
                    record_telemetry_event(
                        workspace_root,
                        "l1_cochange_hinted",
                        {
                            "session_id": session_id,
                            "edited_file": fresh_hints[0][0],
                            "suggested_files": suggested_files,
                        },
                    )

        # 4. Detect verification pass in bash / pwsh / run_command
        verify_passed = False
        if (
            is_shell_tool
            and effective_exit == 0
            and not is_error
            and state.get("uncommitted_mutations")
        ):
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
                verify_passed = True
                apply_session_trace_feedback(store, state, "success")

                # Resolve remaining un-adopted L1 hints as ignored upon verification
                if state.get("pending_l1_hints"):
                    for hint in state["pending_l1_hints"]:
                        record_telemetry_event(
                            workspace_root,
                            "l1_outcome",
                            {
                                "session_id": session_id,
                                "edited_file": hint.get("edited_file", ""),
                                "adopted_files": [],
                                "adopted": False,
                            },
                        )
                    state["pending_l1_hints"] = []

                uncommitted = list(state.get("uncommitted_mutations", []))
                prompted_set = set(state.get("milestone_prompted_for_mutations", []))
                has_code_file = any(is_code_file(f) for f in uncommitted)
                if set(uncommitted) != prompted_set and has_code_file:
                    state["milestone_prompted_for_mutations"] = uncommitted
                    additional_blocks.append(
                        "<tmem_milestone_reached>\n"
                        f"Milestone detected: {len(uncommitted)} file(s) modified ({', '.join(uncommitted[:4])}) and verification succeeded (`{cmd_line[:60]}`).\n"
                        "When concluding this turn, append a concise one-line prompt asking whether to persist the path to memory:\n"
                        "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
                        "</tmem_milestone_reached>"
                    )

        breaker_msg = record_tool_outcome_for_loop(
            state,
            has_failure=has_failure,
            error_sig=sig,
            mutated_files=mutated_this_step,
            verify_passed=verify_passed,
        )
        if breaker_msg:
            additional_blocks.append(breaker_msg)
            apply_session_trace_feedback(store, state, "contradict")

        save_runtime_state(workspace_root, state)
        if additional_blocks:
            return {"additionalContext": "\n\n".join(additional_blocks)}
        return {}

    return {}
