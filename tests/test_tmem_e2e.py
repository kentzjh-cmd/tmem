"""End-to-end verification of tmem: Agy adapter, Backward Slicer, Dual-Tier Store, and Guards."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tmem.adapters import get_adapter
from tmem.cli import handle_agy_hook, install_invisible_integration
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore


class TestTmemEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.workspace_a = self.root / "repo_a"
        self.workspace_b = self.root / "repo_b"
        self.global_dir = self.root / "global_tmem"
        self.workspace_a.mkdir(parents=True)
        self.workspace_b.mkdir(parents=True)
        os.environ["TMEM_GLOBAL_DIR"] = str(self.global_dir)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_noisy_agy_transcript(self, ws: Path) -> Path:
        """Create a realistic 10-step agy transcript.jsonl with dead-ends and error->fix pairs."""
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / "tests").mkdir(parents=True, exist_ok=True)

        # Final surviving files on disk (Ground Truth)
        (ws / "src" / "auth.py").write_text(
            "def verify_jwt_token(token: str, leeway: int = 30):\n    return True\n",
            encoding="utf-8",
        )
        (ws / "tests" / "test_auth.py").write_text(
            "def test_jwt_leeway():\n    assert True\n",
            encoding="utf-8",
        )
        # Unrelated file that was only read
        (ws / "src" / "unrelated.py").write_text("x = 1\n", encoding="utf-8")

        transcript_file = ws / "transcript.jsonl"
        events = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\nFix JWT clock skew verification failure in auth module\n</USER_REQUEST>",
            },
            # Noise 1: exploratory ls command
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"ls -la"'}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Command exited with code 0.\n\nCommand output:\ntotal 8",
            },
            # Noise 2: reading an unrelated file
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": f'"{ws / "src/unrelated.py"}"'}}
                ],
            },
            {
                "step_index": 4,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "1: x = 1",
            },
            # Noise 3: dead mutation on a temporary file that got deleted before completion
            {
                "step_index": 5,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {
                            "Path": f'"{ws / "src/dead_temp.py"}"',
                            "Content": '"def bad_idea(): pass"',
                        },
                    }
                ],
            },
            {
                "step_index": 6,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Created file.",
            },
            # Effective Read: reading src/auth.py
            {
                "step_index": 7,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": f'"{ws / "src/auth.py"}"'}}
                ],
            },
            {
                "step_index": 8,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "1: def verify_jwt_token(token: str): ...",
            },
            # Error Step: pytest fails due to missing dependency
            {
                "step_index": 9,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": '"pytest tests/test_auth.py"'}}
                ],
            },
            {
                "step_index": 10,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Command exited with code 1.\n\nCommand output:\nModuleNotFoundError: No module named 'pyjwt'",
            },
            # Toolchain Fix Step: pip install pyjwt
            {
                "step_index": 11,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": '"pip install pyjwt>=2.8.0"'}}
                ],
            },
            {
                "step_index": 12,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Command exited with code 0.\n\nCommand output:\nSuccessfully installed pyjwt-2.8.0",
            },
            # Effective Mutations: src/auth.py and tests/test_auth.py
            {
                "step_index": 13,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "edit_file",
                        "args": {
                            "Path": f'"{ws / "src/auth.py"}"',
                            "Replacement": '"def verify_jwt_token(token: str, leeway: int = 30):"',
                        },
                    },
                    {
                        "name": "write_file",
                        "args": {
                            "Path": f'"{ws / "tests/test_auth.py"}"',
                            "Content": '"def test_jwt_leeway(): assert True"',
                        },
                    },
                ],
            },
            {
                "step_index": 14,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Edited file.",
            },
            {
                "step_index": 15,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Created file.",
            },
            # Final Verification Pass
            {
                "step_index": 16,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": '"pytest tests/test_auth.py"'}}
                ],
            },
            {
                "step_index": 17,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Command exited with code 0.\n\nCommand output:\n1 passed in 0.03s",
            },
        ]
        transcript_file.write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8"
        )
        return transcript_file

    def test_backward_slicing_and_dual_tier_lifecycle(self) -> None:
        # 1. Install invisible agy plugin in workspace_a
        info = install_invisible_integration(str(self.workspace_a), ["agy"])
        self.assertTrue((self.workspace_a / ".agents" / "plugins" / "tmem" / "hooks.json").exists())
        self.assertEqual(len(info["agents"]), 1)

        # 2. Parse noisy agy session & run BackwardTraceSlicer
        t_path = self._create_noisy_agy_transcript(self.workspace_a)
        adapter = get_adapter("agy")
        session = adapter.parse_transcript(str(t_path), str(self.workspace_a))
        self.assertEqual(len(session.actions), 9)

        slicer = BackwardTraceSlicer()
        pb = slicer.slice_session(session, use_git_ground_truth=False)
        self.assertIsNotNone(pb)
        assert pb is not None

        # Verify Dead Mutation Elimination & Read Shrinking:
        # - src/unrelated.py (read only) and src/dead_temp.py (deleted temp file) must be pruned!
        self.assertEqual(pb.touched_files, ["src/auth.py", "tests/test_auth.py"])
        targets_in_l0 = [s.get("target") for s in pb.l0_critical_path if s.get("target")]
        self.assertNotIn("src/unrelated.py", targets_in_l0)
        self.assertNotIn("src/dead_temp.py", targets_in_l0)
        self.assertIn("src/auth.py", targets_in_l0)

        # Verify L2 Causal Repair Extraction
        self.assertEqual(len(pb.l2_causal_repairs), 1)
        self.assertIn("ModuleNotFoundError: No module named 'pyjwt'", pb.l2_causal_repairs[0].error_signature)
        self.assertTrue(pb.l2_causal_repairs[0].is_env_or_toolchain)

        # 3. Save to Dual-Tier Store (Project + Auto-Promoted Global)
        store_a = DualTierMemoryStore(str(self.workspace_a), str(self.global_dir))
        proj_id, glob_id = store_a.save_playbook(pb, auto_promote_global=True)
        self.assertTrue(proj_id.startswith("tr_"))
        self.assertIsNotNone(glob_id)
        assert glob_id is not None
        self.assertTrue(glob_id.startswith("gtr_"))

        # 4. Verify agy PreInvocation Hook in Workspace A (Guards Pass -> L0_FULL)
        hook_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_2",
                "workspacePaths": [str(self.workspace_a)],
                "lastUserInput": "Update verify_jwt_token in src/auth.py",
            },
        )
        self.assertIn("injectSteps", hook_out)
        sys_msg = hook_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Tier: L0_FULL", sys_msg)
        self.assertIn("Verified L0 Critical Path (Guards Passed)", sys_msg)

        # 5. Simulate Environment Drift: Delete src/auth.py in Workspace A -> Guard Fails -> Auto-Degrades to L2_INVARIANT
        (self.workspace_a / "src" / "auth.py").unlink()
        degraded_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_3",
                "workspacePaths": [str(self.workspace_a)],
                "lastUserInput": "Fix JWT clock skew in auth",
            },
        )
        deg_msg = degraded_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("L0 concrete paths drifted (missing_file:src/auth.py)", deg_msg)
        self.assertIn("L2 Causal Repair", deg_msg)

        # 6. Cross-Project Global Recall in Workspace B (Zero local traces, matches Global ~/.tmem/)
        cross_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_b1",
                "workspacePaths": [str(self.workspace_b)],
                "lastUserInput": "Getting ModuleNotFoundError: No module named 'pyjwt' when running tests",
            },
        )
        self.assertIn("injectSteps", cross_out)
        cross_msg = cross_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Scope: GLOBAL", cross_msg)
        self.assertIn("pip install pyjwt>=2.8.0", cross_msg)

    def test_milestone_prompt_and_slash_commands(self) -> None:
        t_path = self._create_noisy_agy_transcript(self.workspace_a)

        # 1. Simulate PostToolUse: file edit followed by passing test command
        handle_agy_hook(
            "post_tool_use",
            {
                "conversationId": "conv_m1",
                "workspacePaths": [str(self.workspace_a)],
                "toolCall": {"name": "edit_file", "args": {"Path": str(self.workspace_a / "src/auth.py")}},
            },
        )
        handle_agy_hook(
            "post_tool_use",
            {
                "conversationId": "conv_m1",
                "workspacePaths": [str(self.workspace_a)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/test_auth.py"}},
            },
        )

        # 2. Next PreInvocation should inject <tmem_milestone_reached> prompting the agent to ask user
        milestone_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_m1",
                "workspacePaths": [str(self.workspace_a)],
                "lastUserInput": "Run the tests and finish up",
            },
        )
        self.assertIn("injectSteps", milestone_out)
        m_msg = milestone_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("<tmem_milestone_reached>", m_msg)
        self.assertIn("/tmem project", m_msg)
        self.assertIn("/tmem global", m_msg)

        # 3. User types `/tmem both Fix JWT leeway validation` -> PreInvocation intercepts & saves to both tiers
        cmd_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_m1",
                "workspacePaths": [str(self.workspace_a)],
                "transcriptPath": str(t_path),
                "lastUserInput": "/tmem both Fix JWT leeway validation",
            },
        )
        self.assertIn("injectSteps", cmd_out)
        c_msg = cmd_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("<tmem_command_executed>", c_msg)
        self.assertIn('"scope": "both"', c_msg)
        self.assertIn("Fix JWT leeway validation", c_msg)

        # 4. Verify `/tmem status` command
        stat_out = handle_agy_hook(
            "pre_invocation",
            {
                "conversationId": "conv_m1",
                "workspacePaths": [str(self.workspace_a)],
                "lastUserInput": "/tmem status",
            },
        )
        s_msg = stat_out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Command `/tmem status` executed", s_msg)
        self.assertIn("1 traces", s_msg)
        self.assertIn("1 playbooks", s_msg)


if __name__ == "__main__":
    unittest.main()
