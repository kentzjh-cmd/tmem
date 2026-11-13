"""Lifecycle hooks for DeepSeek Harness (dsh) coding agent."""

from tmem.hooks.commands import SLASH_TMEM_RE, _parse_inspect_args, parse_slash_tmem_command
from tmem.hooks.dsh import handle_dsh_hook
from tmem.hooks.state import (
    default_runtime_state,
    load_runtime_state,
    save_runtime_state,
)

__all__ = [
    "SLASH_TMEM_RE",
    "_parse_inspect_args",
    "parse_slash_tmem_command",
    "handle_dsh_hook",
    "default_runtime_state",
    "load_runtime_state",
    "save_runtime_state",
]
