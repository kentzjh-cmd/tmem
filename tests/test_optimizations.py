"""
Unit tests validating optimizations implemented based on pragmatic codebase review:
1. Trivial/Tautological playbook detection and filter (assess_playbook_triviality, --force).
2. Enhanced L2 causal repair extraction from script reproductions and real error signatures.
3. Ground truth boundary isolation: dirty workspace files outside session are not absorbed.
4. Modularized CLI & hooks dispatching.
5. Milestone prompt suppression on non-code file modifications.
6. Clean repository-scoped boundaries (no legacy global scope remnants).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tmem.adapters.base import (
    ParsedSession,
    extract_error_signature,
    is_high_entropy_error_signature,
)
from tmem.cli import execute_capture, handle_agy_hook
from tmem.models import ActionKind, ActionNode, GuardProbe, TracePlaybook
from tmem.slicer import (
    BackwardTraceSlicer,
    assess_playbook_triviality,
    get_git_diff_patch,
    get_git_ground_truth,
)
from tmem.store import ProjectMemoryStore


class TestPragmaticOptimizations(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="tmem_opt_test_")
        self.workspace = Path(self.temp_dir)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=self.workspace, check=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_assess_playbook_triviality_detects_single_file_routine(self) -> None:
        """Verify that a single-file edit-and-test with no error repair is flagged as trivial."""
        pb = TracePlaybook(
            trace_id="tr_trivial_01",
            agent_source="agy",
            scope="project",
            created_at="2026-11-12T00:00:00Z",
            touched_files=["lib/calc.py"],
            l0_critical_path=[
                {"action": "READ", "target": "lib/calc.py"},
                {"action": "MUTATE", "target": "lib/calc.py"},
                {"action": "ENV_EXEC", "command": "pytest tests/test_calc.py"},
            ],
            l1_cochange_topology=["Primary target module: lib/calc.py"],
            l2_causal_repairs=[],
        )
        is_trivial, reason = assess_playbook_triviality(pb)
        self.assertTrue(is_trivial)
        self.assertIn("Tautological single-file routine", reason)

    def test_assess_playbook_triviality_accepts_l2_repairs_or_multi_file(self) -> None:
        """Verify that multi-file changes or traces with L2 causal repairs are non-trivial."""
        from tmem.models import CausalRepairPair

        # Multi-file
        pb_multi = TracePlaybook(
            trace_id="tr_multi_01",
            agent_source="agy",
            scope="project",
            created_at="2026-11-12T00:00:00Z",
            touched_files=["lib/a.py", "lib/b.py"],
            l0_critical_path=[
                {"action": "MUTATE", "target": "lib/a.py"},
                {"action": "MUTATE", "target": "lib/b.py"},
                {"action": "ENV_EXEC", "command": "pytest"},
            ],
        )
        is_triv_m, _ = assess_playbook_triviality(pb_multi)
        self.assertFalse(is_triv_m)

        # L2 repair
        pb_repair = TracePlaybook(
            trace_id="tr_repair_01",
            agent_source="agy",
            scope="project",
            created_at="2026-11-12T00:00:00Z",
            touched_files=["lib/calc.py"],
            l0_critical_path=[
                {"action": "MUTATE", "target": "lib/calc.py"},
                {"action": "ENV_EXEC", "command": "pytest"},
            ],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="ZeroDivisionError",
                    failed_command="pytest",
                    repair_action="edit lib/calc.py",
                    verification_cmd="pytest",
                )
            ],
        )
        is_triv_r, _ = assess_playbook_triviality(pb_repair)
        self.assertFalse(is_triv_r)

    def test_enhanced_l2_causal_repair_extraction_on_reproduction_scripts(self) -> None:
        """Verify that reproduction commands (python reproduce_issue.py) yielding errors are paired into L2 repairs."""
        session = ParsedSession(
            session_id="sess_rep_01",
            agent_name="swe_bench",
            workspace_root=str(self.workspace),
            user_intents=["Fix TypeError in table header rows formatting"],
            actions=[
                ActionNode(step_idx=1, kind=ActionKind.READ, tool_name="read_file", target_file="lib/rst.py"),
                ActionNode(
                    step_idx=2,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="python reproduce_issue.py",
                    exit_code=1,
                    error_signature="TypeError: can only join an iterable",
                ),
                ActionNode(
                    step_idx=3,
                    kind=ActionKind.MUTATE,
                    tool_name="edit_file",
                    target_file="lib/rst.py",
                    symbols_touched=["write", "header_rows"],
                ),
                ActionNode(
                    step_idx=4,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="python reproduce_issue.py",
                    exit_code=0,
                ),
            ],
        )
        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        self.assertEqual(len(pb.l2_causal_repairs), 1)
        r = pb.l2_causal_repairs[0]
        self.assertIn("TypeError: can only join an iterable", r.error_signature)
        self.assertIn("edit lib/rst.py", r.repair_action)
        self.assertEqual(r.verification_cmd, "python reproduce_issue.py")

    def test_git_ground_truth_boundary_isolation_with_dirty_workspace(self) -> None:
        """Verify that unrelated dirty files in the workspace are NOT absorbed into session's effective files."""
        # 1. Commit initial baseline
        (self.workspace / "unrelated.py").write_text("print('old')\n")
        (self.workspace / "target.py").write_text("def run(): pass\n")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, check=True)

        # 2. Modify unrelated.py (dirty workspace from other work)
        (self.workspace / "unrelated.py").write_text("print('dirty unrelated change')\n")

        # 3. Modify target.py in session
        (self.workspace / "target.py").write_text("def run(): return 42\n")

        session = ParsedSession(
            session_id="sess_boundary_01",
            agent_name="agy",
            workspace_root=str(self.workspace),
            user_intents=["Update run in target.py"],
            actions=[
                ActionNode(step_idx=1, kind=ActionKind.READ, tool_name="read_file", target_file="target.py"),
                ActionNode(step_idx=2, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="target.py", symbols_touched=["run"]),
                ActionNode(step_idx=3, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="python3 -m unittest", exit_code=0),
            ],
        )

        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=True)
        self.assertIsNotNone(pb)
        # target.py must be isolated; unrelated.py MUST NOT be in touched_files or diff!
        self.assertEqual(pb.touched_files, ["target.py"])
        diff_patch = slicer.last_git_diff or ""
        self.assertIn("target.py", diff_patch)
        self.assertNotIn("unrelated.py", diff_patch)

    def test_milestone_prompt_suppression_on_non_code_files(self) -> None:
        """Verify that editing non-code files (like README.md) does NOT trigger milestone prompts."""
        # Step 1: Tool modifies README.md
        handle_agy_hook(
            "post_tool_use",
            {
                "conversationId": "conv_doc_01",
                "workspacePaths": [str(self.workspace)],
                "toolCall": {"name": "edit_file", "args": {"Path": str(self.workspace / "README.md")}},
            },
        )
        # Step 2: Verification command executes
        handle_agy_hook(
            "post_tool_use",
            {
                "conversationId": "conv_doc_01",
                "workspacePaths": [str(self.workspace)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "python3 -m unittest"}},
            },
        )
        # Step 3: Next PreInvocation must NOT nag user with milestone prompt
        out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_doc_01",
                "workspacePaths": [str(self.workspace)],
                "lastUserInput": "Updated documentation, all done.",
            },
        )
        inject_steps = out.get("injectSteps", [])
        m_texts = [
            s.get("systemMessage", {}).get("systemMessage", "")
            for s in inject_steps
        ]
        has_milestone = any("<tmem_milestone_reached>" in t for t in m_texts)
        self.assertFalse(has_milestone, "Milestone prompt should not trigger for non-code files like README.md")

    def test_high_entropy_error_signature_quality_gate(self) -> None:
        """Verify that specific exceptions are accepted and generic runner summaries are rejected."""
        # Genuine diagnostic signatures
        valid_sigs = [
            "TypeError: unexpected keyword argument 'header_rows'",
            "ModuleNotFoundError: No module named 'six'",
            "AssertionError: assert 1 == 2",
            "AttributeError: 'NoneType' object has no attribute 'split'",
            "error[E0432]: unresolved import `std::collections`",
            "Cannot find module 'express'",
        ]
        for sig in valid_sigs:
            self.assertTrue(is_high_entropy_error_signature(sig), f"Should accept high-entropy sig: {sig}")

        # Trivial / tautological / generic failure lines
        invalid_sigs = [
            "FAILED (failures=1)",
            "FAILED (errors=2)",
            "FAIL: test_feature",
            "Command failed (exit 2): python3 -m tmem.cli inspect ...",
            "Process exited with code 1",
            "Exit code 1",
            "Error",
            "failed",
        ]
        for sig in invalid_sigs:
            self.assertFalse(is_high_entropy_error_signature(sig), f"Should reject low-entropy sig: {sig}")

    def test_slicer_rejects_generic_failure_as_l2_invariant(self) -> None:
        """Verify slicer does not create L2 causal pairs from generic 'FAILED (failures=1)' summaries."""
        code_file = self.workspace / "logic.py"
        code_file.write_text("def add(a, b): return a + b\n")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, check=True)

        # Agent runs tests (fails with summary only), edits file, tests pass
        actions = [
            ActionNode(
                step_idx=1,
                kind=ActionKind.ENV_EXEC,
                tool_name="run_command",
                command="pytest",
                exit_code=1,
                error_signature="FAILED (failures=1)",
            ),
            ActionNode(
                step_idx=2,
                kind=ActionKind.MUTATE,
                tool_name="edit_file",
                target_file="logic.py",
                symbols_touched=[],
            ),
            ActionNode(
                step_idx=3,
                kind=ActionKind.ENV_EXEC,
                tool_name="run_command",
                command="pytest",
                exit_code=0,
            ),
        ]
        session = ParsedSession(
            agent_name="agy",
            session_id="test_sess_01",
            workspace_root=str(self.workspace),
            actions=actions,
        )
        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        # Low-entropy FAILED summary must be filtered out of L2
        self.assertEqual(len(pb.l2_causal_repairs), 0, "Generic failure summary should not yield L2 repair")

    def test_slicer_rejects_inline_assignment_as_verification_cmd(self) -> None:
        """Verify slicer rejects inline script assignments like 'r = re.compile(...) && print(...)'."""
        slicer = BackwardTraceSlicer()
        cmd = "r = re.compile(r'test') && print(bool(r.search('python3 test.py')))"
        res = slicer._extract_canonical_verification_cmd(cmd)
        self.assertIsNone(res, "Inline assignment expression should be rejected as verification command")


if __name__ == "__main__":
    unittest.main()
