"""End-to-end verification of tmem: DSH adapter, Backward Slicer, Project Store, and Guards."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tmem.adapters import get_adapter
from tmem.cli import handle_dsh_hook, install_invisible_integration
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore


class TestTmemEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.workspace_a = self.root / "repo_a"
        self.workspace_b = self.root / "repo_b"
        self.workspace_a.mkdir(parents=True)
        self.workspace_b.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_noisy_dsh_transcript(self, ws: Path) -> Path:
        """Create a realistic DSH v4 transcript with dead-ends and error->fix pairs."""
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / "tests").mkdir(parents=True, exist_ok=True)

        (ws / "src" / "auth.py").write_text(
            "def verify_jwt_token(token: str, leeway: int = 30):\n    return True\n",
            encoding="utf-8",
        )
        (ws / "tests" / "test_auth.py").write_text(
            "def test_jwt_leeway():\n    assert True\n",
            encoding="utf-8",
        )
        (ws / "src" / "unrelated.py").write_text("x = 1\n", encoding="utf-8")

        transcript_file = ws / "session.v4.jsonl"
        events = [
            {"version": 4, "id": "sess_e2e_01", "createdAt": 1731225600000},
            {
                "type": "user/message",
                "seq": 1,
                "data": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Fix JWT clock skew verification failure in auth module"}],
                    "source": {"kind": "user"},
                },
            },
            # Noise 1: exploratory ls command
            {
                "type": "tool/call",
                "seq": 2,
                "data": {"callId": "c1", "name": "bash", "arguments": json.dumps({"command": "ls -la"})},
            },
            {
                "type": "tool/result",
                "seq": 3,
                "data": {"message": {"callId": "c1", "content": [{"type": "text", "text": "total 8\n[exit code: 0]"}], "isError": False}},
            },
            # Noise 2: reading an unrelated file
            {
                "type": "tool/call",
                "seq": 4,
                "data": {"callId": "c2", "name": "read", "arguments": json.dumps({"file_path": str(ws / "src/unrelated.py")})},
            },
            {
                "type": "tool/result",
                "seq": 5,
                "data": {"message": {"callId": "c2", "content": [{"type": "text", "text": "1: x = 1"}], "isError": False}},
            },
            # Noise 3: dead mutation on deleted temporary file
            {
                "type": "tool/call",
                "seq": 6,
                "data": {"callId": "c3", "name": "write", "arguments": json.dumps({"file_path": str(ws / "src/dead_temp.py"), "content": "def bad_idea(): pass"})},
            },
            {
                "type": "tool/result",
                "seq": 7,
                "data": {"message": {"callId": "c3", "content": [{"type": "text", "text": "Created file."}], "isError": False}},
            },
            # Effective Read: src/auth.py
            {
                "type": "tool/call",
                "seq": 8,
                "data": {"callId": "c4", "name": "read", "arguments": json.dumps({"file_path": str(ws / "src/auth.py")})},
            },
            {
                "type": "tool/result",
                "seq": 9,
                "data": {"message": {"callId": "c4", "content": [{"type": "text", "text": "1: def verify_jwt_token(token: str): ..."}], "isError": False}},
            },
            # Error Step: pytest fails due to missing dependency
            {
                "type": "tool/call",
                "seq": 10,
                "data": {"callId": "c5", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_auth.py"})},
            },
            {
                "type": "tool/result",
                "seq": 11,
                "data": {"message": {"callId": "c5", "content": [{"type": "text", "text": "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]"}], "isError": False}},
            },
            # Toolchain Fix Step: pip install pyjwt
            {
                "type": "tool/call",
                "seq": 12,
                "data": {"callId": "c6", "name": "bash", "arguments": json.dumps({"command": "pip install pyjwt>=2.8.0"})},
            },
            {
                "type": "tool/result",
                "seq": 13,
                "data": {"message": {"callId": "c6", "content": [{"type": "text", "text": "Successfully installed pyjwt-2.8.0\n[exit code: 0]"}], "isError": False}},
            },
            # Effective Mutations: src/auth.py and tests/test_auth.py
            {
                "type": "tool/call",
                "seq": 14,
                "data": {"callId": "c7", "name": "edit", "arguments": json.dumps({"file_path": str(ws / "src/auth.py"), "new_string": "def verify_jwt_token(token: str, leeway: int = 30):"})},
            },
            {
                "type": "tool/result",
                "seq": 15,
                "data": {"message": {"callId": "c7", "content": [{"type": "text", "text": "Edited file."}], "isError": False}},
            },
            {
                "type": "tool/call",
                "seq": 16,
                "data": {"callId": "c8", "name": "write", "arguments": json.dumps({"file_path": str(ws / "tests/test_auth.py"), "content": "def test_jwt_leeway(): assert True"})},
            },
            {
                "type": "tool/result",
                "seq": 17,
                "data": {"message": {"callId": "c8", "content": [{"type": "text", "text": "Created file."}], "isError": False}},
            },
            # Final Verification Pass
            {
                "type": "tool/call",
                "seq": 18,
                "data": {"callId": "c9", "name": "bash", "arguments": json.dumps({"command": "pytest tests/test_auth.py"})},
            },
            {
                "type": "tool/result",
                "seq": 19,
                "data": {"message": {"callId": "c9", "content": [{"type": "text", "text": "1 passed in 0.03s\n[exit code: 0]"}], "isError": False}},
            },
        ]
        transcript_file.write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8"
        )
        return transcript_file

    def test_backward_slicing_and_project_lifecycle(self) -> None:
        # 1. Install DSH plugin in workspace_a
        info = install_invisible_integration(str(self.workspace_a), ["dsh"])
        self.assertTrue((self.workspace_a / ".dsh" / "plugins" / "tmem-dsh-plugin.mjs").exists())
        self.assertEqual(len(info["agents"]), 1)

        # 2. Parse noisy DSH session & run BackwardTraceSlicer
        t_path = self._create_noisy_dsh_transcript(self.workspace_a)
        adapter = get_adapter("dsh")
        session = adapter.parse_transcript(str(t_path), str(self.workspace_a))
        self.assertEqual(len(session.actions), 9)

        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        assert pb is not None

        # Verify Dead Mutation Elimination & Read Shrinking
        self.assertEqual(pb.touched_files, ["src/auth.py", "tests/test_auth.py"])
        targets_in_l0 = [s.get("target") for s in pb.l0_critical_path if s.get("target")]
        self.assertNotIn("src/unrelated.py", targets_in_l0)
        self.assertNotIn("src/dead_temp.py", targets_in_l0)
        self.assertIn("src/auth.py", targets_in_l0)

        # Verify L2 Causal Repair Extraction
        self.assertEqual(len(pb.l2_causal_repairs), 1)
        self.assertIn("ModuleNotFoundError: No module named 'pyjwt'", pb.l2_causal_repairs[0].error_signature)
        self.assertTrue(pb.l2_causal_repairs[0].is_env_or_toolchain)

        # 3. Save to Project Store
        store_a = DualTierMemoryStore(str(self.workspace_a))
        proj_id, _ = store_a.save_playbook(pb)
        self.assertTrue(proj_id.startswith("tr_"))

        # 4. Verify DSH pre_invocation with explicit file anchor (Guards Pass -> L0_FULL)
        hook_out = handle_dsh_hook(
            "pre_invocation",
            {
                "sessionId": "conv_2",
                "workspaceRoot": str(self.workspace_a),
                "lastUserInput": "Update verify_jwt_token in src/auth.py",
            },
        )
        sys_msg = hook_out.get("injectedContext", "")
        self.assertIn("Tier: L0_FULL", sys_msg)
        self.assertIn("Verified L0 Critical Path (Guards Passed)", sys_msg)

        # 5. Simulate Environment Drift: Delete src/auth.py -> Post-Execute L2 error interception degrades to L2_INVARIANT
        (self.workspace_a / "src" / "auth.py").unlink()
        degraded_out = handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_3",
                "workspaceRoot": str(self.workspace_a),
                "toolCall": {"name": "bash", "args": {"command": "pytest tests/test_auth.py"}},
                "resultText": "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]",
            },
        )
        deg_msg = degraded_out.get("additionalContext", "")
        self.assertIn("L0 concrete paths drifted (missing_file:src/auth.py)", deg_msg)
        self.assertIn("L2 Causal Repair", deg_msg)

        # 6. Repository Isolation in Workspace B (Zero local traces -> No memory injected)
        cross_out = handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_b1",
                "workspaceRoot": str(self.workspace_b),
                "toolCall": {"name": "bash", "args": {"command": "pytest tests/test_auth.py"}},
                "resultText": "ModuleNotFoundError: No module named 'pyjwt'\n[exit code: 1]",
            },
        )
        self.assertEqual(cross_out, {})

    def test_milestone_prompt_and_slash_commands(self) -> None:
        t_path = self._create_noisy_dsh_transcript(self.workspace_a)

        # 1. Simulate DSH post_tool_use: file edit followed by passing test command
        handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_m1",
                "workspaceRoot": str(self.workspace_a),
                "toolCall": {"name": "edit", "args": {"file_path": str(self.workspace_a / "src/auth.py")}},
            },
        )
        milestone_out = handle_dsh_hook(
            "post_tool_use",
            {
                "sessionId": "conv_m1",
                "workspaceRoot": str(self.workspace_a),
                "toolCall": {"name": "bash", "args": {"command": "pytest tests/test_auth.py"}},
                "resultText": "1 passed\n[exit code: 0]",
            },
        )
        m_msg = milestone_out.get("additionalContext", "")
        self.assertIn("<tmem_milestone_reached>", m_msg)
        self.assertIn("/tmem", m_msg)

        # 2. User runs `/tmem Fix JWT leeway validation` via DSH command hook
        from tmem.cli import execute_capture

        def _cap(**kwargs):
            kwargs["transcript_path"] = str(t_path)
            return execute_capture(**kwargs)

        cmd_out = handle_dsh_hook(
            "command",
            {
                "sessionId": "conv_m1",
                "workspaceRoot": str(self.workspace_a),
                "args": "Fix JWT leeway validation",
            },
            capture_fn=_cap,
        )
        self.assertEqual(cmd_out["kind"], "success")
        self.assertIn('"scope": "project"', cmd_out["text"])
        self.assertIn("Fix JWT leeway validation", cmd_out["text"])

        # 3. Verify `/tmem status` command
        stat_out = handle_dsh_hook(
            "command",
            {
                "sessionId": "conv_m1",
                "workspaceRoot": str(self.workspace_a),
                "args": "status",
            },
        )
        self.assertEqual(stat_out["kind"], "success")
        self.assertIn("Project Tier", stat_out["text"])
        self.assertIn("1 trace(s)", stat_out["text"])


if __name__ == "__main__":
    unittest.main()
