/**
 * Session Episode Compiler, Progressive Disclosure Renderer (L0 -> L3),
 * and Compaction-Anchor Formatter.
 *
 * Captures compacted or pruned session spans into `.tmem/episodes/` without
 * discarding exploratory or single-file phases (`is_trivial` never blocks Episode
 * archival), while building Turn-granular semantic hierarchies and diff snapshots
 * for lossless progressive recovery.
 */

import { createHash } from "node:crypto";
import path from "node:path";
import { ParsedSession, scrubSecrets } from "./adapters/base.mjs";
import { CausalRepairPair } from "./models.mjs";
import { BackwardTraceSlicer } from "./slicer.mjs";
import { tokenizeInvertedTerms } from "./inverted_index.mjs";

const MAX_STEP_OUTPUT_CHARS = 24000;
const DEFAULT_MAX_RENDER_CHARS = 6000;

function boundStepOutput(text) {
  if (!text) return "";
  const clean = scrubSecrets(String(text));
  if (clean.length <= MAX_STEP_OUTPUT_CHARS) return clean;
  const head = Math.floor(MAX_STEP_OUTPUT_CHARS * 0.6);
  const tail = MAX_STEP_OUTPUT_CHARS - head - 80;
  return (
    clean.slice(0, head) +
    `\n\n[... ${clean.length - head - tail} chars omitted by tmem storage cap ...]\n\n` +
    clean.slice(clean.length - tail)
  );
}

function clipText(text, maxChars) {
  const s = String(text || "");
  if (s.length <= maxChars) return s;
  return (
    s.slice(0, Math.max(200, maxChars - 140)) +
    `\n\n[... truncated at ${maxChars} chars; pass narrower \`seq_from\`/\`seq_to\`, \`turn\`, or \`query\` to inspect remaining details ...]`
  );
}

export class SessionEpisode {
  constructor(data = {}) {
    this.episode_id = data.episode_id || "";
    this.session_id = data.session_id || "default";
    this.compaction_id = data.compaction_id || "";
    this.trigger = data.trigger || "compaction";
    this.prev_episode_id = data.prev_episode_id ?? null;
    this.phase_index = Number(data.phase_index ?? 1);
    this.sub_episode_index = Number(data.sub_episode_index ?? 1);
    this.sub_episode_total = Number(data.sub_episode_total ?? 1);
    this.seq_from = Number(data.seq_from ?? 0);
    this.seq_to = Number(data.seq_to ?? 0);
    this.shadowed_seqs = Array.isArray(data.shadowed_seqs) ? [...data.shadowed_seqs] : [];
    this.created_at = data.created_at || new Date().toISOString();
    this.last_accessed_at = data.last_accessed_at || this.created_at;
    this.git_commit = data.git_commit ?? null;
    this.dsh_summary = data.dsh_summary || "";
    this.phase_intent = data.phase_intent || "";
    this.decisions = Array.isArray(data.decisions) ? [...data.decisions] : [];
    this.touched_files = Array.isArray(data.touched_files) ? [...data.touched_files] : [];
    this.referenced_files = Array.isArray(data.referenced_files) ? [...data.referenced_files] : [];
    this.touched_symbols = Array.isArray(data.touched_symbols) ? [...data.touched_symbols] : [];
    this.error_signatures = Array.isArray(data.error_signatures) ? [...data.error_signatures] : [];
    this.verification_cmds = Array.isArray(data.verification_cmds) ? [...data.verification_cmds] : [];
    this.critical_spine = Array.isArray(data.critical_spine)
      ? data.critical_spine.map((s) => ({ ...s }))
      : [];
    this.l2_causal_repairs = Array.isArray(data.l2_causal_repairs)
      ? data.l2_causal_repairs.map((r) =>
          r instanceof CausalRepairPair ? r : CausalRepairPair.fromDict(r)
        )
      : [];
    this.turns = Array.isArray(data.turns) ? data.turns.map((t) => ({ ...t })) : [];
    this.diff_snippets = Array.isArray(data.diff_snippets)
      ? data.diff_snippets.map((d) => ({ ...d }))
      : [];
    this.steps = Array.isArray(data.steps) ? data.steps.map((s) => ({ ...s })) : [];
    this.raw_steps_count = Number(data.raw_steps_count ?? this.steps.length);
    this.sliced_steps_count = Number(data.sliced_steps_count ?? this.critical_spine.length);
  }

  toDict() {
    return {
      episode_id: this.episode_id,
      session_id: this.session_id,
      compaction_id: this.compaction_id,
      trigger: this.trigger,
      prev_episode_id: this.prev_episode_id,
      phase_index: this.phase_index,
      sub_episode_index: this.sub_episode_index,
      sub_episode_total: this.sub_episode_total,
      seq_from: this.seq_from,
      seq_to: this.seq_to,
      shadowed_seqs: [...this.shadowed_seqs],
      created_at: this.created_at,
      last_accessed_at: this.last_accessed_at,
      git_commit: this.git_commit,
      dsh_summary: this.dsh_summary,
      phase_intent: this.phase_intent,
      decisions: [...this.decisions],
      touched_files: [...this.touched_files],
      referenced_files: [...this.referenced_files],
      touched_symbols: [...this.touched_symbols],
      error_signatures: [...this.error_signatures],
      verification_cmds: [...this.verification_cmds],
      critical_spine: this.critical_spine.map((s) => ({ ...s })),
      l2_causal_repairs: this.l2_causal_repairs.map((r) => r.toDict()),
      turns: this.turns.map((t) => ({ ...t })),
      diff_snippets: this.diff_snippets.map((d) => ({ ...d })),
      steps: this.steps.map((s) => ({ ...s })),
      raw_steps_count: this.raw_steps_count,
      sliced_steps_count: this.sliced_steps_count,
    };
  }

  toJSON() {
    return this.toDict();
  }

  static fromDict(data = {}) {
    return new SessionEpisode(data);
  }
}

/**
 * Compile a parsed DSH span into an immutable SessionEpisode (plus an optional
 * high-signal TracePlaybook candidate if non-trivial).
 */
export function compileEpisodeFromSpan(
  spanData,
  {
    compactionId = "",
    trigger = "compaction",
    dshSummary = "",
    shadowedSeqs = [],
    workspaceRoot = "",
    useGitGroundTruth = false,
    prevEpisodeId = null,
    phaseIndex = 1,
    subEpisodeIndex = 1,
    subEpisodeTotal = 1,
  } = {}
) {
  const session = spanData?.session;
  if (!session) return { episode: null, candidatePlaybook: null };

  const rawActions = session.actions || [];
  const seqs =
    Array.isArray(shadowedSeqs) && shadowedSeqs.length > 0
      ? shadowedSeqs.map(Number).filter((n) => Number.isInteger(n))
      : [];
  const seqFrom =
    seqs.length > 0
      ? Math.min(...seqs)
      : Number(spanData.seq_from ?? rawActions[0]?.step_idx ?? 0);
  const seqTo =
    seqs.length > 0
      ? Math.max(...seqs)
      : Number(spanData.seq_to ?? rawActions[rawActions.length - 1]?.step_idx ?? seqFrom);

  const slicer = new BackwardTraceSlicer();
  const pb = slicer.sliceSession(session, useGitGroundTruth, {
    pruneTrailingUnverified: true,
  });

  const mutatedFiles = new Set();
  const referencedFiles = new Set();
  const allSymbols = new Set();
  const allErrors = new Set();
  const stepTurnMap = spanData.step_turn_map || new Map();

  for (const act of rawActions) {
    if (act.target_file) {
      referencedFiles.add(act.target_file);
      if (act.kind === "MUTATE") mutatedFiles.add(act.target_file);
    }
    for (const sym of act.symbols_touched || []) allSymbols.add(sym);
    if (act.error_signature) allErrors.add(act.error_signature);
  }
  if (pb) {
    for (const f of pb.touched_files || []) {
      mutatedFiles.add(f);
      referencedFiles.add(f);
    }
    for (const s of pb.touched_symbols || []) allSymbols.add(s);
    for (const e of pb.error_signatures || []) allErrors.add(e);
  }

  const touchedFiles =
    mutatedFiles.size > 0 ? Array.from(mutatedFiles).sort() : Array.from(referencedFiles).sort();

  const cleanDshSummary = scrubSecrets(String(dshSummary || "")).trim().slice(0, 3000);

  let phaseIntent = pb?.problem_intent || "";
  if (
    !phaseIntent ||
    phaseIntent === "non_task" ||
    phaseIntent.startsWith("Modify ")
  ) {
    const firstTurnPrompt = (spanData.turns || []).find((t) => t.user_prompt)?.user_prompt || "";
    if (firstTurnPrompt) {
      phaseIntent = firstTurnPrompt.slice(0, 300);
    } else if (cleanDshSummary) {
      const firstMeaningful = cleanDshSummary
        .split(/\r?\n/)
        .map((l) => l.trim())
        .find((l) => l && !l.startsWith("#") && !l.startsWith("<"));
      if (firstMeaningful) phaseIntent = firstMeaningful.slice(0, 300);
    } else if (trigger === "compaction_prune") {
      const toolLabel = rawActions[0]?.command || rawActions[0]?.target_file || rawActions[0]?.tool_name || "tool output";
      phaseIntent = `Pruned output archive: ${String(toolLabel).slice(0, 200)}`;
    } else if (referencedFiles.size > 0) {
      phaseIntent = `Explore/Inspect ${Array.from(referencedFiles).slice(0, 3).join(", ")}`;
    } else {
      phaseIntent = `Session ${session.session_id} phase [${seqFrom}..${seqTo}]`;
    }
  }

  // Merge critical spine: prefer slicer's L0 critical path, fallback to compact raw steps
  let criticalSpine = pb?.l0_critical_path ? [...pb.l0_critical_path] : [];
  if (criticalSpine.length === 0 && rawActions.length > 0) {
    criticalSpine = rawActions.slice(0, 10).map((a) => ({
      step: a.step_idx,
      action: a.kind,
      target: a.target_file || undefined,
      command: a.command || undefined,
      symbols: (a.symbols_touched || []).slice(0, 4),
      error: a.error_signature || undefined,
      rationale: a.rationale || undefined,
    }));
  }

  const steps = rawActions.map((a) => ({
    seq: a.step_idx,
    turn_idx: stepTurnMap.get(a.step_idx) ?? 0,
    kind: a.kind,
    tool_name: a.tool_name,
    target_file: a.target_file,
    command: a.command,
    symbols_touched: [...(a.symbols_touched || [])],
    exit_code: a.exit_code,
    error_signature: a.error_signature,
    rationale: a.rationale,
    output_snippet: a.output_snippet || "",
    output_full: boundStepOutput(a.raw_output || a.output_snippet || ""),
  }));

  const fpSource = `${session.session_id}|${compactionId}|${trigger}|${seqFrom}|${seqTo}|${phaseIntent}`;
  const episodeId =
    "ep_" + createHash("sha256").update(fpSource, "utf-8").digest("hex").slice(0, 10);

  const nowIso = new Date().toISOString();
  const episode = new SessionEpisode({
    episode_id: episodeId,
    session_id: session.session_id || "default",
    compaction_id: String(compactionId || ""),
    trigger,
    prev_episode_id: prevEpisodeId,
    phase_index: phaseIndex,
    sub_episode_index: subEpisodeIndex,
    sub_episode_total: subEpisodeTotal,
    seq_from: seqFrom,
    seq_to: seqTo,
    shadowed_seqs: seqs,
    created_at: nowIso,
    last_accessed_at: nowIso,
    git_commit: pb?.git_commit ?? null,
    dsh_summary: cleanDshSummary,
    phase_intent: phaseIntent,
    decisions: [...(spanData.decisions || [])].slice(0, 12),
    touched_files: touchedFiles,
    referenced_files: Array.from(referencedFiles).sort(),
    touched_symbols: Array.from(allSymbols).sort(),
    error_signatures: Array.from(allErrors),
    verification_cmds: pb?.l1_verification_cmds || [],
    critical_spine: criticalSpine,
    l2_causal_repairs: pb?.l2_causal_repairs || [],
    turns: spanData.turns || [],
    diff_snippets: (spanData.diff_snippets || []).slice(0, 40),
    steps,
    raw_steps_count: rawActions.length,
    sliced_steps_count: criticalSpine.length,
  });

  const candidatePlaybook = pb && !pb.is_trivial ? pb : null;
  return { episode, candidatePlaybook };
}

function fileModuleClusterKey(fp) {
  const norm = String(fp || "").replace(/\\/g, "/").replace(/^\.\//, "");
  if (!norm) return "";
  const stem = path.posix
    .parse(norm)
    .name.toLowerCase()
    .replace(/^(?:test_|tests_)|(?:_test|\.test|\.spec)$/g, "");
  const parts = norm.split("/").filter(Boolean);
  if (parts.length >= 2 && !["src", "lib", "app", "tests", "test"].includes(parts[0].toLowerCase())) {
    return `${parts[0].toLowerCase()}:${stem}`;
  }
  if (parts.length >= 3) {
    return `${parts[1].toLowerCase()}:${stem}`;
  }
  return stem;
}

function fileSetsAreDisjointClusters(filesA, filesB) {
  if (!filesA.size || !filesB.size) return false;
  for (const f of filesA) {
    if (filesB.has(f)) return false;
  }
  const clustersA = new Set(Array.from(filesA).map(fileModuleClusterKey).filter(Boolean));
  const clustersB = new Set(Array.from(filesB).map(fileModuleClusterKey).filter(Boolean));
  if (!clustersA.size || !clustersB.size) return false;
  for (const c of clustersA) {
    if (clustersB.has(c)) return false;
  }
  return true;
}

/**
 * Automatically segment a large multi-intent compaction span into 1..maxSubEpisodes
 * cohesive sub-spans when explicit commit milestones or disjoint file-cluster boundaries exist.
 */
export function segmentSpanIntoSubSpans(spanData, { maxSubEpisodes = 3 } = {}) {
  const turns = Array.isArray(spanData?.turns) ? spanData.turns : [];
  if (!spanData?.session || turns.length < 4 || maxSubEpisodes <= 1) {
    return [spanData];
  }

  const splitAfterTurnIndices = [];
  let segStart = 0;

  for (let i = 0; i < turns.length - 1; i++) {
    if (splitAfterTurnIndices.length >= maxSubEpisodes - 1) break;
    const curTurn = turns[i];
    const hasGitCommit = (curTurn.commands || []).some((c) => /\bgit\s+commit\b/.test(String(c)));

    if (hasGitCommit && i - segStart >= 0 && turns.length - 1 - i >= 1) {
      splitAfterTurnIndices.push(i);
      segStart = i + 1;
      continue;
    }

    // Disjoint file cluster + user intent boundary (require >=2 turns in current segment & >=2 remaining)
    if (i - segStart + 1 >= 2 && turns.length - 1 - i >= 2) {
      const segFiles = new Set();
      for (let k = segStart; k <= i; k++) {
        for (const f of turns[k].files || []) segFiles.add(f);
      }
      const nextFiles = new Set();
      for (let k = i + 1; k < turns.length; k++) {
        for (const f of turns[k].files || []) nextFiles.add(f);
      }
      const nextHasPrompt = Boolean(String(turns[i + 1].user_prompt || "").trim());
      if (nextHasPrompt && fileSetsAreDisjointClusters(segFiles, nextFiles)) {
        splitAfterTurnIndices.push(i);
        segStart = i + 1;
      }
    }
  }

  if (splitAfterTurnIndices.length === 0) {
    return [spanData];
  }

  const boundaries = [...splitAfterTurnIndices, turns.length - 1];
  const subSpans = [];
  let startIdx = 0;
  const origSession = spanData.session;
  const allActions = origSession.actions || [];
  const allDiffs = spanData.diff_snippets || [];
  const origStepTurnMap = spanData.step_turn_map || new Map();

  for (const endIdx of boundaries) {
    const subTurns = turns.slice(startIdx, endIdx + 1);
    startIdx = endIdx + 1;
    if (subTurns.length === 0) continue;

    const subSeqFrom = Number(subTurns[0].seq_from ?? spanData.seq_from ?? 0);
    const subSeqTo = Number(subTurns[subTurns.length - 1].seq_to ?? subSeqFrom);
    const turnIdxSet = new Set(subTurns.map((t) => Number(t.turn_idx)));

    const subActions = allActions.filter((a) => {
      const mappedTurn = origStepTurnMap.get(a.step_idx);
      if (mappedTurn !== undefined) return turnIdxSet.has(Number(mappedTurn));
      return a.step_idx >= subSeqFrom && a.step_idx <= subSeqTo;
    });

    const subSession = new ParsedSession({
      agent_name: origSession.agent_name || "dsh",
      session_id: origSession.session_id || "default",
      workspace_root: origSession.workspace_root || "",
      user_intents: subTurns.map((t) => t.user_prompt).filter(Boolean),
      assistant_conclusions: subTurns.map((t) => t.assistant_summary).filter(Boolean),
      actions: subActions,
      active_errors: Array.from(new Set(subTurns.flatMap((t) => t.errors || []))),
    });

    const subStepTurnMap = new Map();
    for (const a of subActions) {
      if (origStepTurnMap.has(a.step_idx)) {
        subStepTurnMap.set(a.step_idx, origStepTurnMap.get(a.step_idx));
      }
    }

    const subDiffs = allDiffs.filter(
      (d) => turnIdxSet.has(Number(d.turn_idx)) || (d.seq >= subSeqFrom && d.seq <= subSeqTo)
    );
    const subDecisions = Array.from(new Set(subTurns.flatMap((t) => t.decisions || [])));

    subSpans.push({
      session: subSession,
      turns: subTurns,
      diff_snippets: subDiffs,
      decisions: subDecisions,
      step_turn_map: subStepTurnMap,
      seq_from: subSeqFrom,
      seq_to: subSeqTo,
    });
  }

  return subSpans.length > 0 ? subSpans : [spanData];
}

/**
 * Compile one or multiple cohesive SessionEpisodes from a compaction span.
 */
export function compileEpisodesFromSpan(
  spanData,
  {
    compactionId = "",
    trigger = "compaction",
    dshSummary = "",
    shadowedSeqs = [],
    workspaceRoot = "",
    useGitGroundTruth = false,
    maxSubEpisodes = 3,
  } = {}
) {
  const subSpans =
    trigger === "compaction" && maxSubEpisodes > 1
      ? segmentSpanIntoSubSpans(spanData, { maxSubEpisodes })
      : [spanData];

  const episodes = [];
  const candidatePlaybooks = [];
  const total = subSpans.length;
  let prevEpId = null;

  for (let idx = 0; idx < total; idx++) {
    const sub = subSpans[idx];
    const subSeqs =
      total === 1
        ? shadowedSeqs
        : (shadowedSeqs || []).filter((s) => s >= sub.seq_from && s <= sub.seq_to);
    const { episode, candidatePlaybook } = compileEpisodeFromSpan(sub, {
      compactionId,
      trigger,
      dshSummary: idx === total - 1 ? dshSummary : "",
      shadowedSeqs: subSeqs.length > 0 ? subSeqs : [sub.seq_from, sub.seq_to],
      workspaceRoot,
      useGitGroundTruth,
      prevEpisodeId: prevEpId,
      subEpisodeIndex: idx + 1,
      subEpisodeTotal: total,
    });
    if (episode) {
      episodes.push(episode);
      prevEpId = episode.episode_id;
    }
    if (candidatePlaybook) {
      candidatePlaybooks.push(candidatePlaybook);
    }
  }

  return {
    episodes,
    episode: episodes[episodes.length - 1] || null,
    candidatePlaybooks,
    candidatePlaybook: candidatePlaybooks[candidatePlaybooks.length - 1] || null,
  };
}

/**
 * Progressive Disclosure Renderer for SessionEpisodes (L0 -> L3).
 */
export function formatEpisodeProgressiveView(
  episode,
  {
    level = "slice",
    turn = null,
    seqFrom = null,
    seqTo = null,
    query = "",
    file = "",
    maxChars = DEFAULT_MAX_RENDER_CHARS,
  } = {}
) {
  if (!episode) return "Episode not found.";
  const lvl = String(level || "slice").toLowerCase().trim();
  const ep = episode instanceof SessionEpisode ? episode : SessionEpisode.fromDict(episode);

  if (lvl === "index" || lvl === "l0") {
    const filesStr = (ep.touched_files || []).slice(0, 4).join(", ") || "None";
    const symsStr = (ep.touched_symbols || []).slice(0, 5).join(", ") || "None";
    const phaseTag = `Phase: #${ep.phase_index}${
      ep.prev_episode_id ? ` (prev: ${ep.prev_episode_id})` : ""
    }`;
    return (
      `[${ep.episode_id}] Session: ${ep.session_id} | ${phaseTag} | Trigger: ${ep.trigger} | Seq: ${ep.seq_from}..${ep.seq_to} | ` +
      `Turns: ${ep.turns.length} | Intent: ${ep.phase_intent} | Files: ${filesStr} | Symbols: ${symsStr}`
    );
  }

  if (lvl === "diff") {
    const filterFile = String(file || "").trim().toLowerCase();
    const diffs = (ep.diff_snippets || []).filter(
      (d) => !filterFile || String(d.file || "").toLowerCase().includes(filterFile)
    );
    if (diffs.length === 0) {
      return `# Episode ${ep.episode_id}: No recorded edit diffs${filterFile ? ` matching '${file}'` : ""}.`;
    }
    const lines = [
      `### 🧩 Episode Diffs: \`${ep.episode_id}\` (Seq ${ep.seq_from}..${ep.seq_to})`,
    ];
    for (const d of diffs) {
      lines.push(`\n- **${d.file}** (Turn #${d.turn_idx}, seq ${d.seq}, via \`${d.tool_name}\`):`);
      lines.push("```diff\n" + d.snippet + "\n```");
    }
    return clipText(lines.join("\n"), maxChars);
  }

  if (lvl === "turn" || lvl === "l2") {
    return clipText(renderTurnOrWindowView(ep, { turn, seqFrom, seqTo, query }), maxChars);
  }

  if (lvl === "raw" || lvl === "l3") {
    return clipText(renderRawWindowView(ep, { turn, seqFrom, seqTo, query, maxChars }), maxChars);
  }

  // Default: L1 ("slice" / "playbook")
  const lines = [
    `### 📦 Episode Slice (L1): \`${ep.episode_id}\` [${ep.trigger}]`,
    `- **Session**: \`${ep.session_id}\` | **Phase**: \`#${ep.phase_index}\`${
      ep.prev_episode_id ? ` (prev: \`${ep.prev_episode_id}\`)` : ""
    } | **Seq Range**: \`${ep.seq_from}..${ep.seq_to}\`${
      ep.compaction_id ? ` | **Compaction**: \`${ep.compaction_id}\`` : ""
    }`,
    `- **Phase Intent**: ${ep.phase_intent}`,
  ];
  if (ep.touched_files.length > 0) {
    lines.push(`- **Files**: ${ep.touched_files.slice(0, 10).map((f) => `\`${f}\``).join(", ")}`);
  }
  if (ep.touched_symbols.length > 0) {
    lines.push(`- **Symbols**: ${ep.touched_symbols.slice(0, 12).map((s) => `\`${s}\``).join(", ")}`);
  }
  if (ep.error_signatures.length > 0) {
    lines.push(`- **Errors**: ${ep.error_signatures.slice(0, 5).map((e) => `\`${e}\``).join("; ")}`);
  }
  if (ep.decisions.length > 0) {
    lines.push("- **Key Decisions & Constraints**:");
    for (const dec of ep.decisions.slice(0, 8)) {
      lines.push(`  - ${dec}`);
    }
  }
  if (ep.turns.length > 0) {
    lines.push("- **Turn Outline (TOC)**:");
    for (const t of ep.turns.slice(0, 12)) {
      const promptShort = String(t.user_prompt || "(continuation)").slice(0, 90);
      const ansShort = t.assistant_summary ? ` -> _${t.assistant_summary.slice(0, 90)}_` : "";
      lines.push(`  - Turn #${t.turn_idx} [seq ${t.seq_from}..${t.seq_to}]: ${promptShort}${ansShort}`);
    }
  }
  if (ep.critical_spine.length > 0) {
    lines.push("- **Critical Execution Spine**:");
    for (const s of ep.critical_spine.slice(0, 12)) {
      const tgt = s.target || s.command || "";
      const syms = s.symbols && s.symbols.length ? ` (${s.symbols.join(", ")})` : "";
      const err = s.error ? ` ⚠️ \`${s.error}\`` : "";
      const diff = s.diff_summary ? ` [${s.diff_summary}]` : "";
      lines.push(`  - seq ${s.step} \`${s.action}\`: \`${tgt}\`${syms}${err}${diff}`);
    }
  }
  if (ep.l2_causal_repairs.length > 0) {
    lines.push("- **L2 Causal Repairs**:");
    for (const r of ep.l2_causal_repairs) {
      lines.push(`  - \`${r.error_signature}\` => \`${r.repair_action}\` (verify: \`${r.verification_cmd}\`)`);
    }
  }
  if (ep.verification_cmds.length > 0) {
    lines.push(`- **Verification Cmds**: ${ep.verification_cmds.map((c) => `\`${c}\``).join(", ")}`);
  }
  lines.push(
    `\n> 🔍 *Drill down: \`tmem_expand(id="${ep.episode_id}", level="turn", turn=<n>)\`, \`level="diff"\`, or \`level="raw", seq_from=..., seq_to=...\`.*`
  );
  return clipText(lines.join("\n"), maxChars);
}

function selectStepsByFilter(ep, { turn, seqFrom, seqTo, query }) {
  let steps = [...(ep.steps || [])];
  if (turn !== null && turn !== undefined && Number.isFinite(Number(turn))) {
    const tNum = Number(turn);
    const turnMeta = ep.turns.find((t) => Number(t.turn_idx) === tNum);
    steps = steps.filter(
      (s) =>
        Number(s.turn_idx) === tNum ||
        (turnMeta && s.seq >= turnMeta.seq_from && s.seq <= turnMeta.seq_to)
    );
  }
  if (seqFrom !== null && seqFrom !== undefined && Number.isFinite(Number(seqFrom))) {
    const sf = Number(seqFrom);
    steps = steps.filter((s) => Number(s.seq) >= sf);
  }
  if (seqTo !== null && seqTo !== undefined && Number.isFinite(Number(seqTo))) {
    const st = Number(seqTo);
    steps = steps.filter((s) => Number(s.seq) <= st);
  }
  if (query && String(query).trim()) {
    const qTerms = Array.from(tokenizeInvertedTerms(query).keys());
    if (qTerms.length > 0) {
      const matchedIndices = [];
      for (let i = 0; i < steps.length; i++) {
        const hay = `${steps[i].target_file || ""} ${steps[i].command || ""} ${
          (steps[i].symbols_touched || []).join(" ")
        } ${steps[i].error_signature || ""} ${steps[i].rationale || ""} ${
          steps[i].output_full || steps[i].output_snippet || ""
        }`.toLowerCase();
        if (qTerms.some((t) => hay.includes(t))) {
          matchedIndices.push(i);
        }
      }
      if (matchedIndices.length > 0) {
        const kept = new Set();
        for (const idx of matchedIndices) {
          for (let k = Math.max(0, idx - 1); k <= Math.min(steps.length - 1, idx + 1); k++) {
            kept.add(k);
          }
        }
        steps = steps.filter((_, idx) => kept.has(idx));
      }
    }
  }
  return steps;
}

function renderTurnOrWindowView(ep, { turn, seqFrom, seqTo, query }) {
  const lines = [
    `### 🔎 Episode Turn/Window Detail (L2): \`${ep.episode_id}\``,
  ];

  let targetTurns = ep.turns || [];
  if (turn !== null && turn !== undefined && Number.isFinite(Number(turn))) {
    targetTurns = targetTurns.filter((t) => Number(t.turn_idx) === Number(turn));
  } else if (query && String(query).trim()) {
    const qTerms = Array.from(tokenizeInvertedTerms(query).keys());
    const matched = targetTurns.filter((t) => {
      const hay = `${t.user_prompt} ${t.assistant_summary} ${(t.decisions || []).join(" ")} ${
        (t.files || []).join(" ")
      } ${(t.symbols || []).join(" ")} ${(t.errors || []).join(" ")}`.toLowerCase();
      return qTerms.some((qt) => hay.includes(qt));
    });
    if (matched.length > 0) targetTurns = matched;
  }

  for (const t of targetTurns.slice(0, 6)) {
    lines.push(`\n#### Turn #${t.turn_idx} [seq ${t.seq_from}..${t.seq_to}]`);
    if (t.user_prompt) lines.push(`- **User**: ${t.user_prompt}`);
    if (t.decisions && t.decisions.length > 0) {
      lines.push(`- **Decisions**: ${t.decisions.join(" | ")}`);
    }
    if (t.assistant_summary) lines.push(`- **Outcome**: ${t.assistant_summary}`);
  }

  const steps = selectStepsByFilter(ep, { turn, seqFrom, seqTo, query });
  if (steps.length > 0) {
    lines.push("\n#### Actions & Outputs");
    for (const s of steps.slice(0, 20)) {
      const tgt = s.target_file || s.command || s.tool_name;
      const exitStr = s.exit_code !== null && s.exit_code !== undefined ? ` [exit=${s.exit_code}]` : "";
      lines.push(`- **seq ${s.seq}** (Turn #${s.turn_idx}) \`${s.kind}:${s.tool_name}\` \`${tgt}\`${exitStr}`);
      if (s.error_signature) lines.push(`  - Error: \`${s.error_signature}\``);
      const out = s.output_snippet || (s.output_full ? s.output_full.slice(0, 350) : "");
      if (out) lines.push(`  - Output: ${out.replace(/\n+/g, " ").slice(0, 350)}`);
    }
  }

  const stepSeqSet = new Set(steps.map((s) => s.seq));
  const relDiffs = (ep.diff_snippets || []).filter(
    (d) =>
      (turn !== null && turn !== undefined && Number(d.turn_idx) === Number(turn)) ||
      stepSeqSet.has(d.seq)
  );
  if (relDiffs.length > 0) {
    lines.push("\n#### Diffs in Scope");
    for (const d of relDiffs.slice(0, 6)) {
      lines.push(`- **${d.file}** (seq ${d.seq}):\n\`\`\`diff\n${d.snippet.slice(0, 450)}\n\`\`\``);
    }
  }

  return lines.join("\n");
}

function windowOutputAroundQuery(outText, query, maxStepChars = 3600) {
  if (!outText || outText.length <= maxStepChars || !query || !String(query).trim()) {
    return outText;
  }
  const qTerms = Array.from(tokenizeInvertedTerms(query).keys()).sort(
    (a, b) => b.length - a.length
  );
  if (qTerms.length === 0) return outText;
  const low = outText.toLowerCase();
  let matchIdx = -1;
  for (const qt of qTerms) {
    const idx = low.indexOf(qt);
    if (idx >= 0) {
      matchIdx = idx;
      break;
    }
  }
  if (matchIdx < 0) return outText;
  const half = Math.floor(maxStepChars / 2);
  const start = Math.max(0, matchIdx - half);
  const end = Math.min(outText.length, start + maxStepChars);
  const prefix = start > 0 ? `[... ${start} chars before match omitted ...]\n` : "";
  const suffix =
    end < outText.length ? `\n[... ${outText.length - end} chars after match omitted ...]` : "";
  return prefix + outText.slice(start, end) + suffix;
}

function renderRawWindowView(
  ep,
  { turn, seqFrom, seqTo, query, maxChars = DEFAULT_MAX_RENDER_CHARS }
) {
  const steps = selectStepsByFilter(ep, { turn, seqFrom, seqTo, query });
  const lines = [
    `### 🎬 Episode Raw Replay (L3): \`${ep.episode_id}\` (Session: \`${ep.session_id}\`, ${steps.length}/${ep.steps.length} step(s))`,
  ];
  if (steps.length === 0) {
    lines.push("_No matching steps in specified filter range._");
    return lines.join("\n");
  }
  const perStepBudget = Math.max(1200, Math.floor((maxChars - 400) / Math.max(1, steps.length)));
  for (const s of steps) {
    const exitStr = s.exit_code !== null && s.exit_code !== undefined ? ` [exit=${s.exit_code}]` : "";
    lines.push(`\n--- Step seq #${s.seq} (Turn #${s.turn_idx}) | ${s.kind} | ${s.tool_name}${exitStr} ---`);
    if (s.target_file) {
      const syms = s.symbols_touched?.length ? ` (${s.symbols_touched.join(", ")})` : "";
      lines.push(`Target: ${s.target_file}${syms}`);
    }
    if (s.command) lines.push(`Command: ${s.command}`);
    if (s.error_signature) lines.push(`Error: ${s.error_signature}`);
    if (s.rationale) lines.push(`Rationale: ${s.rationale}`);
    if (s.output_full || s.output_snippet) {
      const rawOut = s.output_full || s.output_snippet;
      const focusedOut = query
        ? windowOutputAroundQuery(rawOut, query, perStepBudget)
        : rawOut;
      lines.push(`Output:\n${focusedOut}`);
    }
  }
  return lines.join("\n");
}

/**
 * Format compact L0 inverted-index notice injected into DSH right after a compact-checkpoint.
 * Accepts a single SessionEpisode or an array of sub-episodes from the compaction, plus
 * optional `sessionTimeline` listing all chronological phases archived in the session.
 */
export function formatCompactedIndexNotice(episodeOrEpisodes, { sessionTimeline = [] } = {}) {
  const epListRaw = Array.isArray(episodeOrEpisodes)
    ? episodeOrEpisodes.filter(Boolean)
    : episodeOrEpisodes
      ? [episodeOrEpisodes]
      : [];
  if (epListRaw.length === 0) return "";

  const epList = epListRaw.map((e) =>
    e instanceof SessionEpisode ? e : SessionEpisode.fromDict(e)
  );
  const primary = epList[epList.length - 1];
  const allIds = epList.map((e) => e.episode_id).join(",");
  const totalTurns = epList.reduce((s, e) => s + (e.turns?.length || 0), 0);
  const minSeq = Math.min(...epList.map((e) => e.seq_from));
  const maxSeq = Math.max(...epList.map((e) => e.seq_to));
  const rawSteps = epList.reduce((s, e) => s + (e.raw_steps_count || 0), 0);
  const slicedSteps = epList.reduce((s, e) => s + (e.sliced_steps_count || 0), 0);

  const lines = [
    `<tmem_compacted_index compaction_id="${primary.compaction_id}" episode_id="${allIds}">`,
    `Compacted phase archived & inverted-indexed (${totalTurns} turn(s), seq ${minSeq}..${maxSeq}, ${rawSteps} -> ${slicedSteps} spine steps${
      epList.length > 1 ? `, segmented into ${epList.length} cohesive sub-episodes` : ""
    }):`,
  ];

  if (epList.length === 1) {
    lines.push(`- Phase Intent: ${primary.phase_intent}`);
  } else {
    lines.push("- Sub-Episodes in Compaction:");
    for (const sub of epList) {
      lines.push(
        `  - [${sub.episode_id}] (Phase #${sub.phase_index}, seq ${sub.seq_from}..${sub.seq_to}): ${sub.phase_intent}`
      );
    }
  }

  const mergedFiles = Array.from(new Set(epList.flatMap((e) => e.touched_files || [])));
  const mergedSyms = Array.from(new Set(epList.flatMap((e) => e.touched_symbols || [])));
  const mergedErrs = Array.from(new Set(epList.flatMap((e) => e.error_signatures || [])));
  const mergedDecs = Array.from(new Set(epList.flatMap((e) => e.decisions || [])));

  if (mergedFiles.length > 0) {
    lines.push(`- Files: ${mergedFiles.slice(0, 8).join(", ")}`);
  }
  if (mergedSyms.length > 0) {
    lines.push(`- Symbols: ${mergedSyms.slice(0, 10).join(", ")}`);
  }
  if (mergedErrs.length > 0) {
    lines.push(`- Errors: ${mergedErrs.slice(0, 4).join("; ")}`);
  }
  if (mergedDecs.length > 0) {
    lines.push(`- Key Decisions: ${mergedDecs.slice(0, 3).join(" | ")}`);
  }

  const timeline =
    Array.isArray(sessionTimeline) && sessionTimeline.length > 1
      ? sessionTimeline
      : epList.length > 1
        ? epList
        : [];
  if (timeline.length > 1) {
    const chainStr = timeline
      .slice(-6)
      .map(
        (t, idx) =>
          `#${t.phase_index || idx + 1} [${t.episode_id}, seq ${t.seq_from}..${t.seq_to}: "${String(
            t.phase_intent || ""
          ).slice(0, 45)}"]`
      )
      .join(" -> ");
    lines.push(`- Session Timeline (${timeline.length} phases): ${chainStr}`);
  } else if (primary.prev_episode_id) {
    lines.push(
      `- Session Timeline: prev \`${primary.prev_episode_id}\` -> current \`${primary.episode_id}\` (Phase #${primary.phase_index})`
    );
  }

  lines.push(
    `- Progressive Recovery: Call \`tmem_search(query=...)\` across keywords/symbols/errors or \`tmem_expand(id="${primary.episode_id}", level="slice|turn|diff|raw")\` to inspect full details lost during compaction.`
  );
  lines.push("</tmem_compacted_index>");
  return lines.join("\n");
}

/**
 * Format L0 inverted-index search hits for LLM tool & CLI display.
 */
export function formatSearchHits(hits, query = "") {
  if (!hits || hits.length === 0) {
    return `No matching episode or playbook memories found${query ? ` for query "${query}"` : ""}.`;
  }
  const lines = [
    `Found ${hits.length} inverted-index match(es)${query ? ` for "${query}"` : ""}:`,
  ];
  for (const h of hits) {
    const spanTag = h.doc_type === "episode" ? `seq ${h.seq_from}..${h.seq_to}` : "playbook";
    const rolesTag = h.matched_roles.length ? ` | Roles: [${h.matched_roles.join(", ")}]` : "";
    const termsTag = h.matched_terms.length ? ` | Terms: ${h.matched_terms.join(", ")}` : "";
    lines.push(
      `\n- **[${h.doc_id}]** (${h.doc_type}, session: \`${h.session_id || "-"}\`, ${spanTag}, score: ${h.score}${rolesTag})`
    );
    lines.push(`  - Intent: ${h.intent}`);
    if (h.touched_files.length > 0) {
      lines.push(`  - Files: ${h.touched_files.slice(0, 5).join(", ")}`);
    }
    if (termsTag) lines.push(`  - Matched${termsTag}`);
    if (h.matched_turns && h.matched_turns.length > 0) {
      const turnDesc = h.matched_turns
        .map(
          (t) =>
            `Turn #${t.turn_idx} [seq ${t.seq_from}..${t.seq_to}]${
              t.prompt_preview ? `: "${t.prompt_preview}"` : ""
            }`
        )
        .join("; ");
      lines.push(`  - Pinpointed Turn(s): ${turnDesc}`);
      lines.push(
        `  - Expand: \`tmem_expand(id="${h.doc_id}", level="turn", turn=${h.matched_turns[0].turn_idx})\` or \`level="slice"\``
      );
    } else {
      lines.push(`  - Expand: \`tmem_expand(id="${h.doc_id}", level="${h.doc_type === "episode" ? "slice" : "playbook"}")\``);
    }
  }
  return lines.join("\n");
}
