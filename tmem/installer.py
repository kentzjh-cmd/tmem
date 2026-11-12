"""Installer and harness integrator for tmem."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List

from tmem import logger
from tmem.store import DualTierMemoryStore


def install_invisible_integration(
    workspace_root: str, agents: List[str], install_git_hook: bool = True
) -> Dict[str, Any]:
    """Set up invisible hooks, rules, and `/tmem` skill for target agents and Git post-commit."""
    ws = Path(workspace_root).resolve()
    store = DualTierMemoryStore(str(ws))
    store.ensure_initialized(init_project=True)

    installed: Dict[str, Any] = {"workspace": str(ws), "agents": [], "git_hook": False}
    cli_cmd = "python3 -m tmem.cli"

    if "agy" in agents:
        plugin_dir = ws / ".agents" / "plugins" / "tmem"
        rules_dir = plugin_dir / "rules"
        skill_dir = plugin_dir / "skills" / "tmem"
        rules_dir.mkdir(parents=True, exist_ok=True)
        skill_dir.mkdir(parents=True, exist_ok=True)

        plugin_manifest = {
            "name": "tmem",
            "description": "Invisible Backward-Slicing Execution Trace Memory Compiler (.tmem/)",
        }
        (plugin_dir / "plugin.json").write_text(
            json.dumps(plugin_manifest, indent=2), encoding="utf-8"
        )

        hooks_cfg = {
            "tmem-invisible-memory": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f"{cli_cmd} hook agy pre_invocation",
                        "timeout": 5,
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{cli_cmd} hook agy post_tool_use",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
            }
        }
        (plugin_dir / "hooks.json").write_text(
            json.dumps(hooks_cfg, indent=2), encoding="utf-8"
        )

        rules_md = (
            "# tmem Speculative Execution Memory & Milestone Protocol\n\n"
            "## 1. Consuming Recalled Memory (`<tmem_speculative_memory>`) & `MEMORIES.md`\n"
            "- **Living Memory Index (`MEMORIES.md`)**: When navigating or planning modifications in this repository, consult `MEMORIES.md` (or `MEM.md`) for verified execution paths, co-change rules, and known error repairs.\n"
            "- **L0_FULL (Guards Passed)**: Follow the verified `L0 Critical Path` directly instead of performing redundant exploratory `grep`/`find` scans.\n"
            "- **L1_TOPOLOGY**: Respect the co-change file contract and run the specified verification command before finishing.\n"
            "- **L2_INVARIANT**: When encountering a matching error signature, apply the known causal repair action first.\n"
            "- **Progressive Disclosure**: Detailed playbooks are in `.tmem/traces/*.json`; full forensic step replay is in `.tmem/raw/*.raw.json` (`python3 -m tmem.cli inspect <id> --level raw`).\n\n"
            "## 2. Goal Completion & `/tmem` Memory Persistence\n"
            "- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:\n"
            "  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
            "- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:\n"
            f"  `{cli_cmd} capture`\n"
        )
        (rules_dir / "AGENTS.md").write_text(rules_md, encoding="utf-8")

        skill_md = (
            "---\n"
            "name: tmem\n"
            "description: >-\n"
            "  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.\n"
            "  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.\n"
            "---\n\n"
            "# tmem Execution Trace Memory Skill\n\n"
            "## Commands\n\n"
            "1. **Save Memory Trace**:\n"
            f"   `{cli_cmd} capture`\n"
            "2. **Check Memory Status**:\n"
            f"   `{cli_cmd} status`\n"
        )
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        installed["agents"].append("agy (.agents/plugins/tmem: hooks + rules + /tmem skill)")

    if "dsh" in agents:
        plugin_src = Path(__file__).resolve().parent / "dsh_plugin.mjs"
        dsh_plugin_dir = ws / ".dsh" / "plugins"
        dsh_plugin_dir.mkdir(parents=True, exist_ok=True)
        dsh_plugin_dst = dsh_plugin_dir / "tmem-dsh-plugin.mjs"
        if plugin_src.exists():
            dsh_plugin_dst.write_text(plugin_src.read_text(encoding="utf-8"), encoding="utf-8")

        patch_yaml = (
            "# Generated by tmem init for DeepSeek Harness (@deepseek-ai/dsh)\n"
            "- insert:\n"
            "    - id: tmem\n"
            "      name: ./.dsh/plugins/tmem-dsh-plugin.mjs\n"
        )
        ws_patch_path = ws / ".dsh" / "tmem.patch.yml"
        ws_patch_path.write_text(patch_yaml, encoding="utf-8")

        dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
        try:
            dsh_home.mkdir(parents=True, exist_ok=True)
            home_patch = dsh_home / "cordis.patch.yml"
            if home_patch.exists():
                existing_patch = home_patch.read_text(encoding="utf-8")
                if "id: tmem" not in existing_patch and "tmem-dsh-plugin.mjs" not in existing_patch:
                    home_patch.write_text(
                        existing_patch.rstrip() + "\n" + patch_yaml, encoding="utf-8"
                    )
            else:
                home_patch.write_text(patch_yaml, encoding="utf-8")
        except OSError as e:
            logger.debug("Failed updating cordis.patch.yml: %s", e)

        dsh_skill_dir = ws / ".agents" / "skills" / "tmem"
        dsh_skill_dir.mkdir(parents=True, exist_ok=True)
        dsh_skill_md = (
            "---\n"
            "name: tmem\n"
            "description: >-\n"
            "  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.\n"
            "  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.\n"
            "---\n\n"
            "# tmem Execution Trace Memory Skill (dsh & Multi-Agent)\n\n"
            "## Commands\n\n"
            "1. **Save Memory Trace**:\n"
            f"   `{cli_cmd} capture --agent dsh`\n"
            "2. **Check Memory Status**:\n"
            f"   `{cli_cmd} status`\n"
        )
        (dsh_skill_dir / "SKILL.md").write_text(dsh_skill_md, encoding="utf-8")

        ws_agents_md = ws / "AGENTS.md"
        dsh_rules_section = (
            "# tmem Speculative Execution Memory & Milestone Protocol\n\n"
            "## 1. Consuming Recalled Memory (`<tmem_speculative_memory>`) & `MEMORIES.md`\n"
            "- **Living Memory Index (`MEMORIES.md`)**: When navigating or planning modifications in this repository, consult `MEMORIES.md` (or `MEM.md`) for verified execution paths, co-change rules, and known error repairs.\n"
            "- **L0_FULL (Guards Passed)**: Follow the verified `L0 Critical Path` directly instead of performing redundant exploratory `grep`/`find` scans.\n"
            "- **L1_TOPOLOGY**: Respect the co-change file contract and run the specified verification command before finishing.\n"
            "- **L2_INVARIANT**: When encountering a matching error signature, apply the known causal repair action first.\n"
            "- **Progressive Disclosure**: Detailed playbooks are in `.tmem/traces/*.json`; full forensic step replay is in `.tmem/raw/*.raw.json` (`python3 -m tmem.cli inspect <id> --level raw`).\n\n"
            "## 2. Goal Completion & `/tmem` Memory Persistence\n"
            "- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:\n"
            "  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n"
            "- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:\n"
            f"  `{cli_cmd} capture --agent dsh`\n"
        )
        if ws_agents_md.exists():
            cur_agents = ws_agents_md.read_text(encoding="utf-8")
            if "tmem Speculative Execution Memory" not in cur_agents:
                ws_agents_md.write_text(
                    cur_agents.rstrip() + "\n\n" + dsh_rules_section, encoding="utf-8"
                )
        else:
            ws_agents_md.write_text(dsh_rules_section, encoding="utf-8")

        installed["agents"].append(
            "dsh (.dsh/plugins/tmem-dsh-plugin.mjs + .dsh/tmem.patch.yml + ~/.dsh/cordis.patch.yml + .agents/skills/tmem + AGENTS.md)"
        )

    store = DualTierMemoryStore(str(ws))
    store.ensure_initialized(init_project=True)
    mem_file = store.sync_memories_markdown()
    installed["memories_doc"] = str(mem_file)

    if install_git_hook and (ws / ".git").exists():
        hooks_dir = ws / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        post_commit = hooks_dir / "post-commit"
        hook_line = f"PYTHONPATH={ws} {cli_cmd} capture --quiet || true\n"
        if post_commit.exists():
            existing = post_commit.read_text(encoding="utf-8")
            if "tmem.cli capture" not in existing:
                post_commit.write_text(existing.rstrip() + "\n" + hook_line, encoding="utf-8")
        else:
            post_commit.write_text("#!/bin/sh\n" + hook_line, encoding="utf-8")
        post_commit.chmod(post_commit.stat().st_mode | stat.S_IEXEC)
        installed["git_hook"] = True

    return installed
