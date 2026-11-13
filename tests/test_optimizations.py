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
from tmem.cli import execute_capture, handle_dsh_hook
from tmem.models import ActionKind, ActionNode, CausalRepairPair, GuardProbe, TracePlaybook
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
            agent_source="dsh",
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
            agent_source="dsh",
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
            agent_source="dsh",
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
            agent_name="dsh",
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
        handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_doc_01",
                "workspaceRoot": str(self.workspace),
                "toolCall": {"name": "edit", "args": {"file_path": str(self.workspace / "README.md")}},
            },
        )
        # Step 2: Verification command executes
        post_res = handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_doc_01",
                "workspaceRoot": str(self.workspace),
                "toolCall": {"name": "bash", "args": {"command": "python3 -m unittest"}},
                "resultText": "OK\n[exit code: 0]",
            },
        )
        self.assertNotIn("<tmem_milestone_reached>", post_res.get("additionalContext", ""))
        # Step 3: Next PreInvocation must NOT nag user with milestone prompt
        out = handle_dsh_hook(
            "pre_invocation",
            {
                "sessionId": "conv_doc_01",
                "workspaceRoot": str(self.workspace),
                "lastUserInput": "Updated documentation, all done.",
            },
        )
        self.assertNotIn("<tmem_milestone_reached>", out.get("injectedContext", ""))

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
            agent_name="dsh",
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

    def test_p0_2_tr_37a01e0db5_fixture_produces_zero_l2_repairs(self) -> None:
        """Roadmap 0.2: tr_37a01e0db5 fixture must produce 0 L2 causal repairs."""
        fixture_path = Path(__file__).parent / "fixtures" / "tr_37a01e0db5.raw.json"
        self.assertTrue(fixture_path.exists(), f"Fixture missing: {fixture_path}")

        with fixture_path.open("r", encoding="utf-8") as f:
            raw_data = json.load(f)

        actions = [
            ActionNode(
                step_idx=a["step_idx"],
                kind=ActionKind(a["kind"]),
                tool_name=a.get("tool_name") or "run_command",
                target_file=a.get("target_file"),
                command=a.get("command"),
                exit_code=a.get("exit_code"),
                error_signature=a.get("error_signature"),
                output_snippet=a.get("output_snippet"),
                raw_output=a.get("raw_output"),
                rationale=a.get("rationale"),
                symbols_touched=a.get("symbols_touched") or [],
            )
            for a in raw_data["actions"]
        ]

        slicer = BackwardTraceSlicer()
        repairs, _ = slicer._extract_causal_repairs_with_indices(
            actions=actions,
            effective_files={
                "README.md",
                "README_EN.md",
                "tests/test_optimizations.py",
                "tmem/adapters/base.py",
                "tmem/evaluate.py",
                "tmem/slicer.py",
            },
            workspace_root=str(self.workspace),
        )
        self.assertEqual(len(repairs), 0, "tr_37a01e0db5 fixture must produce exactly 0 L2 repairs")

    def test_p0_3_confidence_calibration_capped_at_0_7(self) -> None:
        """Roadmap 0.3: Single-session playbook confidence must be <= 0.70."""
        code_file = self.workspace / "logic.py"
        code_file.write_text("def add(a, b): return a + b\n")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, check=True)

        actions = [
            ActionNode(
                step_idx=1,
                kind=ActionKind.MUTATE,
                tool_name="edit_file",
                target_file="logic.py",
                symbols_touched=["add"],
            ),
            ActionNode(
                step_idx=2,
                kind=ActionKind.ENV_EXEC,
                tool_name="run_command",
                command="pytest",
                exit_code=0,
            ),
        ]
        session = ParsedSession(
            agent_name="dsh",
            session_id="test_conf_sess",
            workspace_root=str(self.workspace),
            user_intents=["Implement add function in logic.py"],
            actions=actions,
        )
        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        assert pb is not None
        self.assertLessEqual(pb.confidence, 0.70, "Single-session confidence must be capped at 0.70")
        self.assertGreaterEqual(pb.confidence, 0.30)

    def test_p0_4_meta_review_session_marked_as_non_task_and_not_stored(self) -> None:
        """Roadmap 0.4: Meta review sessions must be marked non_task and produce None."""
        actions = [
            ActionNode(
                step_idx=1,
                kind=ActionKind.READ,
                tool_name="read_file",
                target_file="tmem/slicer.py",
            ),
            ActionNode(
                step_idx=2,
                kind=ActionKind.MUTATE,
                tool_name="edit_file",
                target_file="README.md",
            ),
        ]
        session = ParsedSession(
            agent_name="dsh",
            session_id="test_meta_sess",
            workspace_root=str(self.workspace),
            user_intents=["务实的评价下本项目，客观甚至刻薄的给与评分", "根据反馈优化项目"],
            actions=actions,
        )
        slicer = BackwardTraceSlicer()
        intent, summary = slicer._synthesize_semantic_topic(
            session=session,
            sorted_files=["README.md"],
            sorted_symbols=[],
            commit_subject=None,
        )
        self.assertEqual(intent, "non_task")
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNone(pb, "Meta-request session should not be persisted into memory")

    def test_p0_5_memories_table_rendering_preserves_structure_with_multiline_and_pipes(self) -> None:
        """Roadmap 0.5: MEMORIES.md table rendering must escape pipes and collapse newlines."""
        from tmem.mem_doc import render_memories_markdown
        multiline_cmd = "python3 -c '\nimport os\nfor i in range(5):\n    print(\"foo | bar\")\n'"
        repair = CausalRepairPair(
            error_signature="TypeError: bad | signature\nline 2",
            failed_command=multiline_cmd,
            repair_action="edit a.py | edit b.py",
            verification_cmd="pytest -k 'test | pipe'",
        )
        pb = TracePlaybook(
            trace_id="tr_multi_cmd",
            agent_source="dsh",
            l2_causal_repairs=[repair],
        )
        md = render_memories_markdown([pb], str(self.workspace))
        # Find the row containing the multiline command in Section 2
        l2_rows = [ln for ln in md.splitlines() if "python3 -c" in ln and ln.startswith("|")]
        self.assertEqual(len(l2_rows), 1, "Multiline command must render on exactly a single table row")
        l2_row = l2_rows[0]
        self.assertNotIn("\n", l2_row)
        # Verify columns in the L2 table row (should have 4 columns separated by unescaped pipes)
        # Using regex to split by unescaped pipe: (?<!\\)\|
        import re
        cols = [c.strip() for c in re.split(r"(?<!\\)\|", l2_row.strip("|"))]
        self.assertEqual(len(cols), 4, f"L2 table row must have 4 columns, got {len(cols)}")

    def test_p0_6_agents_rules_do_not_contain_skip_exploration(self) -> None:
        """Roadmap 0.6: AGENTS.md rules must not instruct agents to skip exploration."""
        agents_md = Path(__file__).parent.parent / "AGENTS.md"
        content = agents_md.read_text(encoding="utf-8")
        self.assertNotIn("redundant exploratory `grep`/`find` scans", content)
        self.assertNotIn("不要再 grep/find 探索", content)
        self.assertIn("clues", content.lower())

    def test_phase2_non_canonical_diff_cmd_produces_zero_l2_repairs(self) -> None:
        """Phase 2: Non-canonical shell commands like `diff -u` with stderr/diff-output must NOT become L2 repairs."""
        actions = [
            ActionNode(
                step_idx=1,
                kind=ActionKind.ENV_EXEC,
                tool_name="run_command",
                command="diff -u src/a.py src/b.py",
                exit_code=1,
                error_signature="AssertionError: line diff",
            ),
            ActionNode(
                step_idx=2,
                kind=ActionKind.MUTATE,
                tool_name="edit_file",
                target_file="src/a.py",
                symbols_touched=["compute"],
            ),
            ActionNode(
                step_idx=3,
                kind=ActionKind.ENV_EXEC,
                tool_name="run_command",
                command="pytest tests/test_a.py",
                exit_code=0,
            ),
        ]
        session = ParsedSession(
            agent_name="dsh",
            session_id="sess_diff_false_l2",
            workspace_root=str(self.workspace),
            user_intents=["Compare and fix src/a.py"],
            actions=actions,
        )
        pb = BackwardTraceSlicer().slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        self.assertEqual(pb.l2_causal_repairs, [])

    def test_phase2_l2_same_origin_command_matching(self) -> None:
        """Phase 2: `test_a.py` fail -> `test_b.py` pass must be rejected; same/broader runner passes are accepted."""
        # Case A: Mismatched test file target (test_a.py -> test_b.py)
        mismatched = ParsedSession(
            agent_name="dsh",
            session_id="sess_mismatch",
            workspace_root=str(self.workspace),
            user_intents=["Fix src/a.py test failure"],
            actions=[
                ActionNode(
                    step_idx=1,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="pytest tests/test_a.py",
                    exit_code=1,
                    error_signature="ValueError: bad input",
                ),
                ActionNode(
                    step_idx=2,
                    kind=ActionKind.MUTATE,
                    tool_name="edit_file",
                    target_file="src/a.py",
                    symbols_touched=["parse"],
                ),
                ActionNode(
                    step_idx=3,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="pytest tests/test_b.py",
                    exit_code=0,
                ),
            ],
        )
        pb_mismatch = BackwardTraceSlicer().slice_session(mismatched, use_git_ground_truth=False)
        self.assertIsNotNone(pb_mismatch)
        self.assertEqual(pb_mismatch.l2_causal_repairs, [])

        # Case B: Same runner broader suite (pytest tests/test_a.py -> pytest)
        broader = ParsedSession(
            agent_name="dsh",
            session_id="sess_broader",
            workspace_root=str(self.workspace),
            user_intents=["Fix src/a.py test failure"],
            actions=[
                ActionNode(
                    step_idx=1,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="pytest tests/test_a.py",
                    exit_code=1,
                    error_signature="ValueError: bad input",
                ),
                ActionNode(
                    step_idx=2,
                    kind=ActionKind.MUTATE,
                    tool_name="edit_file",
                    target_file="src/a.py",
                    symbols_touched=["parse"],
                ),
                ActionNode(
                    step_idx=3,
                    kind=ActionKind.ENV_EXEC,
                    tool_name="run_command",
                    command="pytest",
                    exit_code=0,
                ),
            ],
        )
        pb_broader = BackwardTraceSlicer().slice_session(broader, use_git_ground_truth=False)
        self.assertIsNotNone(pb_broader)
        self.assertEqual(len(pb_broader.l2_causal_repairs), 1)
        self.assertEqual(pb_broader.l2_causal_repairs[0].verification_cmd, "pytest")

    def test_phase2_ast_diff_line_range_and_guard_priority(self) -> None:
        """Phase 2: AST+line-range extracts enclosing def even when def line wasn't modified, prioritizing source files."""
        import subprocess

        subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.workspace, check=True)

        src_dir = self.workspace / "src"
        src_dir.mkdir(exist_ok=True)
        py_file = src_dir / "engine.py"
        readme = self.workspace / "README.md"
        py_file.write_text(
            "def compute_tax(amount):\n    rate = 0.1\n    return amount * rate\n",
            encoding="utf-8",
        )
        readme.write_text("# Engine\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, check=True, capture_output=True)

        # Modify ONLY body line 2 (not `def compute_tax`) and README.md
        py_file.write_text(
            "def compute_tax(amount):\n    rate = 0.2\n    return amount * rate\n",
            encoding="utf-8",
        )
        readme.write_text("# Engine v2\n", encoding="utf-8")

        actions = [
            ActionNode(step_idx=1, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="README.md"),
            ActionNode(step_idx=2, kind=ActionKind.MUTATE, tool_name="edit_file", target_file="src/engine.py"),
            ActionNode(step_idx=3, kind=ActionKind.ENV_EXEC, tool_name="run_command", command="pytest", exit_code=0),
        ]
        session = ParsedSession(
            agent_name="dsh",
            session_id="sess_ast_guard",
            workspace_root=str(self.workspace),
            user_intents=["Update tax rate in engine"],
            actions=actions,
        )
        pb = BackwardTraceSlicer().slice_session(session, use_git_ground_truth=True)
        self.assertIsNotNone(pb)
        self.assertIn("compute_tax", pb.touched_symbols)
        # First file_exists guard must prioritize source file over README.md
        file_guards = [g.target for g in pb.guards if g.probe_type == "file_exists"]
        self.assertEqual(file_guards[0], "src/engine.py")
        sym_guards = [(g.target, g.expected) for g in pb.guards if g.probe_type == "symbol_present"]
        self.assertIn(("src/engine.py", "compute_tax"), sym_guards)

    def test_phase2_statistical_cochange_ignores_bulk_and_detects_recurring(self) -> None:
        """Phase 2: Statistical co-change skips >10-file bulk commits and extracts >=2x recurring pairs."""
        import subprocess
        from tmem.cochange import compute_cochange_stats

        subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.workspace, check=True)

        # Commit 1: bulk 12-file commit (must be ignored by L1 topology)
        (self.workspace / "README.md").write_text("v1\n", encoding="utf-8")
        for idx in range(11):
            (self.workspace / f"mod_{idx}.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "bulk"], cwd=self.workspace, check=True, capture_output=True)

        # Commit 2 & 3: recurring co-edit between a.py and b.py
        for rev in ("v2", "v3"):
            (self.workspace / "mod_0.py").write_text(f"# {rev}\n", encoding="utf-8")
            (self.workspace / "mod_1.py").write_text(f"# {rev}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
            subprocess.run(["git", "commit", "-m", f"pair {rev}"], cwd=self.workspace, check=True, capture_output=True)

        pairs = compute_cochange_stats(str(self.workspace))
        pair_tuples = {(p.file_a, p.file_b) for p in pairs}
        self.assertIn(("mod_0.py", "mod_1.py"), pair_tuples)
        for a, b in pair_tuples:
            self.assertNotIn("README.md", (a, b))

    def test_phase2_default_deny_skips_long_custom_intent_and_saves_env_facts(self) -> None:
        """Phase 2: Default-deny rejects single-file routine even with long custom_intent, while saving env_facts."""
        from tmem.cli import execute_capture
        from tmem.mem_doc import extract_micro_skills

        def _write_dsh_routine(path: Path, prompt: str) -> None:
            entries = [
                {"version": 4, "id": path.stem, "createdAt": 1731225600000},
                {
                    "type": "user/message",
                    "seq": 1,
                    "data": {"role": "user", "content": [{"type": "text", "text": prompt}], "source": {"kind": "user"}},
                },
                {
                    "type": "tool/call",
                    "seq": 2,
                    "data": {"callId": "c1", "name": "edit", "arguments": json.dumps({"file_path": "src/helper.py"})},
                },
                {
                    "type": "tool/result",
                    "seq": 3,
                    "data": {"message": {"callId": "c1", "content": [{"type": "text", "text": "Edited"}], "isError": False}},
                },
                {
                    "type": "tool/call",
                    "seq": 4,
                    "data": {"callId": "c2", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_helper.py"})},
                },
                {
                    "type": "tool/result",
                    "seq": 5,
                    "data": {"message": {"callId": "c2", "content": [{"type": "text", "text": "1 passed\n[exit code: 0]"}], "isError": False}},
                },
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

        transcript = self.workspace / "routine.v4.jsonl"
        _write_dsh_routine(transcript, "Refactor helper docstring")

        res1 = execute_capture(
            workspace_root=str(self.workspace),
            agent_name="dsh",
            transcript_path=str(transcript),
            custom_intent="Long custom intent exceeding ten chars to verify bypass removal",
            force=False,
        )
        self.assertEqual(res1["status"], "skipped_trivial")
        self.assertFalse(list((self.workspace / ".tmem" / "traces").glob("*.json")) if (self.workspace / ".tmem" / "traces").exists() else False)

        # Second session with same verified cmd enriches env_facts across >=2 sources
        transcript2 = self.workspace / "routine2.v4.jsonl"
        _write_dsh_routine(transcript2, "Second routine helper edit")
        res2 = execute_capture(
            workspace_root=str(self.workspace),
            agent_name="dsh",
            transcript_path=str(transcript2),
            force=False,
        )
        self.assertEqual(res2["status"], "skipped_trivial")

        skills = extract_micro_skills([], min_traces=2, workspace_root=str(self.workspace))
        self.assertIn("pytest tests/test_helper.py", skills)


if __name__ == "__main__":
    unittest.main()
