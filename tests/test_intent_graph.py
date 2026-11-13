"""
Unit and integration tests for Phase 1 Deterministic Execution-Intent Graph:
- ActionNode.rationale extraction and zero-token binding
- Requirement and IntentEdge deterministic generation
- Compact diff summary extraction
- Partial guard degradation and surviving path formatting
- Backward compatibility with schema v1 playbooks
- FTS5 graph semantic indexing and symbol-based trace links
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tmem.adapters.base import ParsedSession, clean_rationale
from tmem.adapters.dsh import DshAdapter
from tmem.guards import evaluate_guards, format_injected_context
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    TracePlaybook,
)
from tmem.slicer import BackwardTraceSlicer, extract_compact_diff_summary
from tmem.store import DualTierMemoryStore


class TestIntentGraph(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="tmem_graph_test_")
        self.workspace = Path(self.test_dir)
        self.tmem_dir = self.workspace / ".tmem" / "traces"
        self.tmem_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clean_rationale_utility(self) -> None:
        """Test rationale cleaning, truncation, and secret scrubbing."""
        raw = "   ### Verify auth token ghp_secret1234567890abcdef\nwith retry logic   "
        cleaned = clean_rationale(raw, max_chars=40)
        self.assertIsNotNone(cleaned)
        self.assertNotIn("ghp_", cleaned)
        self.assertIn("[REDACTED_SECRET]", cleaned)
        self.assertTrue(len(cleaned) <= 40)

        # Empty/whitespace returns None
        self.assertIsNone(clean_rationale(""))
        self.assertIsNone(clean_rationale("   \n\t  "))

    def test_compact_diff_summary_extraction(self) -> None:
        """Test extracting compact diff summary from unified git diff."""
        patch = (
            "diff --git a/tmem/guards.py b/tmem/guards.py\n"
            "--- a/tmem/guards.py\n"
            "+++ b/tmem/guards.py\n"
            "@@ -25,3 +25,5 @@\n"
            "+def evaluate_guards():\n"
            "+    # Added body hash check\n"
            "+    return True\n"
            "diff --git a/tmem/other.py b/tmem/other.py\n"
            "--- a/tmem/other.py\n"
            "+++ b/tmem/other.py\n"
            "@@ -1,2 +1,2 @@\n"
            "+def other(): pass\n"
        )
        summary = extract_compact_diff_summary(patch, "tmem/guards.py")
        self.assertIsNotNone(summary)
        self.assertTrue(summary.startswith("+ "))
        self.assertIn("def evaluate_guards():", summary)
        self.assertIn("return True", summary)
        # Should ignore other files
        self.assertNotIn("def other():", summary)

    def test_dsh_adapter_rationale_and_intent_binding(self) -> None:
        """Test that DshAdapter extracts intents and tool actions from v4 transcripts."""
        transcript_path = self.workspace / "session.v4.jsonl"
        lines = [
            json.dumps({"version": 4, "id": "sess_intent_01", "createdAt": 1731225600000}),
            json.dumps({
                "type": "user/message",
                "seq": 1,
                "time": 1731225601000,
                "data": {
                    "id": "u1",
                    "role": "user",
                    "content": [{"type": "text", "text": "Fix guards verification bug in evaluate_guards"}],
                    "source": {"kind": "user"},
                },
            }),
            json.dumps({
                "type": "tool/call",
                "seq": 2,
                "time": 1731225602000,
                "data": {"callId": "c1", "name": "read", "arguments": json.dumps({"file_path": "tmem/guards.py"})},
            }),
            json.dumps({
                "type": "tool/result",
                "seq": 3,
                "time": 1731225603000,
                "data": {"message": {"callId": "c1", "content": [{"type": "text", "text": "def evaluate_guards(): pass"}], "isError": False}},
            }),
            json.dumps({
                "type": "tool/call",
                "seq": 4,
                "time": 1731225604000,
                "data": {"callId": "c2", "name": "edit", "arguments": json.dumps({"file_path": "tmem/guards.py", "old_string": "pass", "new_string": "return True"})},
            }),
            json.dumps({
                "type": "tool/result",
                "seq": 5,
                "time": 1731225605000,
                "data": {"message": {"callId": "c2", "content": [{"type": "text", "text": "Edited tmem/guards.py"}], "isError": False}},
            }),
            json.dumps({
                "type": "tool/call",
                "seq": 6,
                "time": 1731225606000,
                "data": {"callId": "c3", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_guards.py"})},
            }),
            json.dumps({
                "type": "tool/result",
                "seq": 7,
                "time": 1731225607000,
                "data": {"message": {"callId": "c3", "content": [{"type": "text", "text": "1 passed in 0.05s\n[exit code: 0]"}], "isError": False}},
            }),
        ]
        transcript_path.write_text("\n".join(lines), encoding="utf-8")

        adapter = DshAdapter()
        session = adapter.parse_transcript(str(transcript_path), str(self.workspace))

        self.assertTrue(len(session.user_intents) >= 1)
        self.assertIn("Fix guards verification bug", session.user_intents[0])
        self.assertEqual(len(session.actions), 3)
        self.assertEqual(session.actions[0].target_file, "tmem/guards.py")
        self.assertEqual(session.actions[1].target_file, "tmem/guards.py")
        self.assertEqual(session.actions[2].command, "pytest tests/test_guards.py")

    def test_slicer_chronological_path_and_schema_v3(self) -> None:
        """Test BackwardTraceSlicer compiles schema_version=3 with chronologically ordered L0 steps."""
        act1 = ActionNode(
            step_idx=1,
            kind=ActionKind.READ,
            tool_name="read_file",
            target_file="src/auth.py",
            rationale="Locate auth token verification logic",
        )
        act2 = ActionNode(
            step_idx=2,
            kind=ActionKind.MUTATE,
            tool_name="edit_file",
            target_file="src/auth.py",
            symbols_touched=["verify_token"],
            rationale="Add clock skew leeway check",
        )
        act3 = ActionNode(
            step_idx=3,
            kind=ActionKind.ENV_EXEC,
            tool_name="run_command",
            command="pytest tests/test_auth.py",
            exit_code=0,
            rationale="Verify auth unit tests pass",
        )

        session = ParsedSession(
            agent_name="dsh",
            session_id="test_sess_graph",
            workspace_root=str(self.workspace),
            user_intents=["Fix clock skew error in auth token verification"],
            actions=[act1, act2, act3],
        )

        slicer = BackwardTraceSlicer()
        playbook = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(playbook)
        self.assertEqual(playbook.schema_version, 3)
        step_nums = [s.get("step") for s in playbook.l0_critical_path]
        self.assertEqual(step_nums, sorted(step_nums))
        self.assertEqual(playbook.l0_critical_path[0].get("rationale"), "Locate auth token verification logic")

    def test_schema_v1_v2_backwards_compatibility(self) -> None:
        """Test that legacy v1/v2 playbook JSONs with removed fields load cleanly."""
        v2_data = {
            "trace_id": "tr_legacy_001",
            "agent_source": "agy",
            "scope": "project",
            "created_at": "2026-11-10T10:00:00Z",
            "schema_version": 2,
            "problem_intent": "Legacy intent with deprecated fields",
            "semantic_summary": "Legacy summary",
            "error_signatures": ["SomeError"],
            "touched_files": ["src/legacy.py"],
            "touched_symbols": ["legacy_func"],
            "guards": [{"probe_type": "file_exists", "target": "src/legacy.py", "expected": ""}],
            "l0_critical_path": [{"action": "MUTATE", "target": "src/legacy.py"}],
            "l1_cochange_topology": ["Co-edited in session: src/legacy.py <-> tests/test_legacy.py"],
            "l1_verification_cmds": ["pytest"],
            "l2_causal_repairs": [],
            "requirements": [{"req_id": "R1", "text": "legacy req", "source": "user"}],
            "intent_edges": [{"src": "R1", "dst": "s1", "relation": "served_by", "why": "legacy"}],
            "confidence": 0.95,
            "hit_count": 0,
            "status": "active",
        }
        pb = TracePlaybook.from_dict(v2_data)
        self.assertEqual(pb.trace_id, "tr_legacy_001")
        self.assertEqual(pb.schema_version, 2)
        self.assertIsNone(pb.root_cause)
        self.assertEqual(pb.pitfalls, [])

        # Re-serializing to dict omits removed bloat fields
        d = pb.to_dict()
        self.assertNotIn("requirements", d)
        self.assertNotIn("intent_edges", d)

    def test_partial_guard_degradation(self) -> None:
        """Test partial guard degradation when one file drifts while another file survives."""
        # Create file1 but NOT file2 on disk
        file1 = self.workspace / "src" / "valid.py"
        file1.parent.mkdir(parents=True, exist_ok=True)
        file1.write_text("def valid(): pass\n", encoding="utf-8")

        playbook = TracePlaybook(
            trace_id="tr_partial_001",
            agent_source="agy",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Fix dual file validation bug",
            guards=[
                GuardProbe(probe_type="file_exists", target="src/valid.py", expected=""),
                GuardProbe(probe_type="file_exists", target="src/deleted.py", expected=""),
            ],
            l0_critical_path=[
                {"step": 1, "action": "MUTATE", "target": "src/valid.py", "rationale": "Updated valid handler"},
                {"step": 2, "action": "MUTATE", "target": "src/deleted.py", "rationale": "Updated deleted handler"},
                {"step": 3, "action": "ENV_EXEC", "command": "pytest", "rationale": "Run regression test"},
            ],
            touched_files=["src/valid.py", "src/deleted.py"],
            l1_cochange_topology=["src/valid.py <-> src/deleted.py"],
            l1_verification_cmds=["pytest"],
        )

        ev = evaluate_guards(playbook, str(self.workspace))
        self.assertFalse(ev.l0_valid)
        self.assertEqual(ev.effective_tier, "L1_TOPOLOGY")
        self.assertIn("missing_file:src/deleted.py", ev.failed_guards)
        self.assertIn("src/valid.py", ev.passed_files)

        # Context formatting retains partial surviving path
        context = format_injected_context([ev])
        self.assertIn("L0 concrete paths drifted", context)
        self.assertIn("missing_file:src/deleted.py", context)
        self.assertIn("Partial L0 Path (Drifted files hidden)", context)
        self.assertIn("MUTATE(src/valid.py)", context)
        self.assertNotIn("MUTATE(src/deleted.py)", context)

    def test_fts5_indexing_of_graph_semantics_and_trace_links(self) -> None:
        """Test that FTS5 indexes rationale/why/requirements and trace_links enables symbol linking."""
        store = DualTierMemoryStore(workspace_root=str(self.workspace))

        playbook = TracePlaybook(
            trace_id="tr_graph_indexed",
            agent_source="agy",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Fix authentication session refresh token race condition",
            semantic_summary="Fixed race condition by introducing mutex lock",
            root_cause="Concurrent refresh requests invalidated rotating tokens",
            touched_files=["src/auth_session.py"],
            touched_symbols=["refresh_mutex_lock", "validate_token_rotation"],
            l0_critical_path=[
                {
                    "step": 1,
                    "action": "MUTATE",
                    "target": "src/auth_session.py",
                    "rationale": "Implemented atomic compare-and-swap token rotation lock",
                }
            ],
            l1_cochange_topology=[],
            l1_verification_cmds=["pytest tests/test_auth_session.py"],
        )

        store.save_playbook(playbook, target_scope="project")

        # 1. Lookup by term present ONLY in step rationale ("compare-and-swap")
        res1 = store.cascading_lookup(query_text="compare-and-swap")
        self.assertTrue(len(res1) >= 1)
        self.assertEqual(res1[0].playbook.trace_id, "tr_graph_indexed")

        # 2. Lookup by term present ONLY in root_cause ("Concurrent refresh requests")
        res2 = store.cascading_lookup(query_text="Concurrent refresh")
        self.assertTrue(len(res2) >= 1)
        self.assertEqual(res2[0].playbook.trace_id, "tr_graph_indexed")

        # 3. Check trace_links table
        linked = store.get_linked_traces("refresh_mutex_lock", exclude_trace_id="tr_other")
        self.assertIn("tr_graph_indexed", linked)

    def test_repeated_mutations_symbol_merge_and_causal_attribution(self) -> None:
        """Verify multiple mutations to same file merge symbols and L2 repair matches exact repair step."""
        act1 = ActionNode(
            step_idx=1,
            kind=ActionKind.MUTATE,
            tool_name="edit_file",
            target_file="src/other.py",
            symbols_touched=["init_other"],
        )
        act2 = ActionNode(
            step_idx=2,
            kind=ActionKind.MUTATE,
            tool_name="edit_file",
            target_file="src/auth.py",
            symbols_touched=["parse_header"],
        )
        act3 = ActionNode(
            step_idx=3,
            kind=ActionKind.ENV_EXEC,
            tool_name="run_command",
            command="pytest tests/test_auth.py",
            exit_code=1,
            error_signature="ValueError: invalid signature",
        )
        act4 = ActionNode(
            step_idx=4,
            kind=ActionKind.MUTATE,
            tool_name="edit_file",
            target_file="src/auth.py",
            symbols_touched=["verify_signature"],
            rationale="Fix signature verification padding",
        )
        act5 = ActionNode(
            step_idx=5,
            kind=ActionKind.ENV_EXEC,
            tool_name="run_command",
            command="pytest tests/test_auth.py",
            exit_code=0,
        )

        session = ParsedSession(
            agent_name="agy",
            session_id="sess_multi_mut",
            workspace_root=str(self.workspace),
            user_intents=["Fix signature verification in src/auth.py"],
            actions=[act1, act2, act3, act4, act5],
        )

        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)

        auth_mut = next(s for s in pb.l0_critical_path if s.get("target") == "src/auth.py")
        self.assertEqual(auth_mut["symbols"], ["parse_header", "verify_signature"])
        self.assertEqual(auth_mut.get("rationale"), "Fix signature verification padding")
        self.assertEqual(len(pb.l2_causal_repairs), 1)
        self.assertIn("src/auth.py", pb.l2_causal_repairs[0].repair_action)

    def test_sqlite_orphan_cleanup_and_gc_db_sync(self) -> None:
        """Verify deleted JSON traces are purged from SQLite and GC updates SQLite confidence/status."""
        store = DualTierMemoryStore(workspace_root=str(self.workspace))
        pb = TracePlaybook(
            trace_id="tr_orphan_check",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Fix missing config parser",
            touched_files=["src/missing_cfg.py", "src/missing_schema.py"],
            guards=[GuardProbe(probe_type="file_exists", target="src/missing_cfg.py")],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/missing_cfg.py"}],
            l1_verification_cmds=["pytest tests/test_cfg.py"],
            confidence=1.0,
        )
        store.save_playbook(pb)
        self.assertEqual(len(store.cascading_lookup(query_text="missing config parser")), 1)

        # Run GC: guard fails and no L1/L2 -> status becomes stale, confidence drops to 0.7 in both JSON and SQLite
        stats = store.run_metabolism_gc()
        self.assertEqual(stats["stale"], 1)
        recalled_after_gc = store.cascading_lookup(query_text="missing config parser")
        self.assertEqual(len(recalled_after_gc), 1)
        self.assertEqual(recalled_after_gc[0].playbook.status, "stale")
        self.assertAlmostEqual(recalled_after_gc[0].playbook.confidence, 0.7)

        # Now delete the JSON file directly from disk (e.g. git branch switch / rm)
        (store.project_traces_dir / "tr_orphan_check.json").unlink()
        recalled_after_rm = store.cascading_lookup(query_text="missing config parser")
        self.assertEqual(recalled_after_rm, [])

    def test_ast_symbol_guard_precision(self) -> None:
        """Verify that Python AST symbol guard accurately checks real definitions and rejects comments."""
        code_file = self.workspace / "src" / "service.py"
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.write_text(
            '# Comment mentioning old_legacy_handler\n'
            '"""Docstring mentioning deprecated_method."""\n'
            'class WorkerService:\n'
            '    def process_task(self):\n'
            '        pass\n'
            'def standalone_runner():\n'
            '    pass\n',
            encoding="utf-8",
        )

        pb_valid = TracePlaybook(
            trace_id="tr_ast_valid",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Worker service process task",
            guards=[
                GuardProbe(probe_type="symbol_present", target="src/service.py", expected="WorkerService"),
                GuardProbe(probe_type="symbol_present", target="src/service.py", expected="WorkerService.process_task"),
                GuardProbe(probe_type="symbol_present", target="src/service.py", expected="standalone_runner"),
            ],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/service.py"}],
        )
        ev_valid = evaluate_guards(pb_valid, str(self.workspace))
        self.assertTrue(ev_valid.l0_valid)
        self.assertEqual(ev_valid.effective_tier, "L0_FULL")

        # Comment mention must NOT pass AST check
        pb_comment = TracePlaybook(
            trace_id="tr_ast_comment",
            agent_source="agy",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Legacy handler",
            guards=[
                GuardProbe(probe_type="symbol_present", target="src/service.py", expected="old_legacy_handler"),
            ],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/service.py"}],
        )
        ev_comment = evaluate_guards(pb_comment, str(self.workspace))
        self.assertFalse(ev_comment.l0_valid)
        self.assertIn("missing_symbol:src/service.py#old_legacy_handler", ev_comment.failed_guards)

        # Docstring mention must NOT pass AST check
        pb_doc = TracePlaybook(
            trace_id="tr_ast_doc",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Deprecated method",
            guards=[
                GuardProbe(probe_type="symbol_present", target="src/service.py", expected="deprecated_method"),
            ],
            l0_critical_path=[{"step": 1, "action": "MUTATE", "target": "src/service.py"}],
        )
        ev_doc = evaluate_guards(pb_doc, str(self.workspace))
        self.assertFalse(ev_doc.l0_valid)
        self.assertIn("missing_symbol:src/service.py#deprecated_method", ev_doc.failed_guards)

    def test_portable_init_contracts(self) -> None:
        """Verify that tmem init outputs portable DSH relative configurations without hardcoded absolute paths."""
        from tmem.cli import install_invisible_integration
        temp_dir = tempfile.mkdtemp(prefix="tmem_init_test_")
        try:
            info = install_invisible_integration(temp_dir, ["dsh"])
            self.assertFalse((Path(temp_dir) / ".agents" / "plugins" / "tmem").exists())

            dsh_patch = Path(temp_dir) / ".dsh" / "tmem.patch.yml"
            self.assertTrue(dsh_patch.exists())
            patch_content = dsh_patch.read_text(encoding="utf-8")
            self.assertIn("./.dsh/plugins/tmem-dsh-plugin.mjs", patch_content)
            self.assertNotIn("pythonBin: /usr/bin/python3", patch_content)
            self.assertTrue((Path(temp_dir) / ".dsh" / "plugins" / "tmem-dsh-plugin.mjs").exists())
            self.assertEqual(len(info["agents"]), 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
