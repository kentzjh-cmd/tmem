"""
Headless CLI, `/tmem` Slash-Command Interceptor, and Autonomous Milestone Detector for tmem.

Commands:
- `tmem capture`: Compiles and saves effective execution trace to repository (.tmem/traces/).
- `tmem inspect`: Progressive disclosure inspection (index | playbook | raw | diff).
- `tmem replay`: Forensic chronological playback of raw steps.
- `tmem diff`: View unified git diff recorded during session.
- `tmem sync`: Re-index SQLite FTS5, validate guards, and regenerate MEMORIES.md living index.
- `tmem status`: Display active memory trace counts and store paths.
- `tmem gc`: Run guard evaluation and mark drifted paths stale.
- `tmem init`: Automated zero-config installer for agy, dsh, and git hooks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tmem import logger
from tmem.adapters import get_adapter
from tmem.adapters.dsh import DshAdapter
from tmem.guards import format_injected_context
from tmem.hooks import (
    SLASH_TMEM_RE,
    handle_agy_hook,
    handle_dsh_hook,
    load_runtime_state,
    parse_slash_tmem_command,
    save_runtime_state,
)
from tmem.hooks.agy import _parse_inspect_args
from tmem.installer import install_invisible_integration
from tmem.raw_storage import (
    RawTraceStore,
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.slicer import BackwardTraceSlicer, assess_playbook_triviality
from tmem.store import DualTierMemoryStore


def _read_stdin_json() -> Dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read JSON from stdin: %s", e)
        return {}


def execute_capture(
    workspace_root: str,
    agent_name: str = "agy",
    transcript_path: Optional[str] = None,
    scope: str = "project",
    custom_intent: Optional[str] = None,
    full_session: bool = False,
    session_id: Optional[str] = None,
    force: bool = False,
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

    state = load_runtime_state(workspace_root)
    min_step = 0 if full_session else int(state.get("last_captured_step_idx", 0))

    session = adapter.parse_transcript(t_path, workspace_root, min_step_idx=min_step)
    if not session.actions and min_step > 0:
        session = adapter.parse_transcript(t_path, workspace_root, min_step_idx=0)

    if custom_intent:
        session.user_intents.append(custom_intent.strip())
    else:
        clean_intents = [
            u for u in session.user_intents if not SLASH_TMEM_RE.match(u.strip())
        ]
        if clean_intents:
            session.user_intents = clean_intents

    slicer = BackwardTraceSlicer()
    pb = slicer.slice_session(session, use_git_ground_truth=True)
    if not pb:
        return {"status": "skipped", "reason": "no_effective_actions"}

    # Triviality filter: prevent low-value tautological traces from cluttering memory
    if pb.is_trivial and not force:
        if not custom_intent or len(custom_intent.strip()) < 10:
            return {
                "status": "skipped_trivial",
                "reason": pb.pitfalls[0] if pb.pitfalls else "Trivial single-file trace without error-fix causality or multi-file topology",
                "problem_intent": pb.problem_intent,
                "touched_files": pb.touched_files,
                "message": "Trace was evaluated as a routine single-file edit. Pass --force to capture anyway.",
            }

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
    save_runtime_state(workspace_root, state)

    return {
        "status": "compiled",
        "agent": agent_name,
        "scope": "project",
        "project_trace_id": proj_id,
        "raw_trace_path": pb.raw_trace_path,
        "problem_intent": pb.problem_intent,
        "touched_files": pb.touched_files,
        "is_trivial": pb.is_trivial,
        "confidence": pb.confidence,
        "raw_steps": pb.raw_steps_count,
        "sliced_steps": pb.sliced_steps_count,
        "compression_ratio": f"{pb.raw_steps_count} -> {pb.sliced_steps_count}",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tmem",
        description="Deterministic Backward-Slicing Execution Trace Memory Compiler (.tmem/)",
    )
    parser.add_argument("--version", action="version", version="tmem 0.2.0")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    sub = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # 1. init
    p_init = sub.add_parser("init", help="Install invisible hooks, rules, and skills for agents")
    p_init.add_argument("--agents", default="agy,dsh", help="Comma-separated agent names (agy, dsh)")
    p_init.add_argument("--workspace", default=os.getcwd(), help="Target workspace root")

    # 2. hook
    p_hook = sub.add_parser("hook", help="Headless hook handler invoked by agent harnesses")
    p_hook.add_argument("agent", choices=["agy", "dsh"], help="Agent type")
    p_hook.add_argument("event", help="Lifecycle event (e.g. pre_invocation, post_tool_use, pre_step)")

    # 3. capture
    p_cap = sub.add_parser("capture", help="Compile and save current session execution trace to .tmem/")
    p_cap.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_cap.add_argument("--agent", default="agy", choices=["agy", "dsh", "codex", "opencode", "pi"], help="Agent source (primary: agy, dsh; experimental: codex, opencode, pi)")
    p_cap.add_argument("--transcript", default=None, help="Explicit path to session transcript")
    p_cap.add_argument("--scope", default="project", choices=["project", "auto"], help="Storage scope")
    p_cap.add_argument("--intent", default=None, help="User intent or problem summary")
    p_cap.add_argument("--full-session", action="store_true", help="Capture full session from step 0")
    p_cap.add_argument("--force", "-f", action="store_true", help="Force saving even if evaluated as trivial")
    p_cap.add_argument("--quiet", "-q", action="store_true", help="Quiet output")

    # 4. lookup
    p_look = sub.add_parser("lookup", help="Query execution memory with zero-token pre-flight guards")
    p_look.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_look.add_argument("--query", "-q", default="", help="Intent or problem keyword search text")
    p_look.add_argument("--file", "-f", action="append", default=[], help="File path anchor")
    p_look.add_argument("--error", "-e", action="append", default=[], help="Error signature anchor")
    p_look.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")

    # 5. gc
    p_gc = sub.add_parser("gc", help="Run guard validation metabolism and prune stale paths")
    p_gc.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 6. status
    p_stat = sub.add_parser("status", help="Show execution memory store status and statistics")
    p_stat.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 7. list
    p_list = sub.add_parser("list", help="List stored execution trace playbooks")
    p_list.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")
    p_list.add_argument("--scope", default="project", choices=["project", "all"], help="Scope")

    # 8. forget
    p_forg = sub.add_parser("forget", help="Remove an execution trace playbook by trace_id")
    p_forg.add_argument("trace_id", help="Trace ID to delete")
    p_forg.add_argument("--workspace", default=os.getcwd(), help="Workspace root directory")

    # 9. evaluate
    p_eval = sub.add_parser("evaluate", help="Run benchmark suite on sample or historical trajectories")
    p_eval.add_argument("--data-dir", default=None, help="Directory containing .traj, .json, or .jsonl files")
    p_eval.add_argument("--json", action="store_true", help="Output raw summary as JSON")
    p_eval.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose tables")

    # 10. inspect
    p_insp = sub.add_parser("inspect", help="Progressive disclosure inspection of a trace")
    p_insp.add_argument("trace_id", help="Trace ID or prefix")
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

    if getattr(args, "verbose", False) or os.environ.get("TMEM_DEBUG") == "1":
        import logging
        logging.basicConfig(level=logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    if args.subcommand == "init":
        agent_list = [a.strip() for a in args.agents.split(",") if a.strip()]
        info = install_invisible_integration(args.workspace, agent_list)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "hook":
        payload = _read_stdin_json()
        if args.agent == "agy":
            out = handle_agy_hook(args.event, payload, capture_fn=execute_capture)
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if args.agent == "dsh":
            out = handle_dsh_hook(args.event, payload, capture_fn=execute_capture)
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
            force=args.force,
        )
        if not args.quiet:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("compiled", "skipped_trivial") else 1

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
        if tid:
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
