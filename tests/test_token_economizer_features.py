"""Tests for zero-LLM TokenEconomizer-inspired optimizations in tmem."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tmem.adapters.base import ParsedSession
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
from tmem.store import DualTierMemoryStore


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
            store = DualTierMemoryStore(ws)
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
            store = DualTierMemoryStore(ws)
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


if __name__ == "__main__":
    unittest.main()
