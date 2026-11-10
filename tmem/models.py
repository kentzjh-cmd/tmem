"""Core data models for Action IR, Speculative Guards, and 3-Tier Trace Playbooks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ActionKind(str, Enum):
    """Normalized Agent Tool Action Intermediate Representation (IR)."""

    READ = "READ"          # read_file, view_file, grep, glob
    MUTATE = "MUTATE"      # write_file, edit_file, patch
    ENV_EXEC = "ENV_EXEC"  # run_command, bash, terminal execution
    OTHER = "OTHER"


@dataclass
class ActionNode:
    """Single normalized tool call step from an agent transcript."""

    step_idx: int
    kind: ActionKind
    tool_name: str
    target_file: Optional[str] = None
    command: Optional[str] = None
    symbols_touched: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    error_signature: Optional[str] = None
    output_snippet: Optional[str] = None
    timestamp: str = ""


@dataclass
class GuardProbe:
    """Zero-token (<5ms) pre-flight assertion to validate L0 memory applicability."""

    probe_type: str  # "file_exists" | "symbol_present" | "file_contains"
    target: str      # relative file path
    expected: str = ""  # symbol or substring

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuardProbe":
        return cls(**data)


@dataclass
class CausalRepairPair:
    """L2 Universal Invariant: [Error Signature] -> [Minimal Repair Action] -> [Verification]."""

    error_signature: str
    failed_command: str
    repair_action: str       # e.g., "pip install pyjwt>=2.8.0" or "edit src/auth.py (symbol: verify_jwt)"
    verification_cmd: str
    is_env_or_toolchain: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CausalRepairPair":
        return cls(**data)


@dataclass
class TracePlaybook:
    """
    Compiled post-session memory artifact with 3 abstraction layers (L0, L1, L2)
    and bi-temporal lifecycle tracking (inspired by Zep/Graphiti + Voyager + Beads).
    """

    trace_id: str
    agent_source: str                  # "agy" | "codex" | "opencode" | "pi" | "dsh"
    scope: str                         # "project" | "global"
    created_at: str
    git_commit: Optional[str] = None

    # Trigger anchors for deterministic recall funnel
    problem_intent: str = ""
    error_signatures: List[str] = field(default_factory=list)
    touched_files: List[str] = field(default_factory=list)
    touched_symbols: List[str] = field(default_factory=list)

    # Pre-flight speculative execution guards
    guards: List[GuardProbe] = field(default_factory=list)

    # L0: Concrete file/command critical path (high specificity, environment-sensitive)
    l0_critical_path: List[Dict[str, Any]] = field(default_factory=list)

    # L1: Architectural co-change topology & verification contract (medium specificity)
    l1_cochange_topology: List[str] = field(default_factory=list)
    l1_verification_cmds: List[str] = field(default_factory=list)

    # L2: Root-cause error -> repair invariants (cross-project portable)
    l2_causal_repairs: List[CausalRepairPair] = field(default_factory=list)

    # Metabolism & telemetry
    confidence: float = 1.0
    hit_count: int = 0
    status: str = "active"             # "active" | "stale" | "invalidated"
    raw_steps_count: int = 0
    sliced_steps_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["guards"] = [g.to_dict() for g in self.guards]
        data["l2_causal_repairs"] = [r.to_dict() for r in self.l2_causal_repairs]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TracePlaybook":
        copied = dict(data)
        copied["guards"] = [GuardProbe.from_dict(g) for g in copied.get("guards", [])]
        copied["l2_causal_repairs"] = [
            CausalRepairPair.from_dict(r) for r in copied.get("l2_causal_repairs", [])
        ]
        return cls(**copied)
