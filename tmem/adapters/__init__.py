"""Adapters for DeepSeek Harness (dsh) and offline SWE-bench / ATIF evaluation transcripts."""

from __future__ import annotations

from typing import Dict

from tmem.adapters.base import AgentAdapter
from tmem.adapters.dsh import DshAdapter
from tmem.adapters.swe_bench import SweBenchAdapter


def get_adapter(agent_name: str = "dsh") -> AgentAdapter:
    """Factory returning the adapter for dsh (primary) or swe_bench/atif (offline evaluation)."""
    swe_adapter = SweBenchAdapter()
    adapters: Dict[str, AgentAdapter] = {
        "dsh": DshAdapter(),
        "swe_bench": swe_adapter,
        "swe_agent": swe_adapter,
        "sweagent": swe_adapter,
        "atif": swe_adapter,
    }
    return adapters.get((agent_name or "dsh").lower(), DshAdapter())
