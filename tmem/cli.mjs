#!/usr/bin/env node
/**
 * Headless CLI, `/tmem` Slash-Command Interceptor, and Autonomous Milestone Detector for tmem.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { VERSION, logger, setDebug } from "./index.mjs";
import { getAdapter, DshAdapter } from "./adapters/index.mjs";
import { BenchmarkEvaluator, formatMarkdownReport } from "./evaluate.mjs";
import { formatInjectedContext } from "./guards.mjs";
import {
  SLASH_TMEM_RE,
  handleDshHook,
  loadRuntimeState,
  registerDefaultCaptureFn,
  saveRuntimeState,
} from "./hooks/index.mjs";
import { installInvisibleIntegration } from "./installer.mjs";
import { recordVerifiedEnvCmd } from "./mem_doc.mjs";
import { TracePlaybook } from "./models.mjs";
import {
  formatProgressiveView,
  formatRawTraceDiff,
  formatRawTraceReplay,
} from "./raw_storage.mjs";
import { BackwardTraceSlicer } from "./slicer.mjs";
import { ProjectMemoryStore } from "./store.mjs";
import { computeTelemetrySummary } from "./telemetry.mjs";
import { expandMemoryTarget } from "./dsh_tools.mjs";
import { compileEpisodeFromSpan, formatSearchHits } from "./episodes.mjs";

function readStdinJson() {
  if (process.stdin.isTTY) return {};
  try {
    const raw = readFileSync(0, "utf-8").trim();
    if (!raw) return {};
    return JSON.parse(raw);
  } catch (err) {
    logger.debug("Failed to read JSON from stdin:", err?.message || err);
    return {};
  }
}

export function executeCapture({
  workspaceRoot,
  agentName = "dsh",
  transcriptPath = null,
  scope = "project",
  customIntent = null,
  fullSession = false,
  sessionId = null,
  force = false,
} = {}) {
  const adapter = getAdapter(agentName);
  let tPath;
  if (transcriptPath) {
    tPath = transcriptPath;
  } else if (adapter instanceof DshAdapter) {
    tPath = adapter.discoverLatestTranscript(workspaceRoot, sessionId);
  } else {
    tPath = adapter.discoverLatestTranscript(workspaceRoot);
  }

  if (!tPath || !existsSync(tPath)) {
    return { status: "skipped", reason: "no_transcript_found" };
  }

  let effSessionId = String(sessionId || "").trim() || "default";
  let state = loadRuntimeState(workspaceRoot, effSessionId);
  let minStep = fullSession ? 0 : Number(state.last_captured_step_idx || 0);

  let session = adapter.parseTranscript(tPath, workspaceRoot, minStep);
  if (!sessionId && session.session_id && session.session_id !== effSessionId) {
    effSessionId = session.session_id;
    state = loadRuntimeState(workspaceRoot, effSessionId);
    const newMinStep = fullSession ? 0 : Number(state.last_captured_step_idx || 0);
    if (newMinStep !== minStep) {
      minStep = newMinStep;
      session = adapter.parseTranscript(tPath, workspaceRoot, minStep);
    }
  }

  if (session.actions.length === 0 && minStep > 0) {
    session = adapter.parseTranscript(tPath, workspaceRoot, 0);
  }

  if (customIntent) {
    session.user_intents.push(customIntent.trim());
  } else {
    const cleanIntents = session.user_intents.filter(
      (u) => !SLASH_TMEM_RE.test(u.trim())
    );
    if (cleanIntents.length > 0) {
      session.user_intents = cleanIntents;
    }
  }

  const slicer = new BackwardTraceSlicer();
  const pb = slicer.sliceSession(session, true);
  if (!pb) {
    return { status: "skipped", reason: "no_effective_actions" };
  }

  // Always archive a phase SessionEpisode in `.tmem/episodes/` so even routine/single-file
  // phases remain searchable via the inverted index (`1统一存放` policy).
  const store = new ProjectMemoryStore(workspaceRoot);
  let savedEpisodeId = null;
  try {
    const spanData =
      adapter instanceof DshAdapter && typeof adapter.parseTranscriptSpan === "function"
        ? adapter.parseTranscriptSpan(tPath, workspaceRoot, { minStepIdx: minStep })
        : {
            session,
            turns: [],
            diff_snippets: [],
            decisions: [],
            step_turn_map: new Map(),
            seq_from: minStep,
            seq_to: session.actions.reduce((mx, a) => Math.max(mx, a.step_idx || 0), minStep),
          };
    if (customIntent && spanData.session) {
      spanData.session.user_intents = [...(spanData.session.user_intents || []), customIntent.trim()];
    }
    const { episode } = compileEpisodeFromSpan(spanData, {
      compactionId: `manual_${effSessionId}_${minStep}`,
      trigger: "manual_capture",
      workspaceRoot,
      useGitGroundTruth: true,
    });
    if (episode) {
      if (customIntent) {
        episode.phase_intent = customIntent.trim().slice(0, 300);
      }
      savedEpisodeId = store.saveEpisode(episode);
    }
  } catch (_err) {
    // non-fatal episode archival fallback
  }

  if (pb.is_trivial && !force) {
    if (pb.l1_verification_cmds && pb.l1_verification_cmds.length > 0) {
      recordVerifiedEnvCmd(
        workspaceRoot,
        pb.l1_verification_cmds,
        session.session_id || pb.trace_id
      );
    }
    return {
      status: "skipped_trivial",
      episode_id: savedEpisodeId,
      reason:
        pb.pitfalls && pb.pitfalls.length > 0
          ? pb.pitfalls[0]
          : "Routine/unverified edit without causal repair or multi-file verification",
      problem_intent: pb.problem_intent,
      touched_files: pb.touched_files,
      message:
        "Trace was evaluated as routine/unverified (default-deny gate). Pass --force to capture anyway.",
    };
  }

  if (customIntent) {
    pb.problem_intent = customIntent.trim().slice(0, 300);
    const fingerprint = `${pb.problem_intent}|${pb.touched_files.join(
      "|"
    )}|${pb.error_signatures.join("|")}`;
    pb.trace_id =
      "tr_" + createHash("sha256").update(fingerprint, "utf-8").digest("hex").slice(0, 10);
  }

  const rawTrace = slicer.buildRawTrace(session, pb);
  const [projId] = store.savePlaybook(pb, {
    targetScope: "project",
    rawTrace,
  });

  let persistedPb = pb;
  if (projId) {
    const persistedFile = path.join(store.project_traces_dir, `${projId}.json`);
    if (existsSync(persistedFile)) {
      try {
        persistedPb = TracePlaybook.fromDict(
          JSON.parse(readFileSync(persistedFile, "utf-8"))
        );
      } catch (_err) {
        // ignore
      }
    }
  }

  const maxStep = session.actions.reduce(
    (mx, a) => Math.max(mx, a.step_idx || 0),
    minStep
  );
  state.last_captured_step_idx = maxStep;
  state.uncommitted_mutations = [];
  state.verified_since_mutation = false;
  state.milestone_prompted_for_mutations = [];
  saveRuntimeState(workspaceRoot, state);

  return {
    status: "compiled",
    agent: agentName,
    scope: "project",
    episode_id: savedEpisodeId,
    project_trace_id: projId,
    raw_trace_path: persistedPb.raw_trace_path,
    problem_intent: persistedPb.problem_intent,
    touched_files: persistedPb.touched_files,
    is_trivial: persistedPb.is_trivial,
    confidence: persistedPb.confidence,
    raw_steps: persistedPb.raw_steps_count,
    sliced_steps: persistedPb.sliced_steps_count,
    compression_ratio: `${persistedPb.raw_steps_count} -> ${persistedPb.sliced_steps_count}`,
  };
}

registerDefaultCaptureFn(executeCapture);

function printHelp() {
  console.log(`usage: tmem [-h] [--version] [--verbose] <subcommand> [options]

Deterministic Backward-Slicing Execution Trace Memory Compiler (.tmem/)

Subcommands:
  init        Install DSH plugin hooks, skill, and git hooks
  hook        Headless hook handler invoked by DeepSeek Harness (dsh)
  capture     Compile and save current session execution trace to .tmem/
  lookup      Query execution memory with zero-token pre-flight guards
  gc          Run guard validation metabolism and prune stale paths
  status      Show execution memory store status and statistics
  list        List stored execution trace playbooks
  forget      Remove an execution trace playbook by trace_id
  evaluate    Run benchmark suite on sample or historical trajectories
  inspect     Progressive disclosure inspection of a trace
  search      Search compacted episodes & playbooks via inverted index
  episodes    List compacted session episodes stored in .tmem/episodes/
  expand      Progressive disclosure expansion of an episode or trace (L0 -> L3)
  replay      Forensic chronological terminal replay of raw execution trace
  diff        View recorded unified git diff patch for a trace
  sync        Synchronize indexes, run GC, and regenerate MEMORIES.md living index
  telemetry   Show runtime L2 repair & L1 co-change adoption telemetry`);
}

const VALUE_FLAGS = new Set([
  "workspace",
  "agents",
  "agent",
  "transcript",
  "scope",
  "intent",
  "query",
  "file",
  "symbol",
  "error",
  "role",
  "session",
  "limit",
  "turn",
  "seq-from",
  "seq-to",
  "max-chars",
  "max-episodes-mb",
  "max-episode-days",
  "format",
  "data-dir",
  "level",
]);

function assignValueFlag(flags, key, val) {
  if (key === "workspace") flags.workspace = val;
  else if (key === "agents") flags.agents = val;
  else if (key === "agent") flags.agent = val;
  else if (key === "transcript") flags.transcript = val;
  else if (key === "scope") {
    flags.scope = val;
    flags.scopeExplicit = true;
  } else if (key === "intent") flags.intent = val;
  else if (key === "query") flags.query = val;
  else if (key === "file") flags.file.push(val);
  else if (key === "symbol") flags.symbol.push(val);
  else if (key === "error") flags.error.push(val);
  else if (key === "role") flags.role.push(val);
  else if (key === "session") flags.session = val;
  else if (key === "limit") flags.limit = Number(val);
  else if (key === "turn") flags.turn = Number(val);
  else if (key === "seq-from") flags.seqFrom = Number(val);
  else if (key === "seq-to") flags.seqTo = Number(val);
  else if (key === "max-chars") flags.maxChars = Number(val);
  else if (key === "max-episodes-mb") flags.maxEpisodesMb = Number(val);
  else if (key === "max-episode-days") flags.maxEpisodeDays = Number(val);
  else if (key === "format") flags.format = val;
  else if (key === "data-dir") flags.dataDir = val;
  else if (key === "level") {
    flags.level = val;
    flags.levelExplicit = true;
  }
}

function parseCliArgs(argv) {
  const flags = {
    verbose: false,
    version: false,
    help: false,
    workspace: process.cwd(),
    agents: "dsh",
    agent: "dsh",
    transcript: null,
    scope: "project",
    scopeExplicit: false,
    intent: null,
    fullSession: false,
    force: false,
    quiet: false,
    query: "",
    file: [],
    symbol: [],
    error: [],
    role: [],
    session: null,
    limit: 10,
    turn: null,
    seqFrom: null,
    seqTo: null,
    maxChars: 6000,
    maxEpisodesMb: 200,
    maxEpisodeDays: 30,
    format: "markdown",
    prune: false,
    dataDir: null,
    json: false,
    level: "playbook",
    levelExplicit: false,
  };
  const positionals = [];

  for (let i = 0; i < argv.length; i++) {
    const tok = argv[i];
    if (tok === "--help" || tok === "-h") {
      flags.help = true;
    } else if (tok === "--version") {
      flags.version = true;
    } else if (tok === "--verbose" || tok === "-v") {
      flags.verbose = true;
    } else if (tok === "--full-session") {
      flags.fullSession = true;
    } else if (tok === "--force") {
      flags.force = true;
    } else if (tok === "--quiet") {
      flags.quiet = true;
    } else if (tok === "--prune") {
      flags.prune = true;
    } else if (tok === "--json") {
      flags.json = true;
    } else if (tok === "-f") {
      if (positionals[0] === "lookup" && i + 1 < argv.length) {
        flags.file.push(argv[++i]);
      } else {
        flags.force = true;
      }
    } else if (tok === "-q") {
      if (positionals[0] === "lookup" && i + 1 < argv.length) {
        flags.query = argv[++i];
      } else {
        flags.quiet = true;
      }
    } else if (tok === "-e" && i + 1 < argv.length) {
      flags.error.push(argv[++i]);
    } else if (tok.startsWith("--")) {
      const eqIdx = tok.indexOf("=");
      const key = eqIdx >= 0 ? tok.slice(2, eqIdx) : tok.slice(2);
      if (eqIdx >= 0) {
        assignValueFlag(flags, key, tok.slice(eqIdx + 1));
      } else if (VALUE_FLAGS.has(key) && i + 1 < argv.length) {
        assignValueFlag(flags, key, argv[++i]);
      }
    } else {
      positionals.push(tok);
    }
  }

  return { flags, positionals };
}

export function main(argv = process.argv.slice(2), io = { log: console.log, err: console.error }) {
  const { flags, positionals } = parseCliArgs(argv);

  if (flags.version) {
    io.log(`tmem ${VERSION}`);
    return 0;
  }
  if (flags.help || positionals.length === 0) {
    printHelp();
    return 0;
  }

  if (flags.verbose || process.env.TMEM_DEBUG === "1") {
    setDebug(true);
  }

  const subcommand = positionals[0];
  const rest = positionals.slice(1);

  if (subcommand === "init") {
    const agentList = flags.agents
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean);
    const info = installInvisibleIntegration(flags.workspace, agentList);
    io.log(JSON.stringify(info, null, 2));
    return 0;
  }

  if (subcommand === "hook") {
    const agent = rest[0];
    const event = rest[1];
    const payload = readStdinJson();
    if (agent === "dsh" && event) {
      const out = handleDshHook(event, payload, executeCapture);
      io.log(JSON.stringify(out));
      return 0;
    }
    io.log("{}");
    return 0;
  }

  if (subcommand === "capture") {
    const res = executeCapture({
      workspaceRoot: flags.workspace,
      agentName: flags.agent,
      transcriptPath: flags.transcript,
      scope: flags.scope,
      customIntent: flags.intent,
      fullSession: flags.fullSession,
      force: flags.force,
    });
    if (!flags.quiet) {
      io.log(JSON.stringify(res, null, 2));
    }
    return ["compiled", "skipped_trivial"].includes(res.status) ? 0 : 1;
  }

  if (subcommand === "lookup") {
    const store = new ProjectMemoryStore(flags.workspace);
    const queryPos = rest[0] || "";
    const effectiveQuery = (flags.query || queryPos).trim();
    const evaluated = store.cascadingLookup({
      queryText: effectiveQuery,
      filePaths: flags.file,
      errorSignatures: flags.error,
    });
    if (flags.format === "json") {
      io.log(
        JSON.stringify(
          evaluated.map((ev) => ({
            trace_id: ev.playbook.trace_id,
            scope: ev.playbook.scope,
            effective_tier: ev.effective_tier,
            l0_valid: ev.l0_valid,
            failed_guards: ev.failed_guards,
            playbook: ev.playbook.toDict(),
          })),
          null,
          2
        )
      );
    } else {
      io.log(formatInjectedContext(evaluated));
    }
    return 0;
  }

  if (subcommand === "gc") {
    const store = new ProjectMemoryStore(flags.workspace);
    const stats = store.runMetabolismGc(Boolean(flags.prune));
    const epStats = store.runEpisodeGc({
      maxMb: flags.maxEpisodesMb,
      maxAgeDays: flags.maxEpisodeDays,
    });
    io.log(JSON.stringify({ ...stats, episodes: epStats }, null, 2));
    return 0;
  }

  if (subcommand === "status") {
    const store = new ProjectMemoryStore(flags.workspace);
    const projCount = existsSync(store.project_traces_dir)
      ? readdirSync(store.project_traces_dir).filter((f) => f.endsWith(".json")).length
      : 0;
    const epCount = existsSync(store.project_episodes_dir)
      ? readdirSync(store.project_episodes_dir).filter((f) => f.endsWith(".json")).length
      : 0;
    const telemetry = computeTelemetrySummary(flags.workspace);
    io.log(
      JSON.stringify(
        {
          project_store: store.project_dir,
          project_traces: projCount,
          project_episodes: epCount,
          telemetry,
        },
        null,
        2
      )
    );
    return 0;
  }

  if (subcommand === "search") {
    const store = new ProjectMemoryStore(flags.workspace);
    const q = (flags.query || rest.join(" ")).trim();
    const hits = store.searchMemory({
      query: q,
      files: flags.file,
      symbols: flags.symbol,
      errors: flags.error,
      roles: flags.role,
      scope: flags.scopeExplicit ? flags.scope : "all",
      sessionId: flags.session,
      limit: flags.limit,
    });
    if (flags.json || flags.format === "json") {
      io.log(JSON.stringify(hits, null, 2));
    } else {
      io.log(formatSearchHits(hits, q));
    }
    return 0;
  }

  if (subcommand === "episodes") {
    const store = new ProjectMemoryStore(flags.workspace);
    const eps = store.listEpisodes({
      sessionId: flags.session,
      limit: flags.limit,
    });
    io.log(JSON.stringify(eps, null, 2));
    return 0;
  }

  if (subcommand === "expand") {
    const targetId = (rest[0] || "").trim();
    if (!targetId) {
      io.err("Usage: tmem expand <episode_id|trace_id> [--level index|slice|turn|diff|raw]");
      return 1;
    }
    const level = flags.levelExplicit ? flags.level : rest[1] || "slice";
    const out = expandMemoryTarget(flags.workspace, {
      id: targetId,
      level,
      turn: flags.turn,
      seq_from: flags.seqFrom,
      seq_to: flags.seqTo,
      query: flags.query,
      file: flags.file[0] || "",
      max_chars: flags.maxChars,
    });
    io.log(out);
    return out.startsWith("Memory target '") ? 1 : 0;
  }

  if (subcommand === "list") {
    const store = new ProjectMemoryStore(flags.workspace);
    const items = store.listPlaybooks(flags.scope);
    io.log(JSON.stringify(items, null, 2));
    return 0;
  }

  if (subcommand === "forget") {
    const tid = rest[0] || "";
    const store = new ProjectMemoryStore(flags.workspace);
    const res = store.deletePlaybook(tid);
    io.log(JSON.stringify(res, null, 2));
    return 0;
  }

  if (subcommand === "evaluate") {
    const evaluator = new BenchmarkEvaluator(flags.dataDir);
    const summary = evaluator.runEvaluation();
    if (flags.json) {
      io.log(JSON.stringify(summary, null, 2));
    } else if (!flags.quiet) {
      io.log(formatMarkdownReport(summary));
    }
    return summary.all_passed ? 0 : 1;
  }

  if (subcommand === "inspect") {
    const store = new ProjectMemoryStore(flags.workspace);
    const tid = (rest[0] || "").trim();
    let targetPb = null;
    if (tid) {
      for (const pb of store.getAllProjectPlaybooks()) {
        if (
          pb.trace_id === tid ||
          pb.trace_id.startsWith(tid) ||
          (pb.source_session_ids || []).some((s) => s === tid || s.startsWith(tid))
        ) {
          targetPb = pb;
          break;
        }
      }
    }
    if (!targetPb) {
      io.err(`Error: Trace '${tid}' not found in project memory.`);
      return 1;
    }
    const rawTr = store.getRawTrace(targetPb.trace_id);
    if (flags.json) {
      io.log(
        JSON.stringify(
          {
            playbook: targetPb.toDict(),
            raw_trace: rawTr ? rawTr.toDict() : null,
          },
          null,
          2
        )
      );
    } else {
      io.log(formatProgressiveView(targetPb, rawTr, flags.level));
    }
    return 0;
  }

  if (subcommand === "replay") {
    const tid = rest[0] || "";
    const store = new ProjectMemoryStore(flags.workspace);
    const rawTr = store.getRawTrace(tid);
    if (!rawTr) {
      io.err(`Error: Raw execution trace for '${tid}' not found in .tmem/raw/.`);
      return 1;
    }
    io.log(formatRawTraceReplay(rawTr));
    return 0;
  }

  if (subcommand === "diff") {
    const tid = rest[0] || "";
    const store = new ProjectMemoryStore(flags.workspace);
    const rawTr = store.getRawTrace(tid);
    if (!rawTr) {
      io.err(`Error: Raw execution trace for '${tid}' not found in .tmem/raw/.`);
      return 1;
    }
    io.log(formatRawTraceDiff(rawTr));
    return 0;
  }

  if (subcommand === "sync") {
    const store = new ProjectMemoryStore(flags.workspace);
    store.syncProjectIndexIfNeeded();
    const gcStats = store.runMetabolismGc();
    const docPath = store.syncMemoriesMarkdown();
    io.log(
      JSON.stringify(
        {
          status: "synchronized",
          gc_stats: gcStats,
          living_index: docPath,
        },
        null,
        2
      )
    );
    return 0;
  }

  if (subcommand === "telemetry") {
    io.log(JSON.stringify(computeTelemetrySummary(flags.workspace), null, 2));
    return 0;
  }

  printHelp();
  return 0;
}

function safeRealpath(targetPath) {
  try {
    return realpathSync(targetPath);
  } catch {
    return path.resolve(targetPath);
  }
}

const isEntrypoint =
  Boolean(process.argv[1]) &&
  safeRealpath(process.argv[1]) === safeRealpath(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  process.exitCode = main();
}
