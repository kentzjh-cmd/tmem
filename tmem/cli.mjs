#!/usr/bin/env node
/**
 * Headless CLI, `/tmem` Slash-Command Interceptor, and Autonomous Milestone Detector for tmem.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
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
  const store = new ProjectMemoryStore(workspaceRoot);
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
  "error",
  "format",
  "data-dir",
  "level",
]);

function assignValueFlag(flags, key, val) {
  if (key === "workspace") flags.workspace = val;
  else if (key === "agents") flags.agents = val;
  else if (key === "agent") flags.agent = val;
  else if (key === "transcript") flags.transcript = val;
  else if (key === "scope") flags.scope = val;
  else if (key === "intent") flags.intent = val;
  else if (key === "query") flags.query = val;
  else if (key === "file") flags.file.push(val);
  else if (key === "error") flags.error.push(val);
  else if (key === "format") flags.format = val;
  else if (key === "data-dir") flags.dataDir = val;
  else if (key === "level") flags.level = val;
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
    intent: null,
    fullSession: false,
    force: false,
    quiet: false,
    query: "",
    file: [],
    error: [],
    format: "markdown",
    prune: false,
    dataDir: null,
    json: false,
    level: "playbook",
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
    io.log(JSON.stringify(stats, null, 2));
    return 0;
  }

  if (subcommand === "status") {
    const store = new ProjectMemoryStore(flags.workspace);
    const projCount = existsSync(store.project_traces_dir)
      ? readdirSync(store.project_traces_dir).filter((f) => f.endsWith(".json")).length
      : 0;
    const telemetry = computeTelemetrySummary(flags.workspace);
    io.log(
      JSON.stringify(
        {
          project_store: store.project_dir,
          project_traces: projCount,
          telemetry,
        },
        null,
        2
      )
    );
    return 0;
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

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  process.exitCode = main();
}
