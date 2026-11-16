"""Tests for zero-LLM TokenEconomizer-inspired optimizations in tmem."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tmem.adapters.base import ParsedSession
from tmem.adapters.dsh import parse_apply_patch_targets
from tmem.adapters.swe_bench import SweBenchAdapter
from tmem.guards import evaluate_guards
from tmem.hooks.dsh import handle_dsh_hook
from tmem.mem_doc import (
    extract_micro_skills,
    format_micro_skills_block,
    normalize_command_template,
    render_memories_markdown,
)
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    TracePlaybook,
)
from tmem.slicer import BackwardTraceSlicer
from tmem.store import ProjectMemoryStore


def _make_sample_playbook(
    trace_id: str,
    target_file: str = "app.py",
    verify_cmd: str = "pytest -q",
    confidence: float = 0.5,
    status: str = "active",
) -> TracePlaybook:
    return TracePlaybook(
        trace_id=trace_id,
        agent_source="dsh",
        problem_intent=f"Fix issue in {target_file}",
        error_signatures=["ValueError: bad input"],
        touched_files=[target_file],
        touched_symbols=["process"],
        guards=[GuardProbe(probe_type="file_exists", target=target_file)],
        l0_critical_path=[{"action": "MUTATE", "target": target_file}],
        l1_cochange_topology=[f"Co-change: {target_file}"],
        l1_verification_cmds=[verify_cmd],
        confidence=confidence,
        status=status,
    )


class TestFailureLoopCircuitBreaker(unittest.TestCase):
    def test_dsh_error_streak_triggers_breaker_once_and_resets_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("def process():\n    return 1\n", encoding="utf-8")

            # 3 consecutive identical failures in DSH post_tool_use triggers breaker once on 3rd failure
            last_res = {}
            for _ in range(3):
                last_res = handle_dsh_hook(
                    "post_tool_use",
                    {
                        "workspaceRoot": ws,
                        "sessionId": "conv-loop",
                        "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                        "resultText": "ValueError: bad input\n\n[exit code: 1]",
                        "isError": True,
                    },
                )

            self.assertIn("<tmem_loop_breaker>", last_res.get("additionalContext", ""))
            self.assertIn("ValueError: bad input", last_res.get("additionalContext", ""))

            # 4th identical failure should NOT re-trigger the same key
            res2 = handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": ws,
                    "sessionId": "conv-loop",
                    "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                    "resultText": "ValueError: bad input\n\n[exit code: 1]",
                    "isError": True,
                },
            )
            self.assertNotIn("<tmem_loop_breaker>", res2.get("additionalContext", ""))

    def test_dsh_edit_fail_streak_triggers_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("x = 1\n", encoding="utf-8")

            # Edit -> Fail x3
            out = {}
            for i in range(3):
                handle_dsh_hook(
                    "post_tool_use",
                    {
                        "workspaceRoot": ws,
                        "sessionId": "dsh-loop",
                        "toolCall": {"name": "edit_file", "args": {"Path": "app.py"}},
                        "exitCode": 0,
                    },
                )
                out = handle_dsh_hook(
                    "post_tool_use",
                    {
                        "workspaceRoot": ws,
                        "sessionId": "dsh-loop",
                        "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                        "exitCode": 1,
                        "resultText": f"AssertionError: case {i}",
                    },
                )

            self.assertIn("additionalContext", out)
            self.assertIn("<tmem_loop_breaker>", out["additionalContext"])
            self.assertIn("app.py", out["additionalContext"])


class TestOutcomeDrivenMetabolism(unittest.TestCase):
    def test_record_outcome_updates_confidence_and_degrades_l0(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("def process():\n    return 1\n", encoding="utf-8")
            store = ProjectMemoryStore(ws)
            pb = _make_sample_playbook("trace-001", confidence=0.5)
            store.save_playbook(pb)

            # Positive outcome boosts confidence
            updated = store.record_trace_outcome(["trace-001"], "success")
            self.assertAlmostEqual(updated["trace-001"], 0.6)

            # Two contradictions drop confidence below 0.3 and mark stale
            store.record_trace_outcome(["trace-001"], "contradict")
            updated = store.record_trace_outcome(["trace-001"], "contradict")
            self.assertAlmostEqual(updated["trace-001"], 0.2)

            reloaded = next(p for p in store.get_all_project_playbooks() if p.trace_id == "trace-001")
            self.assertEqual(reloaded.status, "stale")
            self.assertEqual(reloaded.hit_count, 3)
            self.assertEqual(reloaded.success_count, 1)
            self.assertEqual(reloaded.contradict_count, 2)

            # Even though app.py still exists, evaluate_guards must degrade L0 -> L1
            ev = evaluate_guards(reloaded, ws)
            self.assertFalse(ev.l0_valid)
            self.assertEqual(ev.effective_tier, "L1_TOPOLOGY")

    def test_hook_rewards_injected_trace_on_verification_pass(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("def process():\n    return 1\n", encoding="utf-8")
            store = ProjectMemoryStore(ws)
            store.save_playbook(_make_sample_playbook("trace-reward", confidence=0.5))

            # Pre-invocation recalls trace-reward via explicit file anchor `app.py`
            handle_dsh_hook(
                "pre_invocation",
                {
                    "workspaceRoot": ws,
                    "sessionId": "conv-reward",
                    "lastUserInput": "Fix issue in app.py",
                },
            )
            # Edit + passing test
            handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": ws,
                    "sessionId": "conv-reward",
                    "toolCall": {"name": "edit", "args": {"file_path": "app.py"}},
                },
            )
            handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": ws,
                    "sessionId": "conv-reward",
                    "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                    "resultText": "1 passed",
                    "exitCode": 0,
                },
            )

            reloaded = next(
                p for p in store.get_all_project_playbooks() if p.trace_id == "trace-reward"
            )
            self.assertAlmostEqual(reloaded.confidence, 0.6)
            self.assertEqual(reloaded.success_count, 1)


class TestZeroLLMMicroSkills(unittest.TestCase):
    def test_extract_and_render_cross_task_micro_skills(self) -> None:
        pb1 = _make_sample_playbook("t1", verify_cmd="PYTHONPATH=. pytest -q 2>&1 | tail -n 20")
        pb2 = _make_sample_playbook("t2", verify_cmd="PYTHONPATH=. pytest -q")
        pb3 = _make_sample_playbook("t3", verify_cmd="python3 only_once.py")
        pb2.l2_causal_repairs.append(
            CausalRepairPair(
                error_signature="ModuleNotFoundError: foo",
                failed_command="pytest",
                repair_action="pip install foo",
                verification_cmd="pytest -q",
                is_env_or_toolchain=True,
            )
        )
        pb1.l2_causal_repairs.append(
            CausalRepairPair(
                error_signature="ModuleNotFoundError: foo",
                failed_command="pytest",
                repair_action="pip install foo",
                verification_cmd="pytest -q",
                is_env_or_toolchain=True,
            )
        )

        self.assertEqual(
            normalize_command_template("PYTHONPATH=. pytest -q 2>&1 | tail -n 20"),
            "PYTHONPATH=. pytest -q",
        )
        skills = extract_micro_skills([pb1, pb2, pb3], min_traces=2)
        self.assertIn("PYTHONPATH=. pytest -q", skills)
        self.assertIn("pip install foo", skills)
        self.assertNotIn("python3 only_once.py", skills)

        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("x = 1\n", encoding="utf-8")
            md = render_memories_markdown([pb1, pb2, pb3], ws)
            self.assertIn("## 0. Verified Env & Test Commands (Cross-Task Micro-Skills)", md)
            self.assertIn("`PYTHONPATH=. pytest -q`", md)

            block = format_micro_skills_block(skills)
            self.assertIn("<tmem_env_facts>", block)


class TestSlicerReadPrecision(unittest.TestCase):
    def test_generic_stem_reads_in_unrelated_dirs_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "pkg_a").mkdir()
            (Path(ws) / "pkg_b").mkdir()
            (Path(ws) / "pkg_a" / "utils.py").write_text("def helper_a():\n    return 1\n", encoding="utf-8")
            (Path(ws) / "pkg_b" / "utils.py").write_text("def helper_b():\n    return 2\n", encoding="utf-8")

            session = ParsedSession(
                agent_name="dsh",
                session_id="s-prec",
                workspace_root=ws,
                user_intents=["Update helper_a in pkg_a/utils.py"],
                actions=[
                    ActionNode(step_idx=1, kind=ActionKind.READ, tool_name="read_file", target_file="pkg_b/utils.py"),
                    ActionNode(step_idx=2, kind=ActionKind.READ, tool_name="read_file", target_file="pkg_a/utils.py"),
                    ActionNode(
                        step_idx=3,
                        kind=ActionKind.MUTATE,
                        tool_name="edit_file",
                        target_file="pkg_a/utils.py",
                        symbols_touched=["helper_a"],
                    ),
                    ActionNode(
                        step_idx=4,
                        kind=ActionKind.ENV_EXEC,
                        tool_name="run_command",
                        command="pytest -q",
                        exit_code=0,
                    ),
                ],
            )
            slicer = BackwardTraceSlicer()
            pb = slicer.slice(session, use_git_ground_truth=False)
            l0_reads = [s["target"] for s in pb.l0_critical_path if s["action"] == "READ"]
            self.assertIn("pkg_a/utils.py", l0_reads)
            self.assertNotIn("pkg_b/utils.py", l0_reads)


class TestPhase1TemporalCausality(unittest.TestCase):
    def test_parse_apply_patch_skips_context_diff_headers_and_end_patch(self) -> None:
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "*** 1,3 ****\n"
            "--- 1,3 ----\n"
            "+def added():\n"
            "+    return 1\n"
            "*** End Patch\n"
        )
        targets = parse_apply_patch_targets(patch)
        self.assertEqual([t[0] for t in targets], ["src/app.py"])

    def test_swe_adapter_marks_syntax_error_edits_failed_without_false_positive_file_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            traj_path = Path(ws) / "sample.traj"
            traj_payload = {
                "info": {},
                "trajectory": [
                    {
                        "action": "open src/qdp.py\n",
                        "observation": "[File: /repo/src/qdp.py (10 lines total)]\n1: raise ValueError('ERROR: bad')\n",
                    },
                    {
                        "action": "edit 2:2\nbad indent\nend_of_edit\n",
                        "observation": "Your proposed edit has introduced new syntax error(s).\n\nERRORS:\n- E999 SyntaxError\n",
                    },
                    {
                        "action": "edit 2:2\n    return 42\nend_of_edit\n",
                        "observation": "[File: /repo/src/qdp.py (11 lines total)]\n2:     return 42\n",
                    },
                    {
                        "action": "pytest tests/test_qdp.py\n",
                        "observation": "1 passed in 0.10s",
                        "exit_code": 0,
                    },
                ],
            }
            traj_path.write_text(json.dumps(traj_payload), encoding="utf-8")
            session = SweBenchAdapter().parse_transcript(str(traj_path), ws)
            self.assertEqual(session.actions[0].exit_code, 0)
            self.assertEqual(session.actions[1].exit_code, 1)
            self.assertEqual(session.actions[2].exit_code, 0)

            slicer = BackwardTraceSlicer()
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            self.assertIsNotNone(pb)
            self.assertNotIn(2, slicer.last_sliced_step_indices)
            self.assertIn(3, slicer.last_sliced_step_indices)

    def test_canonical_verify_accepts_env_prefix_and_rejects_swallow_true(self) -> None:
        slicer = BackwardTraceSlicer()
        self.assertEqual(
            slicer._extract_canonical_verification_cmd("PYTHONPATH=. python3 -m unittest discover -s tests -v"),
            "PYTHONPATH=. python3 -m unittest discover -s tests -v",
        )
        self.assertIsNone(
            slicer._extract_canonical_verification_cmd("flake8 src/ tests/ 2>/dev/null || true")
        )
        self.assertIsNone(
            slicer._extract_canonical_verification_cmd("r = re.compile(r'test') && print('test')")
        )

    def test_destroy_barrier_prunes_reverted_and_deleted_scratch_actions(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "src").mkdir()
            (Path(ws) / "tests").mkdir()
            (Path(ws) / "src" / "billing.py").write_text("def bill():\n    return 1\n", encoding="utf-8")
            session = ParsedSession(
                agent_name="dsh",
                session_id="s-barrier",
                workspace_root=ws,
                user_intents=["Fix billing module"],
                actions=[
                    ActionNode(step_idx=1, kind=ActionKind.READ, tool_name="read_file", target_file="src/reverted.py"),
                    ActionNode(step_idx=2, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="src/reverted.py"),
                    ActionNode(step_idx=3, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="git checkout -- src/reverted.py", exit_code=0),
                    ActionNode(step_idx=4, kind=ActionKind.MUTATE, tool_name="write_file", target_file="tests/scratch_debug.py"),
                    ActionNode(step_idx=5, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="python3 tests/scratch_debug.py", exit_code=0),
                    ActionNode(step_idx=6, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="rm -f tests/scratch_debug.py", exit_code=0),
                    ActionNode(step_idx=7, kind=ActionKind.READ, tool_name="read_file", target_file="src/billing.py"),
                    ActionNode(step_idx=8, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="src/billing.py", symbols_touched=["bill"]),
                    ActionNode(step_idx=9, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="PYTHONPATH=. python3 -m unittest discover -s tests -v", exit_code=0),
                ],
            )
            slicer = BackwardTraceSlicer()
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            self.assertIsNotNone(pb)
            self.assertEqual(pb.touched_files, ["src/billing.py"])
            self.assertEqual(slicer.last_sliced_step_indices, [7, 8, 9])

    def test_persistent_env_install_not_evicted_and_dangling_post_read_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "src").mkdir()
            (Path(ws) / "src" / "limiter.py").write_text("def limit():\n    return True\n", encoding="utf-8")
            session = ParsedSession(
                agent_name="dsh",
                session_id="s-install",
                workspace_root=ws,
                user_intents=["Add redis rate limiter"],
                actions=[
                    ActionNode(step_idx=1, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="pip install redis pytest", exit_code=0),
                    ActionNode(step_idx=2, kind=ActionKind.READ, tool_name="read_file", target_file="src/limiter.py"),
                    ActionNode(step_idx=3, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="pytest tests/test_limiter.py", exit_code=0),
                    ActionNode(step_idx=4, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="src/limiter.py", symbols_touched=["limit"]),
                    ActionNode(step_idx=5, kind=ActionKind.READ, tool_name="read_file", target_file="src/limiter.py"),
                    ActionNode(step_idx=6, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="pytest tests/test_limiter.py", exit_code=0),
                    ActionNode(step_idx=7, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="ruff check src/ || true", exit_code=0),
                ],
            )
            slicer = BackwardTraceSlicer()
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            self.assertIsNotNone(pb)
            # Install (1), pre-edit read (2), final mutate (4), and post-edit pytest (6) kept;
            # baseline pytest (3), dangling post-mutate read (5), and `|| true` lint (7) pruned.
            self.assertEqual(slicer.last_sliced_step_indices, [1, 2, 4, 6])
            l0_cmds = [s["command"] for s in pb.l0_critical_path if s["action"] == "ENV_EXEC"]
            self.assertIn("pip install redis pytest", l0_cmds)
            self.assertIn("pytest tests/test_limiter.py", l0_cmds)

    def test_which_prefix_and_dot_slash_normalization_and_atomic_state(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from tmem.adapters.base import normalize_rel_path
        from tmem.hooks.state import default_runtime_state, load_runtime_state, save_runtime_state

        slicer = BackwardTraceSlicer()
        self.assertEqual(
            slicer._extract_canonical_verification_cmd("which pytest && pytest tests/test_a.py"),
            "pytest tests/test_a.py",
        )
        self.assertIsNone(slicer._extract_canonical_verification_cmd("which pytest"))
        self.assertEqual(
            slicer._extract_canonical_verification_cmd("pytest -k 'which_case'"),
            "pytest -k 'which_case'",
        )
        self.assertIsNone(
            slicer._extract_canonical_verification_cmd(
                'PYTHONPATH=. python3 -c "import pytest; pytest.main()"'
            )
        )

        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "src").mkdir()
            (Path(ws) / "src" / "foo.py").write_text("def foo(): return 1\n", encoding="utf-8")
            self.assertEqual(normalize_rel_path("./src/foo.py", ws), "src/foo.py")

            session = ParsedSession(
                agent_name="dsh",
                session_id="s-norm",
                workspace_root=ws,
                user_intents=["Update foo in src/foo.py"],
                actions=[
                    ActionNode(
                        step_idx=1,
                        kind=ActionKind.READ,
                        tool_name="read_file",
                        target_file=normalize_rel_path("./src/foo.py", ws),
                    ),
                    ActionNode(
                        step_idx=2,
                        kind=ActionKind.MUTATE,
                        tool_name="edit_file",
                        target_file="src/foo.py",
                        symbols_touched=["foo"],
                    ),
                    ActionNode(
                        step_idx=3,
                        kind=ActionKind.READ,
                        tool_name="read_file",
                        target_file="src/foo.py",
                    ),
                    ActionNode(
                        step_idx=4,
                        kind=ActionKind.ENV_EXEC,
                        tool_name="run_command",
                        command="which pytest && pytest -q",
                        exit_code=0,
                    ),
                ],
            )
            pb = slicer.slice_session(session, use_git_ground_truth=False)
            self.assertIsNotNone(pb)
            assert pb is not None
            l0_reads = [s for s in pb.l0_critical_path if s["action"] == "READ"]
            self.assertEqual(len(l0_reads), 1)
            self.assertEqual(pb.l1_verification_cmds, ["pytest -q"])

            # Concurrent atomic state persistence across 8 threads leaves no .tmp artifacts and drops no session
            def _save(idx: int) -> None:
                st = default_runtime_state(f"conc_sess_{idx}")
                st["recent_files"] = [f"src/file_{idx}.py"]
                save_runtime_state(ws, st)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_save, range(8)))

            tmp_leftovers = list((Path(ws) / ".tmem").glob(".runtime_state.*.tmp"))
            self.assertEqual(tmp_leftovers, [])
            for idx in range(8):
                loaded = load_runtime_state(ws, f"conc_sess_{idx}")
                self.assertEqual(loaded["recent_files"], [f"src/file_{idx}.py"])

    def test_sqlite_connections_closed_on_all_store_operations(self) -> None:
        import os
        import sqlite3
        from unittest.mock import patch
        import tmem.store as store_mod

        opened = 0
        closed = 0
        orig_connect = sqlite3.connect

        class _TrackedConn:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            def __enter__(self):
                self._inner.__enter__()
                return self._inner

            def __exit__(self, exc_type, exc, tb):
                return self._inner.__exit__(exc_type, exc, tb)

            def close(self) -> None:
                nonlocal closed
                closed += 1
                self._inner.close()

            def __getattr__(self, name: str):
                return getattr(self._inner, name)

        def _tracked_connect(*args, **kwargs):
            nonlocal opened
            opened += 1
            return _TrackedConn(orig_connect(*args, **kwargs))

        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            pb = _make_sample_playbook("tr_close_test", target_file="app.py")
            pb.problem_intent = "Fix KeyError in app"
            pb.error_signatures = ["KeyError: 'cfg'"]
            pb.l2_causal_repairs = [
                CausalRepairPair(
                    error_signature="KeyError: 'cfg'",
                    failed_command="pytest -q",
                    repair_action="edit app.py (run)",
                    verification_cmd="pytest -q",
                )
            ]
            pb.touched_symbols = ["run"]

            with patch.object(store_mod.sqlite3, "connect", side_effect=_tracked_connect):
                st = ProjectMemoryStore(ws)
                st.save_playbook(pb, merge_similar=False)
                st.cascading_lookup("KeyError in app", file_paths=["app.py"])
                st.lookup_l2_repair(["KeyError: 'cfg'"], file_paths=["app.py"])
                st.record_trace_outcome(["tr_close_test"], "success")
                st.get_linked_traces("run")
                st.run_metabolism_gc()
                st.delete_playbook("tr_close_test")

                self.assertGreater(opened, 0)
                self.assertEqual(opened, closed, f"Leaked SQLite connections: opened={opened}, closed={closed}")
                if st.project_db_path.exists():
                    os.unlink(st.project_db_path)

    def test_post_commit_hook_cross_platform_diagnostics(self) -> None:
        import os
        import subprocess
        from unittest.mock import patch
        from tmem.installer import install_invisible_integration, resolve_dsh_entry_js

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir()
            subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
            dsh_home = Path(td) / "dsh_home"

            with patch.dict(os.environ, {"DSH_HOME": str(dsh_home)}):
                install_invisible_integration(str(ws), ["dsh"], install_git_hook=True)

            hook_path = ws / ".git" / "hooks" / "post-commit"
            hook_text = hook_path.read_text(encoding="utf-8")
            self.assertNotIn("|| true", hook_text)
            self.assertIn("TMEM_PY=", hook_text)
            self.assertIn("TMEM_PKG_ROOT=", hook_text)
            self.assertIn("[tmem] warning:", hook_text)

            # Verify runtime warning is emitted (not silently swallowed) if package root is unreachable
            broken_hook = hook_text.replace(
                f"TMEM_PKG_ROOT=",
                "TMEM_PKG_ROOT='/nonexistent_tmem_pkg_root_xyz' # ",
                1,
            )
            broken_path = Path(td) / "broken_hook.sh"
            broken_path.write_text(broken_hook, encoding="utf-8")
            res = subprocess.run(
                ["/bin/sh", str(broken_path)],
                cwd=ws,
                env={"PATH": os.environ.get("PATH", ""), "HOME": td},
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("[tmem] warning:", res.stderr)

            # Verify resolve_dsh_entry_js respects DSH_PACKAGE_ENTRY override
            fake_entry = Path(td) / "lib" / "index.js"
            fake_entry.parent.mkdir(parents=True)
            fake_entry.write_text("// fake entry\n", encoding="utf-8")
            with patch.dict(os.environ, {"DSH_PACKAGE_ENTRY": str(fake_entry)}):
                self.assertEqual(resolve_dsh_entry_js(), fake_entry.resolve())

    def test_partial_edit_schemas_and_unified_diff_preserve_l2_and_symbols(self) -> None:
        import subprocess
        from tmem.adapters.dsh import DshAdapter, parse_apply_patch_details
        from tmem.cli import execute_capture

        variants = [
            (
                "edit_file_old_new",
                "edit_file",
                {
                    "file_path": "src/parser.py",
                    "old_string": "    return text.strip()",
                    "new_string": '    if text is None:\n        return ""\n    return text.strip()',
                },
            ),
            (
                "str_replace_editor",
                "str_replace_editor",
                {
                    "path": "src/parser.py",
                    "old_str": "    return text.strip()",
                    "new_str": '    if text is None:\n        return ""\n    return text.strip()',
                },
            ),
            (
                "apply_patch_codex",
                "apply_patch",
                {
                    "input": (
                        "*** Begin Patch\n"
                        "*** Update File: src/parser.py\n"
                        "@@\n"
                        "-    return text.strip()\n"
                        "+    if text is None:\n"
                        '+        return ""\n'
                        "+    return text.strip()\n"
                        "*** End Patch"
                    )
                },
            ),
            (
                "apply_patch_unified_diff",
                "apply_patch",
                {
                    "patch": (
                        "diff --git a/src/parser.py b/src/parser.py\n"
                        "--- a/src/parser.py\n"
                        "+++ b/src/parser.py\n"
                        "@@ -1,2 +1,4 @@ def parse(text):\n"
                        " def parse(text):\n"
                        "-    return text.strip()\n"
                        "+    if text is None:\n"
                        '+        return ""\n'
                        "+    return text.strip()\n"
                    )
                },
            ),
        ]

        # Check unified diff parsing directly
        parsed_ud = parse_apply_patch_details(variants[-1][2]["patch"])
        self.assertEqual(len(parsed_ud), 1)
        self.assertEqual(parsed_ud[0][0], "src/parser.py")
        self.assertEqual(parsed_ud[0][2], [(1, 4)])

        for label, tool_name, tool_args in variants:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as ws:
                    ws_p = Path(ws)
                    (ws_p / "src").mkdir()
                    (ws_p / "tests").mkdir()
                    (ws_p / "src" / "parser.py").write_text(
                        "def parse(text):\n    return text.strip()\n", encoding="utf-8"
                    )
                    (ws_p / "tests" / "test_parser.py").write_text(
                        'from src.parser import parse\ndef test_p():\n    assert parse(None) == ""\n',
                        encoding="utf-8",
                    )
                    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
                    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, check=True)
                    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
                    subprocess.run(["git", "add", "."], cwd=ws, check=True)
                    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

                    # Fixed file on disk after agent edit
                    (ws_p / "src" / "parser.py").write_text(
                        'def parse(text):\n    if text is None:\n        return ""\n    return text.strip()\n',
                        encoding="utf-8",
                    )

                    sess_path = ws_p / f"{label}.v4.jsonl"
                    events = [
                        {"version": 4, "id": f"sess_{label}", "createdAt": 1731225600000},
                        {
                            "type": "user/message",
                            "seq": 1,
                            "data": {
                                "role": "user",
                                "content": [{"type": "text", "text": "Fix AttributeError in src/parser.py"}],
                                "source": {"kind": "user"},
                            },
                        },
                        {
                            "type": "tool/call",
                            "seq": 2,
                            "data": {"callId": "c1", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_parser.py"})},
                        },
                        {
                            "type": "tool/result",
                            "seq": 3,
                            "data": {
                                "message": {
                                    "callId": "c1",
                                    "content": [{"type": "text", "text": 'File "src/parser.py", line 2, in parse\nAttributeError: NoneType object has no attribute strip\n[exit code: 1]'}],
                                    "isError": False,
                                }
                            },
                        },
                        {
                            "type": "tool/call",
                            "seq": 4,
                            "data": {"callId": "c2", "name": tool_name, "arguments": json.dumps(tool_args)},
                        },
                        {
                            "type": "tool/result",
                            "seq": 5,
                            "data": {"message": {"callId": "c2", "content": [{"type": "text", "text": "OK"}], "isError": False}},
                        },
                        {
                            "type": "tool/call",
                            "seq": 6,
                            "data": {"callId": "c3", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_parser.py"})},
                        },
                        {
                            "type": "tool/result",
                            "seq": 7,
                            "data": {"message": {"callId": "c3", "content": [{"type": "text", "text": "1 passed\n[exit code: 0]"}], "isError": False}},
                        },
                    ]
                    sess_path.write_text("\n".join(json.dumps(ev) for ev in events), encoding="utf-8")

                    for use_git in (False, True):
                        sess = DshAdapter().parse_transcript(str(sess_path), ws)
                        pb = BackwardTraceSlicer().slice_session(sess, use_git_ground_truth=use_git)
                        self.assertIsNotNone(pb)
                        assert pb is not None
                        self.assertFalse(pb.is_trivial, f"{label} (git={use_git}) falsely marked trivial: {pb.pitfalls}")
                        self.assertEqual(len(pb.l2_causal_repairs), 1)
                        self.assertIn("src/parser.py (parse)", pb.l2_causal_repairs[0].repair_action)

                    cap = execute_capture(
                        workspace_root=ws,
                        agent_name="dsh",
                        transcript_path=str(sess_path),
                        session_id=f"sess_{label}",
                    )
                    self.assertEqual(cap["status"], "compiled")

        # Verify unified-diff apply_patch also tracks target_file in post_tool_use hook and top-level source edits keep L2
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "src").mkdir()
            (Path(ws) / "src" / "cfg.py").write_text("TIMEOUT = 60\n", encoding="utf-8")
            handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": ws,
                    "sessionId": "s_ud_hook",
                    "toolCall": {
                        "name": "apply_patch",
                        "args": {"patch": "--- a/src/cfg.py\n+++ b/src/cfg.py\n@@ -1 +1 @@\n-TIMEOUT = 0\n+TIMEOUT = 60\n"},
                    },
                },
            )
            ms = handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": ws,
                    "sessionId": "s_ud_hook",
                    "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                    "resultText": "1 passed\n[exit code: 0]",
                },
            )
            self.assertIn("<tmem_milestone_reached>", ms.get("additionalContext", ""))
            self.assertIn("src/cfg.py", ms.get("additionalContext", ""))

            # Top-level source edit (no enclosing function/class symbol) still retains L2 causality
            top_sess = ParsedSession(
                agent_name="dsh",
                session_id="s_top_level",
                workspace_root=ws,
                user_intents=["Fix timeout ValueError in src/cfg.py"],
                actions=[
                    ActionNode(
                        step_idx=1,
                        kind=ActionKind.ENV_EXEC,
                        tool_name="bash",
                        command="pytest tests/test_cfg.py",
                        exit_code=1,
                        error_signature="ValueError: timeout must be positive",
                    ),
                    ActionNode(
                        step_idx=2,
                        kind=ActionKind.MUTATE,
                        tool_name="edit_file",
                        target_file="src/cfg.py",
                        symbols_touched=[],
                    ),
                    ActionNode(
                        step_idx=3,
                        kind=ActionKind.ENV_EXEC,
                        tool_name="bash",
                        command="pytest tests/test_cfg.py",
                        exit_code=0,
                    ),
                ],
            )
            pb_top = BackwardTraceSlicer().slice_session(top_sess, use_git_ground_truth=False)
            self.assertIsNotNone(pb_top)
            assert pb_top is not None
            self.assertFalse(pb_top.is_trivial)
            self.assertEqual(len(pb_top.l2_causal_repairs), 1)
            self.assertEqual(pb_top.l2_causal_repairs[0].repair_action, "edit src/cfg.py")


if __name__ == "__main__":
    unittest.main()
