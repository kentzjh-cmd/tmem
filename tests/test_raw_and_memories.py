"""
Unit and integration tests for raw execution trace storage, progressive disclosure,
living memory index (MEMORIES.md / MEM.md), and forensic replay/diff CLI subcommands.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tmem.cli import main as cli_main
from tmem.cli import SLASH_TMEM_RE, handle_agy_hook
from tmem.mem_doc import render_memories_markdown, sync_memories_file
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    RawExecutionTrace,
    TracePlaybook,
)
from tmem.raw_storage import (
    RawTraceStore,
    format_progressive_view,
    format_raw_trace_diff,
    format_raw_trace_replay,
)
from tmem.store import DualTierMemoryStore


class TestRawAndMemories(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="tmem_raw_test_")
        self.workspace = Path(self.test_dir)
        self.raw_dir = self.workspace / ".tmem" / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

        # Create sample actions
        self.action1 = ActionNode(
            step_idx=1,
            kind=ActionKind.ENV_EXEC,
            tool_name="bash",
            command="pytest tests/test_calc.py",
            exit_code=1,
            error_signature="ModuleNotFoundError: No module named 'calc'",
            raw_input={"command": "pytest tests/test_calc.py"},
            raw_output="ModuleNotFoundError: No module named 'calc'",
        )
        self.action2 = ActionNode(
            step_idx=2,
            kind=ActionKind.READ,
            tool_name="view",
            target_file="tests/test_calc.py",
            raw_input={"path": "tests/test_calc.py"},
            raw_output="import calc\ndef test_add(): assert calc.add(1, 2) == 3",
        )
        self.action3 = ActionNode(
            step_idx=3,
            kind=ActionKind.MUTATE,
            tool_name="create",
            target_file="src/calc.py",
            raw_input={"path": "src/calc.py", "content": "def add(a, b): return a + b\n"},
            raw_output="File created successfully.",
        )
        self.action4 = ActionNode(
            step_idx=4,
            kind=ActionKind.ENV_EXEC,
            tool_name="bash",
            command="pytest tests/test_calc.py",
            exit_code=0,
            raw_input={"command": "pytest tests/test_calc.py"},
            raw_output="1 passed in 0.05s",
        )

        self.sample_diff = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/calc.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def add(a, b):\n"
            "+    return a + b\n"
        )

        self.sample_playbook = TracePlaybook(
            trace_id="tr_unit_test_001",
            agent_source="dsh",
            scope="project",
            created_at="2026-11-11T12:00:00Z",
            problem_intent="Fix missing calc module and pass unit tests",
            semantic_summary="Created src/calc.py implementing add function to resolve import error",
            error_signatures=["ModuleNotFoundError: No module named 'calc'"],
            touched_files=["src/calc.py", "tests/test_calc.py"],
            touched_symbols=["add", "test_add"],
            guards=[
                GuardProbe(
                    probe_type="file_exists",
                    target="tests/test_calc.py",
                    expected="",
                )
            ],
            l0_critical_path=[
                {"step": 3, "action": "MUTATE", "target": "src/calc.py"},
                {"step": 4, "action": "EXEC", "command": "pytest tests/test_calc.py"},
            ],
            l1_cochange_topology=["tests/test_calc.py <-> src/calc.py"],
            l1_verification_cmds=["pytest tests/test_calc.py"],
            l2_causal_repairs=[
                CausalRepairPair(
                    error_signature="ModuleNotFoundError: No module named 'calc'",
                    failed_command="pytest tests/test_calc.py",
                    repair_action="MUTATE(src/calc.py)",
                    verification_cmd="pytest tests/test_calc.py",
                )
            ],
            confidence=0.98,
            hit_count=1,
            status="active",
            raw_steps_count=4,
            sliced_steps_count=2,
            raw_trace_path=".tmem/raw/tr_unit_test_001.raw.json",
        )

        self.sample_raw_trace = RawExecutionTrace(
            trace_id="tr_unit_test_001",
            session_id="sess_unit_test_001",
            agent_source="unit_test",
            workspace_root=str(self.workspace),
            created_at="2026-11-11T12:00:00Z",
            user_intents=["Fix missing calc module and pass unit tests"],
            assistant_conclusions=["All unit tests passed"],
            total_steps=4,
            actions=[
                self.action1.to_dict(),
                self.action2.to_dict(),
                self.action3.to_dict(),
                self.action4.to_dict(),
            ],
            sliced_step_indices=[3, 4],
            diff_patch=self.sample_diff,
            metadata={"test_run": True},
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_raw_trace_store_lifecycle(self) -> None:
        """Test RawTraceStore saving, loading, listing, and deletion."""
        store = RawTraceStore(str(self.workspace))

        # Save trace
        saved_path = store.save_raw_trace(self.sample_raw_trace)
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.name, "tr_unit_test_001.raw.json")

        # Load trace
        loaded = store.load_raw_trace("tr_unit_test_001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.trace_id, "tr_unit_test_001")
        self.assertEqual(len(loaded.actions), 4)
        self.assertEqual(loaded.actions[0].get("raw_output"), "ModuleNotFoundError: No module named 'calc'")
        self.assertEqual(loaded.diff_patch, self.sample_diff)
        self.assertEqual(loaded.sliced_step_indices, [3, 4])

        # List traces
        traces = store.list_raw_traces()
        trace_ids = [t["trace_id"] for t in traces]
        self.assertIn("tr_unit_test_001", trace_ids)

        # Delete trace
        deleted = store.delete_raw_trace("tr_unit_test_001")
        self.assertTrue(deleted)
        self.assertIsNone(store.load_raw_trace("tr_unit_test_001"))

    def test_format_raw_trace_replay_and_diff(self) -> None:
        """Test forensic terminal replay playback and unified diff rendering."""
        # Replay formatting
        replay_output = format_raw_trace_replay(self.sample_raw_trace)
        self.assertIn("Forensic Trace Replay", replay_output)
        self.assertIn("tr_unit_test_001", replay_output)
        self.assertIn("NOISE PRUNED", replay_output)
        self.assertIn("CRITICAL PATH", replay_output)
        self.assertIn("pytest tests/test_calc.py", replay_output)
        self.assertIn("src/calc.py", replay_output)

        # Diff formatting
        diff_output = format_raw_trace_diff(self.sample_raw_trace)
        self.assertIn("diff --git a/src/calc.py", diff_output)
        self.assertIn("+def add(a, b):", diff_output)

    def test_progressive_disclosure_levels(self) -> None:
        """Test format_progressive_view for all 4 disclosure tiers."""
        # Level 1: Index
        idx_view = format_progressive_view(
            self.sample_playbook, self.sample_raw_trace, level="index"
        )
        self.assertIn("[tr_unit_test_001]", idx_view)
        self.assertIn("Scope: project", idx_view)
        self.assertIn("Fix missing calc module", idx_view)

        # Level 2: Playbook
        pb_view = format_progressive_view(
            self.sample_playbook, self.sample_raw_trace, level="playbook"
        )
        self.assertIn("Playbook: `tr_unit_test_001`", pb_view)
        self.assertIn("L0 Critical Path", pb_view)
        self.assertIn("L1 Co-Change Topology", pb_view)
        self.assertIn("L2 Universal Causal Repairs", pb_view)
        self.assertIn("ModuleNotFoundError", pb_view)

        # Level 3: Raw
        raw_view = format_progressive_view(
            self.sample_playbook, self.sample_raw_trace, level="raw"
        )
        self.assertIn("Forensic Trace Replay", raw_view)
        self.assertIn("CRITICAL PATH", raw_view)

        # Level 4: Diff
        diff_view = format_progressive_view(
            self.sample_playbook, self.sample_raw_trace, level="diff"
        )
        self.assertIn("diff --git a/src/calc.py", diff_view)

    def test_living_memory_index_sync_and_symlink(self) -> None:
        """Test MEMORIES.md and MEM.md generation and atomic maintenance."""
        target_path = sync_memories_file(
            workspace_root=str(self.workspace),
            playbooks=[self.sample_playbook],
        )

        self.assertTrue(target_path.exists())
        self.assertEqual(target_path.name, "MEMORIES.md")

        memories_content = target_path.read_text(encoding="utf-8")
        self.assertIn("Project Execution Memories", memories_content)
        self.assertIn("tr_unit_test_001", memories_content)
        self.assertIn("Fix missing calc module", memories_content)
        self.assertIn("pytest tests/test_calc.py", memories_content)
        self.assertIn("ModuleNotFoundError", memories_content)
        self.assertIn("tests/test_calc.py <-> src/calc.py", memories_content)
        self.assertIn("python3 -m tmem.cli inspect", memories_content)

        # Verify symlink MEM.md
        mem_alias = self.workspace / "MEM.md"
        self.assertTrue(mem_alias.exists())
        if hasattr(os, "readlink"):
            # Check symlink or content match
            self.assertTrue(mem_alias.is_symlink() or mem_alias.read_text() == memories_content)

    def test_dual_tier_store_integration_with_raw_and_memories(self) -> None:
        """Test DualTierMemoryStore automatic raw trace binding and MEMORIES.md synchronization."""
        store = DualTierMemoryStore(workspace_root=str(self.workspace))

        # First save raw trace
        raw_store = RawTraceStore(str(self.workspace))
        raw_store.save_raw_trace(self.sample_raw_trace)

        # Save playbook into project store
        store.save_playbook(self.sample_playbook, target_scope="project")

        # Check raw trace path updated
        playbooks = store.get_all_project_playbooks()
        self.assertEqual(len(playbooks), 1)
        loaded_pb = playbooks[0]
        self.assertEqual(loaded_pb.trace_id, "tr_unit_test_001")
        self.assertTrue(loaded_pb.raw_trace_path.endswith("tr_unit_test_001.raw.json"))
        self.assertIsNotNone(loaded_pb.raw_trace_hash)

        # Check MEMORIES.md automatically generated
        mem_file = self.workspace / "MEMORIES.md"
        self.assertTrue(mem_file.exists())
        self.assertIn("tr_unit_test_001", mem_file.read_text(encoding="utf-8"))

        # Test get_raw_trace helper
        retrieved_raw = store.get_raw_trace("tr_unit_test_001")
        self.assertIsNotNone(retrieved_raw)
        self.assertEqual(len(retrieved_raw.actions), 4)

        # Test delete_playbook cleans raw trace and refreshes MEMORIES.md
        del_res = store.delete_playbook("tr_unit_test_001")
        self.assertTrue(del_res["deleted"])
        self.assertIsNone(store.get_raw_trace("tr_unit_test_001"))
        self.assertNotIn("tr_unit_test_001", mem_file.read_text(encoding="utf-8"))

    def test_cli_subcommands(self) -> None:
        """Test CLI subcommands: inspect, replay, diff, sync."""
        store = DualTierMemoryStore(workspace_root=str(self.workspace))
        raw_store = RawTraceStore(str(self.workspace))
        raw_store.save_raw_trace(self.sample_raw_trace)
        store.save_playbook(self.sample_playbook, target_scope="project")

        # Test inspect --level index
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main(["inspect", "tr_unit_test_001", "--level", "index", "--workspace", str(self.workspace)])
        self.assertEqual(ret, 0)
        self.assertIn("[tr_unit_test_001]", buf.getvalue())
        self.assertIn("Fix missing calc module", buf.getvalue())

        # Test inspect --level playbook
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main(["inspect", "tr_unit_test_001", "--level", "playbook", "--workspace", str(self.workspace)])
        self.assertEqual(ret, 0)
        self.assertIn("Playbook: `tr_unit_test_001`", buf.getvalue())

        # Test replay
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main(["replay", "tr_unit_test_001", "--workspace", str(self.workspace)])
        self.assertEqual(ret, 0)
        self.assertIn("Forensic Trace Replay", buf.getvalue())
        self.assertIn("CRITICAL PATH", buf.getvalue())

        # Test diff
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main(["diff", "tr_unit_test_001", "--workspace", str(self.workspace)])
        self.assertEqual(ret, 0)
        self.assertIn("diff --git a/src/calc.py", buf.getvalue())

        # Test sync
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main(["sync", "--workspace", str(self.workspace)])
        self.assertEqual(ret, 0)
        self.assertIn("synchronized", buf.getvalue())
        self.assertIn("MEMORIES.md", buf.getvalue())

    def test_slash_command_regex_and_agy_hook(self) -> None:
        """Test slash command regex and hook dispatch for new actions."""
        # Regex tests
        m_inspect = SLASH_TMEM_RE.search("/tmem inspect tr_123 --level raw")
        self.assertIsNotNone(m_inspect)
        self.assertEqual(m_inspect.group(1), "inspect")
        self.assertEqual(m_inspect.group(2).strip(), "tr_123 --level raw")

        m_replay = SLASH_TMEM_RE.search("/tmem replay tr_abc")
        self.assertIsNotNone(m_replay)
        self.assertEqual(m_replay.group(1), "replay")
        self.assertEqual(m_replay.group(2).strip(), "tr_abc")

        m_diff = SLASH_TMEM_RE.search("/tmem diff tr_abc")
        self.assertIsNotNone(m_diff)
        self.assertEqual(m_diff.group(1), "diff")
        self.assertEqual(m_diff.group(2).strip(), "tr_abc")

        m_sync = SLASH_TMEM_RE.search("/tmem sync")
        self.assertIsNotNone(m_sync)
        self.assertEqual(m_sync.group(1), "sync")

        # Setup workspace for hook
        store = DualTierMemoryStore(workspace_root=str(self.workspace))
        raw_store = RawTraceStore(str(self.workspace))
        raw_store.save_raw_trace(self.sample_raw_trace)
        store.save_playbook(self.sample_playbook, target_scope="project")

        # Test agy hook execution for inspect (both positional level and --level flag)
        payload = {
            "conversationId": "conv_test_1",
            "lastUserInput": "/tmem inspect tr_unit_test_001 index",
            "workspacePaths": [str(self.workspace)],
        }
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("[tr_unit_test_001]", msg)

        payload["lastUserInput"] = "/tmem inspect tr_unit_test_001 --level raw"
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Forensic Trace Replay", msg)

        payload["lastUserInput"] = "/tmem inspect"
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("not found in project memory", msg)

        # Test agy hook execution for replay
        payload["lastUserInput"] = "/tmem replay tr_unit_test_001"
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Forensic Trace Replay", msg)

        # Test agy hook execution for diff
        payload["lastUserInput"] = "/tmem diff tr_unit_test_001"
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("diff --git a/src/calc.py", msg)

        # Test agy hook execution for sync
        payload["lastUserInput"] = "/tmem sync"
        hook_resp = handle_agy_hook("pre_invocation", payload)
        msg = hook_resp["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Command `/tmem sync` completed successfully", msg)


if __name__ == "__main__":
    unittest.main()
