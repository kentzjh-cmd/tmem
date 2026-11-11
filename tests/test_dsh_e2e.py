"""
End-to-End (E2E) test suite for DeepSeek Harness (`@deepseek-ai/dsh`) integration:
1. Native `DshAdapter` parsing `session.v4.jsonl` (`user/message`, `tool/call`, `tool/result`,
   `apply_patch` multi-file diffs, `bash` `[exit code: N]` markers, and secret scrubbing).
2. `tmem init --agents dsh` generating `.dsh/plugins/tmem-dsh-plugin.mjs`, `.dsh/tmem.patch.yml`,
   `.agents/skills/tmem/SKILL.md`, and `AGENTS.md`.
3. Live `@deepseek-ai/dsh` (`@deepseek-ai/cordis` + `@deepseek-ai/dsh-commands`) runtime execution
   verifying `/tmem` slash commands, `tools/post-execute` milestone & L2 error repair injection,
   `agent/pre-step` speculative memory recall (`L0_FULL` -> `L1_TOPOLOGY` + `L2_INVARIANT`),
   and `dsh --profile headless --patch ... --dump-config`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tmem.adapters import get_adapter
from tmem.adapters.base import extract_symbols_from_text
from tmem.adapters.dsh import DshAdapter
from tmem.cli import handle_dsh_hook, install_invisible_integration
from tmem.slicer import BackwardTraceSlicer
from tmem.store import DualTierMemoryStore


class TestDshPluginEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.workspace = self.root / "dsh_repo"
        self.global_home = self.root / "global_tmem"
        self.dsh_home = self.root / "dsh_home"
        self.workspace.mkdir(parents=True)
        self.global_home.mkdir(parents=True)
        self.dsh_home.mkdir(parents=True)

        # Initialize git repository with initial baseline files
        subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dev@tmem.local"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "tmem-tester"], cwd=self.workspace, check=True)

        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()
        (self.workspace / "src" / "billing.py").write_text(
            "def calculate_unit_tax(amount, units):\n    return amount / units\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "tax_rules.py").write_text(
            "DEFAULT_TAX_FLOOR = 0.0\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "dead_experiment.py").write_text(
            "DEAD = False\n",
            encoding="utf-8",
        )
        (self.workspace / "tests" / "test_billing.py").write_text(
            "from src.billing import calculate_unit_tax\n"
            "def test_zero_units():\n"
            "    assert calculate_unit_tax(100, 0) == 0.0\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.workspace, check=True, capture_output=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_dsh_v4_session(self, session_id: str = "sess_dsh_001") -> Path:
        """
        Simulate a realistic `@deepseek-ai/dsh` v4 session (`session.v4.jsonl`) with:
        - System agent-instructions notice (should be ignored as user intent)
        - User prompt with a secret token (should be scrubbed)
        - Exploratory `read`
        - Dead-end `edit` on `src/dead_experiment.py` (reverted on disk)
        - Failing `bash` `pytest -q` with `[exit code: 1]` and `ZeroDivisionError`
        - Multi-file `apply_patch` updating `src/billing.py` and `src/tax_rules.py`
        - Passing `bash` `pytest -q` (exit code 0)
        """
        # Apply final ground-truth modifications on disk (`src/dead_experiment.py` remains untouched)
        (self.workspace / "src" / "billing.py").write_text(
            "from src.tax_rules import normalize_rate\n\n"
            "def calculate_unit_tax(amount, units):\n"
            "    if units == 0:\n"
            "        return normalize_rate(0.0)\n"
            "    return normalize_rate(amount / units)\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "tax_rules.py").write_text(
            "DEFAULT_TAX_FLOOR = 0.0\n\n"
            "def normalize_rate(val):\n"
            "    return max(DEFAULT_TAX_FLOOR, round(float(val), 4))\n",
            encoding="utf-8",
        )

        sess_dir = self.dsh_home / "sessions" / session_id
        sess_dir.mkdir(parents=True, exist_ok=True)
        v4_path = sess_dir / "session.v4.jsonl"

        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: src/billing.py\n"
            "@@\n"
            "-def calculate_unit_tax(amount, units):\n"
            "-    return amount / units\n"
            "+from src.tax_rules import normalize_rate\n"
            "+def calculate_unit_tax(amount, units):\n"
            "+    if units == 0:\n"
            "+        return normalize_rate(0.0)\n"
            "+    return normalize_rate(amount / units)\n"
            "*** Update File: src/tax_rules.py\n"
            "@@\n"
            " DEFAULT_TAX_FLOOR = 0.0\n"
            "+def normalize_rate(val):\n"
            "+    return max(DEFAULT_TAX_FLOOR, round(float(val), 4))\n"
            "*** End Patch"
        )

        events = [
            {
                "version": 4,
                "id": session_id,
                "createdAt": 1731225600000,
                "isSeeded": False,
                "delegationDepth": 0,
            },
            {
                "type": "user/message",
                "seq": 1,
                "time": 1731225601000,
                "data": {
                    "id": "msg_sys",
                    "role": "user",
                    "content": [{"type": "text", "text": "<system-instruction>AGENTS.md</system-instruction>"}],
                    "source": {"kind": "agent-instructions", "form": "notice"},
                },
            },
            {
                "type": "user/message",
                "seq": 2,
                "time": 1731225602000,
                "data": {
                    "id": "msg_u1",
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Fix calculate_unit_tax ZeroDivisionError in src/billing.py and normalize rate in src/tax_rules.py "
                                "(token github_pat_11ABCDEF0123456789_secretTokenValue)"
                            ),
                        }
                    ],
                    "source": {"kind": "user"},
                },
            },
            {
                "type": "tool/call",
                "seq": 3,
                "time": 1731225603000,
                "data": {
                    "turn": 1,
                    "step": 0,
                    "callId": "call_read_1",
                    "name": "read",
                    "arguments": json.dumps({"file_path": str(self.workspace / "src" / "billing.py")}),
                },
            },
            {
                "type": "tool/result",
                "seq": 4,
                "time": 1731225604000,
                "sourceEventSeqs": [3],
                "data": {
                    "turn": 1,
                    "step": 0,
                    "message": {
                        "id": "msg_tr_1",
                        "role": "tool",
                        "callId": "call_read_1",
                        "content": [{"type": "text", "text": "1\tdef calculate_unit_tax(amount, units):\n2\t    return amount / units"}],
                        "isError": False,
                    },
                },
            },
            {
                "type": "tool/call",
                "seq": 5,
                "time": 1731225605000,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "callId": "call_dead_edit",
                    "name": "edit",
                    "arguments": json.dumps(
                        {
                            "file_path": str(self.workspace / "src" / "dead_experiment.py"),
                            "old_string": "DEAD = False",
                            "new_string": "def abandoned_helper(): return True",
                        }
                    ),
                },
            },
            {
                "type": "tool/result",
                "seq": 6,
                "time": 1731225606000,
                "sourceEventSeqs": [5],
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "id": "msg_tr_2",
                        "role": "tool",
                        "callId": "call_dead_edit",
                        "content": [{"type": "text", "text": "Successfully edited src/dead_experiment.py"}],
                        "isError": False,
                    },
                },
            },
            {
                "type": "tool/call",
                "seq": 7,
                "time": 1731225607000,
                "data": {
                    "turn": 1,
                    "step": 2,
                    "callId": "call_bash_fail",
                    "name": "bash",
                    "arguments": json.dumps({"command": "pytest -q"}),
                },
            },
            {
                "type": "tool/result",
                "seq": 8,
                "time": 1731225608000,
                "sourceEventSeqs": [7],
                "data": {
                    "turn": 1,
                    "step": 2,
                    "message": {
                        "id": "msg_tr_3",
                        "role": "tool",
                        "callId": "call_bash_fail",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "FAILED tests/test_billing.py::test_zero_units\n"
                                    "ZeroDivisionError: division by zero in calculate_unit_tax\n\n"
                                    "[exit code: 1]"
                                ),
                            }
                        ],
                        "isError": False,
                    },
                },
            },
            {
                "type": "tool/call",
                "seq": 9,
                "time": 1731225609000,
                "data": {
                    "turn": 1,
                    "step": 3,
                    "callId": "call_patch_fix",
                    "name": "apply_patch",
                    "arguments": json.dumps({"input": patch_text}),
                },
            },
            {
                "type": "tool/result",
                "seq": 10,
                "time": 1731225610000,
                "sourceEventSeqs": [9],
                "data": {
                    "turn": 1,
                    "step": 3,
                    "message": {
                        "id": "msg_tr_4",
                        "role": "tool",
                        "callId": "call_patch_fix",
                        "content": [{"type": "text", "text": "Applied patch to 2 files."}],
                        "isError": False,
                    },
                },
            },
            {
                "type": "tool/call",
                "seq": 11,
                "time": 1731225611000,
                "data": {
                    "turn": 1,
                    "step": 4,
                    "callId": "call_bash_pass",
                    "name": "bash",
                    "arguments": json.dumps({"command": "pytest -q"}),
                },
            },
            {
                "type": "tool/result",
                "seq": 12,
                "time": 1731225612000,
                "sourceEventSeqs": [11],
                "data": {
                    "turn": 1,
                    "step": 4,
                    "message": {
                        "id": "msg_tr_5",
                        "role": "tool",
                        "callId": "call_bash_pass",
                        "content": [{"type": "text", "text": "1 passed in 0.02s"}],
                        "isError": False,
                    },
                },
            },
            {
                "type": "assistant/message",
                "seq": 13,
                "time": 1731225613000,
                "data": {
                    "turn": 1,
                    "step": 5,
                    "message": {
                        "id": "msg_asst_1",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Fixed ZeroDivisionError in [src/billing.py](file:///src/billing.py) by guarding `units == 0` "
                                    "and normalizing tax rates via `normalize_rate` in [src/tax_rules.py](file:///src/tax_rules.py)."
                                ),
                            }
                        ],
                    },
                },
            },
            {
                "type": "user/message",
                "seq": 14,
                "time": 1731225614000,
                "data": {
                    "id": "msg_u_filler",
                    "role": "user",
                    "content": [{"type": "text", "text": "done"}],
                    "source": {"kind": "user"},
                },
            },
        ]

        with v4_path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return v4_path

    def test_dsh_adapter_v4_parsing_and_backward_slicing(self) -> None:
        v4_path = self._create_dsh_v4_session("sess_dsh_001")
        with patch.dict(os.environ, {"DSH_HOME": str(self.dsh_home)}):
            adapter = get_adapter("dsh")
            self.assertIsInstance(adapter, DshAdapter)
            discovered = adapter.discover_latest_transcript(str(self.workspace), session_id="sess_dsh_001")
            self.assertEqual(discovered, str(v4_path))

            parsed = adapter.parse_transcript(str(v4_path), str(self.workspace))
            self.assertEqual(parsed.session_id, "sess_dsh_001")
            # Agent-instructions notice was filtered out; secret PAT was scrubbed
            self.assertEqual(len(parsed.user_intents), 1)
            self.assertIn("[REDACTED_SECRET]", parsed.user_intents[0])
            self.assertNotIn("github_pat_", parsed.user_intents[0])

            slicer = BackwardTraceSlicer()
            playbook = slicer.slice_session(parsed, use_git_ground_truth=True)
            self.assertIsNotNone(playbook)
            assert playbook is not None

            # Verify dead experiment file was pruned by git ground-truth backward slicing
            self.assertEqual(playbook.touched_files, ["src/billing.py", "src/tax_rules.py"])
            self.assertNotIn("src/dead_experiment.py", playbook.touched_files)
            self.assertLess(playbook.sliced_steps_count, playbook.raw_steps_count)

            # Verify L1 co-change topology, semantic summary & L2 error-to-fix invariant
            self.assertIn("Fixed ZeroDivisionError in src/billing.py", playbook.semantic_summary)
            self.assertIn("Key symbols: calculate_unit_tax, normalize_rate", playbook.semantic_summary)
            self.assertTrue(
                any(
                    "src/billing.py" in c and "src/tax_rules.py" in c
                    for c in playbook.l1_cochange_topology
                )
            )
            self.assertIn("pytest -q", playbook.l1_verification_cmds)
            self.assertEqual(len(playbook.l2_causal_repairs), 1)
            self.assertIn(
                "ZeroDivisionError: division by zero in calculate_unit_tax",
                playbook.l2_causal_repairs[0].error_signature,
            )

    def test_live_dsh_cordis_runtime_and_cli_e2e(self) -> None:
        """
        Execute the actual `@deepseek-ai/dsh` (`@deepseek-ai/cordis` + `@deepseek-ai/dsh-commands`)
        Node.js runtime with `.dsh/plugins/tmem-dsh-plugin.mjs` loaded!
        """
        self._create_dsh_v4_session("sess_dsh_live")
        env_patch = {
            "DSH_HOME": str(self.dsh_home),
            "TMEM_GLOBAL_DIR": str(self.global_home),
            "PYTHONPATH": "/home/ubuntu/proj",
        }
        with patch.dict(os.environ, env_patch):
            info = install_invisible_integration(str(self.workspace), ["dsh"], install_git_hook=True)
            self.assertTrue((self.workspace / ".dsh" / "plugins" / "tmem-dsh-plugin.mjs").exists())
            self.assertTrue((self.workspace / ".dsh" / "tmem.patch.yml").exists())
            self.assertTrue((self.workspace / ".agents" / "skills" / "tmem" / "SKILL.md").exists())
            self.assertTrue((self.workspace / "AGENTS.md").exists())
            self.assertTrue(info["git_hook"])

        # 1. Verify `dsh --profile headless --patch <ws>/.dsh/tmem.patch.yml --dump-config` succeeds on real dsh CLI
        dump_proc = subprocess.run(
            [
                "dsh",
                "--profile",
                "headless",
                "--patch",
                str(self.workspace / ".dsh" / "tmem.patch.yml"),
                "--dump-config",
            ],
            cwd=self.workspace,
            env={**os.environ, **env_patch},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("id: tmem", dump_proc.stdout)
        self.assertIn("tmem-dsh-plugin.mjs", dump_proc.stdout)

        # 2. Run a live Node.js E2E harness importing @deepseek-ai/cordis, @deepseek-ai/dsh-commands,
        # and our generated `.dsh/plugins/tmem-dsh-plugin.mjs`
        plugin_url = (self.workspace / ".dsh" / "plugins" / "tmem-dsh-plugin.mjs").as_uri()
        harness_js = f"""
import {{ createRequire }} from "node:module";
const req = createRequire("/usr/lib/node_modules/@deepseek-ai/dsh/lib/index.js");
const {{ Context }} = req("@deepseek-ai/cordis");
const dshCommands = req("@deepseek-ai/dsh-commands");
const tmemPlugin = await import({json.dumps(plugin_url)});

const ctx = new Context();
ctx.plugin(dshCommands.default);
ctx.plugin(tmemPlugin.default, {{
  workspaceRoot: {json.dumps(str(self.workspace))},
  tmemRoot: "/home/ubuntu/proj",
  pythonBin: "/usr/bin/python3"
}});

// Wait a tick for Cordis service injection
await new Promise((r) => setTimeout(r, 50));

// A. Execute `/tmem both` via dsh-commands CommandRuntime
const commandsSvc = ctx.get("commands");
const commandLifecycleLog = [];
const mockAgent = {{
  session: {{
    id: "sess_dsh_live",
    append(type, data) {{
      commandLifecycleLog.push({{ type, data }});
    }}
  }}
}};
const ac = new AbortController();
const captureExec = await commandsSvc.execute(mockAgent, "/tmem both", [], ac.signal);
const captureRes = captureExec.result;

// B. Execute `/tmem status` and `/tmem bench` via dsh-commands CommandRuntime
const statusExec = await commandsSvc.execute(mockAgent, "/tmem status", [], ac.signal);
const statusRes = statusExec.result;

const benchExec = await commandsSvc.execute(mockAgent, "/tmem bench", [], ac.signal);
const benchRes = benchExec.result;

// C. Simulate `tools/post-execute` for `edit` -> `bash (pytest -q)` to trigger `<tmem_milestone_reached>`
const runPostTool = async (exec, result) => {{
  let idx = -1;
  const hooks = [...(ctx.events._hooks["tools/post-execute"] || [])];
  const dispatch = async (i) => {{
    if (i <= idx) throw new Error("next() called multiple times");
    idx = i;
    const entry = hooks[i];
    if (!entry) return {{ content: result.content, isError: result.isError }};
    return await entry.callback.call(ctx, exec, result, () => dispatch(i + 1));
  }};
  return await dispatch(0);
}};

await runPostTool(
  {{
    name: "edit",
    arguments: JSON.stringify({{ file_path: "src/billing.py", old_string: "a", new_string: "b" }}),
    agent: {{ session: {{ id: "sess_dsh_live" }} }}
  }},
  {{
    content: [{{ type: "text", text: "Edited src/billing.py" }}],
    isError: false
  }}
);

const postVerify = await runPostTool(
  {{
    name: "bash",
    arguments: JSON.stringify({{ command: "pytest -q" }}),
    agent: {{ session: {{ id: "sess_dsh_live" }} }}
  }},
  {{
    content: [{{ type: "text", text: "1 passed in 0.01s" }}],
    isError: false
  }}
);

// D. Simulate `agent/pre-step` on a new session to verify L0_FULL speculative recall
const runPreStep = async (payload) => {{
  const hooks = [...(ctx.events._hooks["agent/pre-step"] || [])];
  let idx = -1;
  const dispatch = async (i) => {{
    idx = i;
    const entry = hooks[i];
    if (!entry) return {{ action: "continue", messages: payload.messages }};
    return await entry.callback.call(ctx, payload, () => dispatch(i + 1));
  }};
  return await dispatch(0);
}};

const preStepDecision = await runPreStep({{
  agent: {{ session: {{ id: "sess_dsh_recall" }} }},
  step: 0,
  messages: [
    {{
      id: "u_new",
      role: "user",
      content: [{{ type: "text", text: "Fix calculate_unit_tax ZeroDivisionError in src/billing.py" }}],
      source: {{ kind: "user" }}
    }}
  ]
}});

console.log(JSON.stringify({{
  captureKind: captureRes.kind,
  captureText: captureRes.text,
  statusText: statusRes.text,
  benchText: benchRes.text,
  milestoneContexts: postVerify.additionalContexts || [],
  preStepMessages: preStepDecision.messages
}}));
"""
        node_proc = subprocess.run(
            ["node", "--input-type=module", "-e", harness_js],
            cwd=self.workspace,
            env={**os.environ, **env_patch},
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(node_proc.stdout.strip().splitlines()[-1])

        # Verify `/tmem both` compiled the trace
        self.assertEqual(payload["captureKind"], "success")
        self.assertIn('"status": "compiled"', payload["captureText"])
        self.assertIn('"agent": "dsh"', payload["captureText"])

        # Verify `/tmem status` reported the project trace
        self.assertIn("Project Tier", payload["statusText"])
        self.assertIn("1 trace(s)", payload["statusText"])

        # Verify `/tmem bench` executed and returned benchmark report
        self.assertIn("tmem Upstream Memory Compilation", payload["benchText"])
        self.assertIn("tmem Downstream Agent Efficacy", payload["benchText"])

        # Verify `tools/post-execute` injected `<tmem_milestone_reached>` after `edit` -> `bash(pytest)`
        self.assertEqual(len(payload["milestoneContexts"]), 1)
        milestone_text = payload["milestoneContexts"][0]["content"][0]["text"]
        self.assertIn("<tmem_milestone_reached>", milestone_text)
        self.assertIn("/tmem", milestone_text)

        # Verify `agent/pre-step` injected `<tmem_speculative_memory>` with `tier="L0_FULL"`
        self.assertEqual(len(payload["preStepMessages"]), 2)
        recalled_msg = payload["preStepMessages"][0]
        self.assertEqual(recalled_msg["source"]["kind"], "tmem")
        recalled_text = recalled_msg["content"][0]["text"]
        self.assertIn("<tmem_speculative_memory>", recalled_text)
        self.assertIn("Tier: L0_FULL", recalled_text)

        # 3. Now simulate code drift on `src/billing.py` and a failing `bash` tool call in `dsh`,
        # verifying automatic guard degradation to L1_TOPOLOGY + L2_INVARIANT repair!
        (self.workspace / "src" / "billing.py").write_text(
            "# Refactored completely so symbol_present guard fails\n"
            "def compute_v2(amount, units):\n    return amount / units\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, env_patch):
            err_out = handle_dsh_hook(
                "post_tool_use",
                {
                    "workspaceRoot": str(self.workspace),
                    "sessionId": "sess_dsh_drift",
                    "toolCall": {"name": "bash", "args": {"command": "pytest -q"}},
                    "resultText": "ZeroDivisionError: division by zero in calculate_unit_tax\n\n[exit code: 1]",
                    "isError": False,
                    "exitCode": None,
                },
            )
        self.assertIn("additionalContext", err_out)
        self.assertIn("Tier: L2_INVARIANT", err_out["additionalContext"])
        self.assertIn("missing_symbol:src/billing.py#calculate_unit_tax", err_out["additionalContext"])
        self.assertIn("- L1 Topology:", err_out["additionalContext"])
        self.assertIn("L2 Causal Repair:", err_out["additionalContext"])
        self.assertIn("ZeroDivisionError", err_out["additionalContext"])

        # 4. Verify `/tmem list` and `/tmem forget <trace_id>` + non-code file guard filtering
        self.assertEqual(extract_symbols_from_text("function for the system", "README.md"), [])
        with patch.dict(os.environ, env_patch):
            list_out = handle_dsh_hook(
                "command",
                {
                    "workspaceRoot": str(self.workspace),
                    "sessionId": "sess_dsh_live",
                    "args": "list project",
                },
            )
            self.assertEqual(list_out["kind"], "success")
            self.assertIn("tr_", list_out["text"])

            forget_out = handle_dsh_hook(
                "command",
                {
                    "workspaceRoot": str(self.workspace),
                    "sessionId": "sess_dsh_live",
                    "args": "forget tr_",
                },
            )
            self.assertEqual(forget_out["kind"], "success")
            self.assertIn('"deleted": true', forget_out["text"])


if __name__ == "__main__":
    unittest.main()
