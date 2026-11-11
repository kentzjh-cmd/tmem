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

from tmem.adapters.agy import AgyAdapter
from tmem.adapters.base import ParsedSession, clean_rationale
from tmem.guards import evaluate_guards, format_injected_context
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    IntentEdge,
    Requirement,
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

    def test_agy_adapter_rationale_and_requirement_binding(self) -> None:
        """Test that AgyAdapter binds toolSummary/content to ActionNode.rationale and extracts requirements."""
        transcript_path = self.workspace / "transcript.jsonl"
        lines = [
            json.dumps({
                "type": "USER_INPUT",
                "step_index": 1,
                "created_at": "2026-11-11T12:00:00Z",
                "content": "<USER_REQUEST>Fix guards verification bug in evaluate_guards</USER_REQUEST>",
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "step_index": 2,
                "created_at": "2026-11-11T12:00:10Z",
                "content": "I will examine the guards module to locate the assertion check.",
                "tool_calls": [{
                    "name": "read_file",
                    "args": {"Path": "tmem/guards.py", "toolSummary": "Inspect evaluate_guards implementation"},
                }],
            }),
            json.dumps({
                "type": "GENERIC",
                "step_index": 3,
                "created_at": "2026-11-11T12:00:15Z",
                "content": "def evaluate_guards(): pass",
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "step_index": 4,
                "created_at": "2026-11-11T12:00:20Z",
                "content": "Now updating evaluate_guards to add symbol checking.",
                "tool_calls": [{
                    "name": "edit_file",
                    "args": {
                        "Path": "tmem/guards.py",
                        "Replacement": "def evaluate_guards(): return True",
                        "toolSummary": "Implement symbol checking logic",
                    },
                }],
            }),
            json.dumps({
                "type": "GENERIC",
                "step_index": 5,
                "created_at": "2026-11-11T12:00:25Z",
                "content": "File modified successfully.",
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "step_index": 6,
                "created_at": "2026-11-11T12:00:30Z",
                "content": "Running test suite to verify fix.",
                "tool_calls": [{
                    "name": "run_command",
                    "args": {"CommandLine": "pytest tests/test_guards.py", "toolSummary": "Run guards unit tests"},
                }],
            }),
            json.dumps({
                "type": "GENERIC",
                "step_index": 7,
                "created_at": "2026-11-11T12:00:35Z",
                "content": "1 passed in 0.05s\nCommand exited with code 0",
            }),
        ]
        transcript_path.write_text("\n".join(lines), encoding="utf-8")

        adapter = AgyAdapter()
        session = adapter.parse_transcript(str(transcript_path), str(self.workspace))

        # Check requirements parsed
        self.assertEqual(len(session.user_requirements), 1)
        self.assertEqual(session.user_requirements[0].req_id, "R1")
        self.assertIn("Fix guards verification bug", session.user_requirements[0].text)

        # Check rationales bound on actions
        self.assertEqual(len(session.actions), 3)
        self.assertEqual(session.actions[0].rationale, "Inspect evaluate_guards implementation")
        self.assertEqual(session.actions[1].rationale, "Implement symbol checking logic")
        self.assertEqual(session.actions[2].rationale, "Run guards unit tests")

    def test_slicer_intent_graph_generation(self) -> None:
        """Test BackwardTraceSlicer generates deterministic IntentEdges and Requirement nodes."""
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
            agent_name="agy",
            session_id="test_sess_graph",
            workspace_root=str(self.workspace),
            user_intents=["Fix clock skew error in auth token verification"],
            user_requirements=[
                Requirement(req_id="R1", text="Fix clock skew error in auth token verification", source="user")
            ],
            actions=[act1, act2, act3],
        )

        slicer = BackwardTraceSlicer()
        playbook = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(playbook)
        self.assertEqual(playbook.schema_version, 2)
        self.assertEqual(len(playbook.requirements), 1)
        self.assertEqual(playbook.requirements[0].req_id, "R1")

        # Check intent_edges
        self.assertTrue(len(playbook.intent_edges) >= 3)
        edge_relations = {e.relation for e in playbook.intent_edges}
        self.assertIn("served_by", edge_relations)
        self.assertIn("informs", edge_relations)
        self.assertIn("modifies", edge_relations)
        self.assertIn("verifies", edge_relations)

        # Check informs edge connects s1 to s2
        informs_edge = next(e for e in playbook.intent_edges if e.relation == "informs")
        self.assertEqual(informs_edge.src, "s1")
        self.assertEqual(informs_edge.dst, "s2")
        self.assertIn(1, informs_edge.evidence)
        self.assertIn(2, informs_edge.evidence)

        # Check modifies edge references symbol
        modifies_edge = next(e for e in playbook.intent_edges if e.relation == "modifies")
        self.assertEqual(modifies_edge.src, "s2")
        self.assertIn("file:src/auth.py#verify_token", modifies_edge.dst)

    def test_schema_v1_backwards_compatibility(self) -> None:
        """Test that legacy v1 playbook JSONs load cleanly with default graph fields."""
        v1_data = {
            "trace_id": "tr_legacy_001",
            "agent_source": "agy",
            "scope": "project",
            "created_at": "2026-11-10T10:00:00Z",
            "problem_intent": "Legacy intent without graph schema",
            "semantic_summary": "Legacy summary",
            "error_signatures": ["SomeError"],
            "touched_files": ["src/legacy.py"],
            "touched_symbols": ["legacy_func"],
            "guards": [{"probe_type": "file_exists", "target": "src/legacy.py", "expected": ""}],
            "l0_critical_path": [{"action": "MUTATE", "target": "src/legacy.py"}],
            "l1_cochange_topology": ["Primary target: src/legacy.py"],
            "l1_verification_cmds": ["pytest"],
            "l2_causal_repairs": [],
            "confidence": 0.95,
            "hit_count": 0,
            "status": "active",
        }
        pb = TracePlaybook.from_dict(v1_data)
        self.assertEqual(pb.trace_id, "tr_legacy_001")
        self.assertEqual(pb.schema_version, 1)
        self.assertEqual(pb.requirements, [])
        self.assertEqual(pb.intent_edges, [])
        self.assertIsNone(pb.root_cause)
        self.assertEqual(pb.pitfalls, [])

        # Re-serializing to dict works
        d = pb.to_dict()
        self.assertEqual(d["requirements"], [])
        self.assertEqual(d["intent_edges"], [])

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
            requirements=[
                Requirement(req_id="R1", text="Prevent duplicate token rotation in high concurrency", source="user")
            ],
            intent_edges=[
                IntentEdge(
                    src="s1",
                    dst="file:src/auth_session.py#refresh_mutex_lock",
                    relation="modifies",
                    why="Applied atomic compare-and-swap on token version",
                    evidence=[1],
                )
            ],
            l0_critical_path=[
                {
                    "step": 1,
                    "action": "MUTATE",
                    "target": "src/auth_session.py",
                    "rationale": "Implemented atomic CAS token rotation lock",
                }
            ],
            l1_cochange_topology=["Primary target: src/auth_session.py"],
            l1_verification_cmds=["pytest tests/test_auth_session.py"],
        )

        store.save_playbook(playbook, target_scope="project")

        # 1. Lookup by term present ONLY in rationale ("compare-and-swap")
        res1 = store.cascading_lookup(query_text="compare-and-swap")
        self.assertTrue(len(res1) >= 1)
        self.assertEqual(res1[0].playbook.trace_id, "tr_graph_indexed")

        # 2. Lookup by term present ONLY in root_cause ("Concurrent refresh requests")
        res2 = store.cascading_lookup(query_text="Concurrent refresh")
        self.assertTrue(len(res2) >= 1)
        self.assertEqual(res2[0].playbook.trace_id, "tr_graph_indexed")

        # 3. Lookup by term present ONLY in requirement ("high concurrency")
        res3 = store.cascading_lookup(query_text="high concurrency")
        self.assertTrue(len(res3) >= 1)
        self.assertEqual(res3[0].playbook.trace_id, "tr_graph_indexed")

        # 4. Check trace_links table
        linked = store.get_linked_traces("refresh_mutex_lock", exclude_trace_id="tr_other")
        self.assertIn("tr_graph_indexed", linked)


if __name__ == "__main__":
    unittest.main()
