"""Phase 3 unit and integration tests for DSH-only focus, merging, co-change, and telemetry."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tmem.adapters.base import ParsedSession
from tmem.guards import evaluate_guards, format_injected_context
from tmem.hooks.dsh import handle_dsh_hook
from tmem.mem_doc import render_memories_markdown
from tmem.models import ActionKind, ActionNode, CausalRepairPair, GuardProbe, RawExecutionTrace, TracePlaybook
from tmem.raw_storage import format_progressive_view, format_raw_trace_replay
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore
from tmem.telemetry import compute_telemetry_summary


class TestPhase3DshArchitecture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prewrite_similarity_merging_avoids_duplicate_playbooks(self) -> None:
        store = DualTierMemoryStore(str(self.ws))
        pb1 = TracePlaybook(
            trace_id="tr_first_001",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-13T10:00:00Z",
            problem_intent="Fix ZeroDivisionError in billing tax calculator",
            error_signatures=["ZeroDivisionError: division by zero"],
            touched_files=["src/billing.py", "tests/test_billing.py"],
            touched_symbols=["calculate_unit_tax"],
            guards=[GuardProbe(probe_type="file_exists", target="src/billing.py")],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/billing.py"}],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="ZeroDivisionError: division by zero",
                    failed_command="pytest tests/test_billing.py",
                    repair_action="edit src/billing.py (symbols: calculate_unit_tax)",
                    verification_cmd="pytest tests/test_billing.py",
                    is_env_or_toolchain=False,
                )
            ],
            confidence=0.6,
        )
        pb2 = TracePlaybook(
            trace_id="tr_second_002",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-13T11:00:00Z",
            problem_intent="Fix ZeroDivisionError in billing tax calculator edge case",
            error_signatures=["ZeroDivisionError: division by zero"],
            touched_files=["src/billing.py", "tests/test_billing.py"],
            touched_symbols=["calculate_unit_tax", "normalize_rate"],
            guards=[GuardProbe(probe_type="file_exists", target="src/billing.py")],
            l0_critical_path=[
                {"step": 1, "action": "READ", "target": "src/billing.py"},
                {"step": 2, "action": "MUTATE", "target": "src/billing.py"},
            ],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="ZeroDivisionError: division by zero",
                    failed_command="pytest tests/test_billing.py",
                    repair_action="edit src/billing.py (symbols: calculate_unit_tax)",
                    verification_cmd="pytest tests/test_billing.py",
                    is_env_or_toolchain=False,
                )
            ],
            confidence=0.6,
        )

        id1, _ = store.save_playbook(pb1)
        id2, _ = store.save_playbook(pb2)
        self.assertEqual(id1, id2, "Similar L2 repair playbook should merge into existing trace_id")
        all_pbs = store.get_all_project_playbooks()
        self.assertEqual(len(all_pbs), 1)
        self.assertEqual(all_pbs[0].merge_count, 1)
        self.assertAlmostEqual(all_pbs[0].confidence, 0.68)
        self.assertIn("normalize_rate", all_pbs[0].touched_symbols)

        # Unrelated trace must NOT merge
        pb_other = TracePlaybook(
            trace_id="tr_other_003",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-13T11:30:00Z",
            problem_intent="Add Redis cache layer for session store",
            touched_files=["src/cache.py", "tests/test_cache.py"],
            confidence=0.6,
        )
        id3, _ = store.save_playbook(pb_other)
        self.assertEqual(id3, "tr_other_003")
        self.assertEqual(len(store.get_all_project_playbooks()), 2)

    def test_guard_drift_streak_and_gc_pruning(self) -> None:
        store = DualTierMemoryStore(str(self.ws))
        pb_no_l2 = TracePlaybook(
            trace_id="tr_drift_prune",
            agent_source="dsh",
            scope="project",
            problem_intent="Edit removed file",
            touched_files=["src/gone.py", "tests/test_gone.py"],
            guards=[GuardProbe(probe_type="file_exists", target="src/gone.py")],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/gone.py"}],
            confidence=0.7,
        )
        pb_with_l2 = TracePlaybook(
            trace_id="tr_drift_keep_l2",
            agent_source="dsh",
            scope="project",
            problem_intent="Fix missing package error",
            touched_files=["src/gone.py"],
            guards=[GuardProbe(probe_type="file_exists", target="src/gone.py")],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="ModuleNotFoundError: No module named 'httpx'",
                    failed_command="pytest",
                    repair_action="pip install httpx",
                    verification_cmd="pytest",
                    is_env_or_toolchain=True,
                )
            ],
            confidence=0.8,
        )
        store.save_playbook(pb_no_l2)
        store.save_playbook(pb_with_l2)

        for _ in range(2):
            store.run_metabolism_gc(prune_stale=False)
        stats3 = store.run_metabolism_gc(prune_stale=True)

        self.assertEqual(stats3["pruned"], 1)
        remaining_ids = [p.trace_id for p in store.get_all_project_playbooks()]
        self.assertNotIn("tr_drift_prune", remaining_ids)
        self.assertIn("tr_drift_keep_l2", remaining_ids)

    def test_pre_step_silences_vague_nl_and_post_tool_emits_l1_l2_telemetry(self) -> None:
        subprocess.run(["git", "init"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.ws, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.ws, check=True)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "a.py").write_text("def a(): return 1\n", encoding="utf-8")
        (self.ws / "src" / "b.py").write_text("def b(): return 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.ws, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.ws, check=True, capture_output=True)

        for rev in (1, 2):
            (self.ws / "src" / "a.py").write_text(f"def a(): return {rev}\n", encoding="utf-8")
            (self.ws / "src" / "b.py").write_text(f"def b(): return {rev}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.ws, check=True)
            subprocess.run(["git", "commit", "-m", f"pair {rev}"], cwd=self.ws, check=True, capture_output=True)

        store = DualTierMemoryStore(str(self.ws))
        pb = TracePlaybook(
            trace_id="tr_l2_tel",
            agent_source="dsh",
            scope="project",
            problem_intent="Fix KeyError in src/a.py",
            error_signatures=["KeyError: 'user_id'"],
            touched_files=["src/a.py", "src/b.py"],
            guards=[GuardProbe(probe_type="file_exists", target="src/a.py")],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="KeyError: 'user_id'",
                    failed_command="pytest -q",
                    repair_action="edit src/a.py (symbols: a)",
                    verification_cmd="pytest -q",
                    is_env_or_toolchain=False,
                )
            ],
            confidence=0.7,
        )
        store.save_playbook(pb)

        # 1. Vague NL prompt in pre_invocation must NOT inject speculative trace recall
        pre_vague = handle_dsh_hook(
            "pre_invocation",
            {
                "workspaceRoot": str(self.ws),
                "sessionId": "s_vague",
                "lastUserInput": "Can you inspect the architecture and explain error handling?",
            },
        )
        self.assertNotIn("<tmem_speculative_memory>", pre_vague.get("injectedContext", ""))

        # 2. Edit src/a.py -> L1 co-change hint suggests src/b.py
        edit_a = handle_dsh_hook(
            "post_tool_use",
            {
                "workspaceRoot": str(self.ws),
                "sessionId": "s_flow",
                "toolCall": {"name": "edit", "args": {"file_path": "src/a.py"}},
            },
        )
        self.assertIn("<tmem_cochange_hint>", edit_a.get("additionalContext", ""))
        self.assertIn("src/b.py", edit_a.get("additionalContext", ""))

        # 3. Agent reads/edits src/b.py -> L1 telemetry resolves as adopted=True
        handle_dsh_hook(
            "post_tool_use",
            {
                "workspaceRoot": str(self.ws),
                "sessionId": "s_flow",
                "toolCall": {"name": "read", "args": {"file_path": "src/b.py"}},
            },
        )

        # 4. Bash failure triggers L2 repair hint, then same-origin pass resolves telemetry
        fail_res = handle_dsh_hook(
            "post_tool_use",
            {
                "workspaceRoot": str(self.ws),
                "sessionId": "s_flow",
                "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                "resultText": "KeyError: 'user_id'\n[exit code: 1]",
            },
        )
        self.assertIn("L2 Causal Repair", fail_res.get("additionalContext", ""))

        handle_dsh_hook(
            "post_tool_use",
            {
                "workspaceRoot": str(self.ws),
                "sessionId": "s_flow",
                "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                "resultText": "1 passed\n[exit code: 0]",
            },
        )

        tel = compute_telemetry_summary(str(self.ws))
        self.assertEqual(tel["l1_hints"], 1)
        self.assertEqual(tel["l1_adopted"], 1)
        self.assertEqual(tel["l2_injections"], 1)
        self.assertEqual(tel["l2_resolved_green"], 1)

    def test_zero_token_behavior_summary_and_inline_session_id_lineage(self) -> None:
        (self.ws / "src").mkdir(exist_ok=True)
        (self.ws / "tests").mkdir(exist_ok=True)
        (self.ws / "src" / "router.py").write_text("def dispatch(): return 1\n", encoding="utf-8")
        (self.ws / "tests" / "test_router.py").write_text("def test_dispatch(): assert True\n", encoding="utf-8")

        slicer = BackwardTraceSlicer()
        sess1 = ParsedSession(
            agent_name="dsh",
            session_id="sess_hist_alpha",
            workspace_root=str(self.ws),
            user_intents=["Fix route dispatch fallback in src/router.py"],
            actions=[
                ActionNode(step_idx=1, kind=ActionKind.READ, tool_name="read", target_file="src/router.py"),
                ActionNode(step_idx=2, kind=ActionKind.MUTATE, tool_name="edit", target_file="src/router.py", symbols_touched=["dispatch"]),
                ActionNode(step_idx=3, kind=ActionKind.MUTATE, tool_name="edit", target_file="tests/test_router.py", symbols_touched=["test_dispatch"]),
                ActionNode(step_idx=4, kind=ActionKind.ENV_EXEC, tool_name="bash", command="pytest -q", exit_code=0),
            ],
        )
        pb1 = slicer.slice_session(sess1, use_git_ground_truth=False)
        self.assertIsNotNone(pb1)
        assert pb1 is not None
        self.assertEqual(pb1.source_session_ids, ["sess_hist_alpha"])
        self.assertIn("Behavior:", pb1.semantic_summary)
        self.assertIn("edited 2 file(s)", pb1.semantic_summary)

        store = DualTierMemoryStore(str(self.ws))
        id1, _ = store.save_playbook(pb1)

        # Merge a follow-up session for the same task and verify inline session lineage accumulates
        sess2 = ParsedSession(
            agent_name="dsh",
            session_id="sess_hist_beta",
            workspace_root=str(self.ws),
            user_intents=["Fix route dispatch fallback in src/router.py edge case"],
            actions=list(sess1.actions),
        )
        pb2 = slicer.slice_session(sess2, use_git_ground_truth=False)
        assert pb2 is not None
        id2, _ = store.save_playbook(pb2)
        self.assertEqual(id1, id2)

        merged_pb = store.get_all_project_playbooks()[0]
        self.assertEqual(merged_pb.source_session_ids, ["sess_hist_alpha", "sess_hist_beta"])

        # Verify inline session_id across injected context, MEMORIES.md, progressive view, replay, and inspect-by-session
        ev = evaluate_guards(merged_pb, str(self.ws))
        injected = format_injected_context([ev])
        self.assertIn("Session(s): sess_hist_alpha, sess_hist_beta", injected)

        md = render_memories_markdown([merged_pb], str(self.ws))
        self.assertIn("`sess_hist_alpha`", md)
        self.assertIn("`sess_hist_beta`", md)

        idx_view = format_progressive_view(merged_pb, level="index")
        self.assertIn("Session(s): sess_hist_alpha, sess_hist_beta", idx_view)
        pb_view = format_progressive_view(merged_pb, level="playbook")
        self.assertIn("**Session ID(s)**", pb_view)
        self.assertIn("`sess_hist_alpha`", pb_view)

        raw_tr = RawExecutionTrace(
            trace_id=merged_pb.trace_id,
            session_id="sess_hist_beta",
            agent_source="dsh",
            workspace_root=str(self.ws),
            created_at="2026-11-13T12:00:00Z",
        )
        replay_txt = format_raw_trace_replay(raw_tr)
        self.assertIn("Session: sess_hist_beta", replay_txt)

        insp_by_sess = handle_dsh_hook(
            "command",
            {"workspaceRoot": str(self.ws), "args": "inspect sess_hist_alpha index"},
        )
        self.assertEqual(insp_by_sess["kind"], "success")
        self.assertIn(merged_pb.trace_id, insp_by_sess["text"])


if __name__ == "__main__":
    unittest.main()
