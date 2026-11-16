/**
 * Core data models for Action IR, Speculative Guards, and 3-Tier Trace Playbooks.
 */

export const ActionKind = Object.freeze({
  READ: "READ",
  MUTATE: "MUTATE",
  ENV_EXEC: "ENV_EXEC",
  OTHER: "OTHER",
});

const VALID_ACTION_KINDS = new Set(Object.values(ActionKind));

export class ActionNode {
  constructor({
    step_idx,
    kind = ActionKind.OTHER,
    tool_name = "tool",
    target_file = null,
    command = null,
    symbols_touched = [],
    exit_code = null,
    error_signature = null,
    output_snippet = null,
    timestamp = "",
    raw_input = null,
    raw_output = null,
    rationale = null,
  } = {}) {
    this.step_idx = step_idx ?? 0;
    this.kind = VALID_ACTION_KINDS.has(kind) ? kind : ActionKind.OTHER;
    this.tool_name = tool_name ?? "tool";
    this.target_file = target_file ?? null;
    this.command = command ?? null;
    this.symbols_touched = Array.isArray(symbols_touched) ? [...symbols_touched] : [];
    this.exit_code = exit_code ?? null;
    this.error_signature = error_signature ?? null;
    this.output_snippet = output_snippet ?? null;
    this.timestamp = timestamp ?? "";
    this.raw_input = raw_input ?? null;
    this.raw_output = raw_output ?? null;
    this.rationale = rationale ?? null;
  }

  toDict() {
    return {
      step_idx: this.step_idx,
      kind: this.kind,
      tool_name: this.tool_name,
      target_file: this.target_file,
      command: this.command,
      symbols_touched: [...this.symbols_touched],
      exit_code: this.exit_code,
      error_signature: this.error_signature,
      output_snippet: this.output_snippet,
      timestamp: this.timestamp,
      raw_input: this.raw_input,
      raw_output: this.raw_output,
      rationale: this.rationale,
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    return new ActionNode(data);
  }
}

export class GuardProbe {
  constructor({ probe_type, target, expected = "" } = {}) {
    this.probe_type = probe_type;
    this.target = target;
    this.expected = expected ?? "";
  }

  toDict() {
    return {
      probe_type: this.probe_type,
      target: this.target,
      expected: this.expected,
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    return new GuardProbe(data);
  }
}

export class CausalRepairPair {
  constructor({
    error_signature,
    failed_command,
    repair_action,
    verification_cmd,
    is_env_or_toolchain = false,
  } = {}) {
    this.error_signature = error_signature;
    this.failed_command = failed_command;
    this.repair_action = repair_action;
    this.verification_cmd = verification_cmd;
    this.is_env_or_toolchain = Boolean(is_env_or_toolchain);
  }

  toDict() {
    return {
      error_signature: this.error_signature,
      failed_command: this.failed_command,
      repair_action: this.repair_action,
      verification_cmd: this.verification_cmd,
      is_env_or_toolchain: this.is_env_or_toolchain,
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    return new CausalRepairPair(data);
  }
}

const TRACE_PLAYBOOK_FIELDS = new Set([
  "trace_id",
  "agent_source",
  "scope",
  "created_at",
  "git_commit",
  "schema_version",
  "source_session_ids",
  "problem_intent",
  "semantic_summary",
  "error_signatures",
  "touched_files",
  "touched_symbols",
  "guards",
  "l0_critical_path",
  "l1_cochange_topology",
  "l1_verification_cmds",
  "l2_causal_repairs",
  "root_cause",
  "pitfalls",
  "confidence",
  "hit_count",
  "success_count",
  "contradict_count",
  "merge_count",
  "guard_fail_streak",
  "status",
  "is_trivial",
  "raw_steps_count",
  "sliced_steps_count",
  "raw_trace_path",
  "raw_trace_hash",
]);

export class TracePlaybook {
  constructor(data = {}) {
    this.trace_id = data.trace_id;
    this.agent_source = data.agent_source;
    this.scope = data.scope ?? "project";
    this.created_at = data.created_at ?? "";
    this.git_commit = data.git_commit ?? null;
    this.schema_version = data.schema_version ?? 3;
    this.source_session_ids = Array.isArray(data.source_session_ids)
      ? [...data.source_session_ids]
      : [];
    this.problem_intent = data.problem_intent ?? "";
    this.semantic_summary = data.semantic_summary ?? "";
    this.error_signatures = Array.isArray(data.error_signatures)
      ? [...data.error_signatures]
      : [];
    this.touched_files = Array.isArray(data.touched_files) ? [...data.touched_files] : [];
    this.touched_symbols = Array.isArray(data.touched_symbols) ? [...data.touched_symbols] : [];
    this.guards = Array.isArray(data.guards)
      ? data.guards.map((g) => (g instanceof GuardProbe ? g : GuardProbe.fromDict(g)))
      : [];
    this.l0_critical_path = Array.isArray(data.l0_critical_path)
      ? data.l0_critical_path.map((s) => ({ ...s }))
      : [];
    this.l1_cochange_topology = Array.isArray(data.l1_cochange_topology)
      ? [...data.l1_cochange_topology]
      : [];
    this.l1_verification_cmds = Array.isArray(data.l1_verification_cmds)
      ? [...data.l1_verification_cmds]
      : [];
    this.l2_causal_repairs = Array.isArray(data.l2_causal_repairs)
      ? data.l2_causal_repairs.map((r) =>
          r instanceof CausalRepairPair ? r : CausalRepairPair.fromDict(r)
        )
      : [];
    this.root_cause = data.root_cause ?? null;
    this.pitfalls = Array.isArray(data.pitfalls) ? [...data.pitfalls] : [];
    this.confidence = data.confidence ?? 0.5;
    this.hit_count = data.hit_count ?? 0;
    this.success_count = data.success_count ?? 0;
    this.contradict_count = data.contradict_count ?? 0;
    this.merge_count = data.merge_count ?? 0;
    this.guard_fail_streak = data.guard_fail_streak ?? 0;
    this.status = data.status ?? "active";
    this.is_trivial = Boolean(data.is_trivial ?? false);
    this.raw_steps_count = data.raw_steps_count ?? 0;
    this.sliced_steps_count = data.sliced_steps_count ?? 0;
    this.raw_trace_path = data.raw_trace_path ?? null;
    this.raw_trace_hash = data.raw_trace_hash ?? null;
  }

  toDict() {
    return {
      trace_id: this.trace_id,
      agent_source: this.agent_source,
      scope: this.scope,
      created_at: this.created_at,
      git_commit: this.git_commit,
      schema_version: this.schema_version,
      source_session_ids: [...this.source_session_ids],
      problem_intent: this.problem_intent,
      semantic_summary: this.semantic_summary,
      error_signatures: [...this.error_signatures],
      touched_files: [...this.touched_files],
      touched_symbols: [...this.touched_symbols],
      guards: this.guards.map((g) => g.toDict()),
      l0_critical_path: this.l0_critical_path.map((s) => ({ ...s })),
      l1_cochange_topology: [...this.l1_cochange_topology],
      l1_verification_cmds: [...this.l1_verification_cmds],
      l2_causal_repairs: this.l2_causal_repairs.map((r) => r.toDict()),
      root_cause: this.root_cause,
      pitfalls: [...this.pitfalls],
      confidence: this.confidence,
      hit_count: this.hit_count,
      success_count: this.success_count,
      contradict_count: this.contradict_count,
      merge_count: this.merge_count,
      guard_fail_streak: this.guard_fail_streak,
      status: this.status,
      is_trivial: this.is_trivial,
      raw_steps_count: this.raw_steps_count,
      sliced_steps_count: this.sliced_steps_count,
      raw_trace_path: this.raw_trace_path,
      raw_trace_hash: this.raw_trace_hash,
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    const filtered = {};
    for (const [k, v] of Object.entries(data || {})) {
      if (TRACE_PLAYBOOK_FIELDS.has(k)) {
        filtered[k] = v;
      }
    }
    if (filtered.schema_version === undefined) {
      filtered.schema_version = 1;
    }
    return new TracePlaybook(filtered);
  }
}

export class RawExecutionTrace {
  constructor(data = {}) {
    this.trace_id = data.trace_id;
    this.session_id = data.session_id;
    this.agent_source = data.agent_source;
    this.workspace_root = data.workspace_root;
    this.created_at = data.created_at;
    this.git_commit = data.git_commit ?? null;
    this.user_intents = Array.isArray(data.user_intents) ? [...data.user_intents] : [];
    this.assistant_conclusions = Array.isArray(data.assistant_conclusions)
      ? [...data.assistant_conclusions]
      : [];
    this.total_steps = data.total_steps ?? 0;
    this.actions = Array.isArray(data.actions) ? [...data.actions] : [];
    this.sliced_step_indices = Array.isArray(data.sliced_step_indices)
      ? [...data.sliced_step_indices]
      : [];
    this.diff_patch = data.diff_patch ?? null;
    this.metadata = data.metadata && typeof data.metadata === "object" ? { ...data.metadata } : {};
  }

  toDict() {
    return {
      trace_id: this.trace_id,
      session_id: this.session_id,
      agent_source: this.agent_source,
      workspace_root: this.workspace_root,
      created_at: this.created_at,
      git_commit: this.git_commit,
      user_intents: [...this.user_intents],
      assistant_conclusions: [...this.assistant_conclusions],
      total_steps: this.total_steps,
      actions: [...this.actions],
      sliced_step_indices: [...this.sliced_step_indices],
      diff_patch: this.diff_patch,
      metadata: { ...this.metadata },
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    return new RawExecutionTrace(data);
  }
}
