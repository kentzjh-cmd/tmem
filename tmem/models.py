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
    raw_input: Optional[Dict[str, Any]] = None
    raw_output: Optional[str] = None
    rationale: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_idx": self.step_idx,
            "kind": self.kind.value if isinstance(self.kind, ActionKind) else str(self.kind),
            "tool_name": self.tool_name,
            "target_file": self.target_file,
            "command": self.command,
            "symbols_touched": list(self.symbols_touched),
            "exit_code": self.exit_code,
            "error_signature": self.error_signature,
            "output_snippet": self.output_snippet,
            "timestamp": self.timestamp,
            "raw_input": self.raw_input,
            "raw_output": self.raw_output,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionNode":
        copied = dict(data)
        if "kind" in copied and isinstance(copied["kind"], str):
            try:
                copied["kind"] = ActionKind(copied["kind"])
            except ValueError:
                copied["kind"] = ActionKind.OTHER
        return cls(**copied)


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
class Requirement:
    """User requirement or sub-intent node in the execution-intent graph."""

    req_id: str            # "R1", "R2", ...
    text: str              # User requirement / sub-intent description
    source: str = "user"   # "user" | "rule" | "llm"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Requirement":
        return cls(**data)


@dataclass
class IntentEdge:
    """
    Deterministic/evidence-backed causal edge connecting requirements, steps,
    files, and error invariants in the execution-intent graph.
    """

    src: str               # "R1" | "s3" | "E1"
    dst: str               # "s3" | "file:tmem/guards.py#evaluate_guards" | "R1"
    relation: str          # "served_by" | "informs" | "modifies" | "verifies" | "fixed_by"
    why: str = ""          # Concise semantic explanation
    evidence: List[int] = field(default_factory=list)   # Cites concrete step indices
    provenance: str = "rule"   # "rule" | "transcript" | "llm"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IntentEdge":
        return cls(**data)


@dataclass
class TracePlaybook:
    """
    Compiled post-session memory artifact with 3 abstraction layers (L0, L1, L2),
    execution-intent graph topology (schema_version 2), and bi-temporal lifecycle tracking.
    """

    trace_id: str
    agent_source: str                  # "agy" | "codex" | "opencode" | "pi" | "dsh"
    scope: str = "project"             # Repository-scoped: "project"
    created_at: str = ""
    git_commit: Optional[str] = None
    schema_version: int = 2

    # Trigger anchors for deterministic recall funnel
    problem_intent: str = ""
    semantic_summary: str = ""
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

    # Execution-Intent Graph (Phase 1: Deterministic Evidence-Backed Topology)
    requirements: List[Requirement] = field(default_factory=list)
    intent_edges: List[IntentEdge] = field(default_factory=list)
    root_cause: Optional[str] = None
    pitfalls: List[str] = field(default_factory=list)

    # Metabolism & telemetry
    confidence: float = 1.0
    hit_count: int = 0
    status: str = "active"             # "active" | "stale" | "invalidated"
    is_trivial: bool = False
    raw_steps_count: int = 0
    sliced_steps_count: int = 0
    raw_trace_path: Optional[str] = None
    raw_trace_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["guards"] = [g.to_dict() for g in self.guards]
        data["l2_causal_repairs"] = [r.to_dict() for r in self.l2_causal_repairs]
        data["requirements"] = [req.to_dict() for req in self.requirements]
        data["intent_edges"] = [edge.to_dict() for edge in self.intent_edges]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TracePlaybook":
        copied = dict(data)
        copied["guards"] = [GuardProbe.from_dict(g) for g in copied.get("guards", [])]
        copied["l2_causal_repairs"] = [
            CausalRepairPair.from_dict(r) for r in copied.get("l2_causal_repairs", [])
        ]
        copied["requirements"] = [
            Requirement.from_dict(req) for req in copied.get("requirements", [])
        ]
        copied["intent_edges"] = [
            IntentEdge.from_dict(edge) for edge in copied.get("intent_edges", [])
        ]
        copied.setdefault("schema_version", 1)
        copied.setdefault("semantic_summary", "")
        copied.setdefault("raw_trace_path", None)
        copied.setdefault("raw_trace_hash", None)
        copied.setdefault("root_cause", None)
        copied.setdefault("pitfalls", [])
        copied.setdefault("is_trivial", False)
        return cls(**copied)


@dataclass
class RawExecutionTrace:
    """
    Full-fidelity raw execution trace for forensic replay, deep progressive disclosure,
    and auditing. Stored in .tmem/raw/<trace_id>.raw.json.
    """

    trace_id: str
    session_id: str
    agent_source: str
    workspace_root: str
    created_at: str
    git_commit: Optional[str] = None
    user_intents: List[str] = field(default_factory=list)
    assistant_conclusions: List[str] = field(default_factory=list)
    total_steps: int = 0
    actions: List[Dict[str, Any]] = field(default_factory=list)
    sliced_step_indices: List[int] = field(default_factory=list)
    diff_patch: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RawExecutionTrace":
        return cls(**data)
