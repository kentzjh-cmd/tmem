/**
 * Raw Execution Trajectory Storage & Forensic Replay Engine.
 */

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { logger } from "./index.mjs";
import { scrubSecrets } from "./adapters/base.mjs";
import { ActionKind, RawExecutionTrace } from "./models.mjs";

export class RawTraceStore {
  constructor(workspaceRoot) {
    this.workspace_root = path.resolve(workspaceRoot);
    this.raw_dir = path.join(this.workspace_root, ".tmem", "raw");
  }

  ensureInitialized() {
    mkdirSync(this.raw_dir, { recursive: true });
  }

  saveRawTrace(rawTrace) {
    this.ensureInitialized();
    const targetPath = path.join(this.raw_dir, `${rawTrace.trace_id}.raw.json`);
    const tempPath = path.join(this.raw_dir, `.${rawTrace.trace_id}.raw.tmp`);
    const payload =
      typeof rawTrace.toDict === "function" ? rawTrace.toDict() : rawTrace;
    writeFileSync(tempPath, JSON.stringify(payload, null, 2), "utf-8");
    renameSync(tempPath, targetPath);
    return targetPath;
  }

  loadRawTrace(traceId) {
    const tid = String(traceId || "").trim();
    if (!tid || !existsSync(this.raw_dir)) return null;

    const exact = path.join(this.raw_dir, `${tid}.raw.json`);
    if (existsSync(exact)) {
      try {
        return RawExecutionTrace.fromDict(JSON.parse(readFileSync(exact, "utf-8")));
      } catch (err) {
        logger.debug("Failed loading raw trace", exact, err);
        return null;
      }
    }

    try {
      const matches = readdirSync(this.raw_dir)
        .filter((name) => name.startsWith(tid) && name.endsWith(".raw.json"))
        .sort();
      if (matches.length > 0) {
        const matchPath = path.join(this.raw_dir, matches[0]);
        return RawExecutionTrace.fromDict(JSON.parse(readFileSync(matchPath, "utf-8")));
      }
    } catch (err) {
      logger.debug("Failed loading raw trace prefix", tid, err);
    }
    return null;
  }

  deleteRawTrace(traceId, exact = false) {
    const tid = String(traceId || "").trim();
    if (!tid || !existsSync(this.raw_dir)) return false;

    if (exact) {
      const target = path.join(this.raw_dir, `${tid}.raw.json`);
      if (existsSync(target)) {
        try {
          unlinkSync(target);
          return true;
        } catch (err) {
          logger.debug("Failed deleting raw trace", target, err);
        }
      }
      return false;
    }

    let deleted = false;
    try {
      for (const name of readdirSync(this.raw_dir)) {
        if (name.startsWith(tid) && name.endsWith(".raw.json")) {
          try {
            unlinkSync(path.join(this.raw_dir, name));
            deleted = true;
          } catch (err) {
            logger.debug("Failed deleting raw trace", name, err);
          }
        }
      }
    } catch (_err) {
      // ignore
    }
    return deleted;
  }

  listRawTraces() {
    if (!existsSync(this.raw_dir)) return [];
    const items = [];
    for (const name of readdirSync(this.raw_dir).sort()) {
      if (!name.endsWith(".raw.json")) continue;
      const p = path.join(this.raw_dir, name);
      try {
        const data = JSON.parse(readFileSync(p, "utf-8"));
        items.push({
          trace_id: data.trace_id || name.replace(/\.raw\.json$/, ""),
          agent_source: data.agent_source || "",
          created_at: data.created_at || "",
          total_steps: data.total_steps || 0,
          sliced_steps: (data.sliced_step_indices || []).length,
          has_diff: Boolean(data.diff_patch),
          file_size: statSync(p).size,
        });
      } catch (err) {
        logger.debug("Failed listing raw trace", p, err);
      }
    }
    return items;
  }
}

export function formatRawTraceReplay(rawTrace, showAll = true) {
  const lines = [
    "════════════════════════════════════════════════════════════════════════════",
    `🎬 Forensic Trace Replay: [${rawTrace.trace_id}]`,
    `   Session: ${rawTrace.session_id} | Agent: ${rawTrace.agent_source} | Commit: ${rawTrace.git_commit || "HEAD"} | Time: ${rawTrace.created_at}`,
    `   Trajectory Stats: ${rawTrace.total_steps} total steps -> ${(rawTrace.sliced_step_indices || []).length} critical path steps`,
  ];
  if (rawTrace.user_intents && rawTrace.user_intents.length > 0) {
    lines.push(`   Task Intent: ${rawTrace.user_intents[0]}`);
  }
  lines.push("────────────────────────────────────────────────────────────────────────────");

  const slicedIndices = new Set(rawTrace.sliced_step_indices || []);
  const actions = rawTrace.actions || [];

  for (let i = 0; i < actions.length; i++) {
    const act = actions[i] || {};
    const stepIdx = act.step_idx ?? i + 1;
    const isCritical = slicedIndices.has(stepIdx) || slicedIndices.has(i);
    const toolName = act.tool_name || "unknown";
    const kindStr = act.kind || ActionKind.OTHER;
    const target = act.target_file || "";
    const cmd = act.command || "";
    const exitCode = act.exit_code;
    const errSig = act.error_signature;
    const syms = act.symbols_touched || [];
    const outputSnip = act.output_snippet || "";

    const badge = isCritical
      ? `🟢 [CRITICAL PATH: ${kindStr}]`
      : `⚪ [NOISE PRUNED: ${kindStr}]`;

    lines.push(`\nStep #${stepIdx} ${badge} tool: ${toolName}`);

    if (target) {
      const symClause = syms.length ? ` (symbols: ${syms.slice(0, 4).join(", ")})` : "";
      lines.push(`   Target: ${target}${symClause}`);
    }
    if (cmd) {
      const codeClause =
        exitCode !== null && exitCode !== undefined ? ` [exit: ${exitCode}]` : "";
      lines.push(`   Command: ${cmd}${codeClause}`);
    }
    if (errSig) {
      lines.push(`   ⚠️  Error Encountered: ${errSig}`);
    }
    if (outputSnip && (errSig || isCritical || !showAll)) {
      const cleanSnip = scrubSecrets(outputSnip.trim().replace(/\n/g, " ")).slice(0, 140);
      lines.push(`   Output: ${cleanSnip}...`);
    }
  }

  lines.push("\n════════════════════════════════════════════════════════════════════════════");
  return lines.join("\n");
}

export function formatRawTraceDiff(rawTrace) {
  if (!rawTrace.diff_patch) {
    return `# Trace ${rawTrace.trace_id}: No unified diff patch recorded.`;
  }
  return rawTrace.diff_patch;
}

export function formatProgressiveView(playbook, rawTrace = null, level = "playbook") {
  const lvl = String(level || "playbook").toLowerCase().trim();

  if (lvl === "index") {
    const filesStr = (playbook.touched_files || []).slice(0, 3).join(", ") || "None";
    const verifyCmd =
      playbook.l1_verification_cmds && playbook.l1_verification_cmds.length > 0
        ? playbook.l1_verification_cmds[playbook.l1_verification_cmds.length - 1]
        : "None";
    const rawRef = playbook.raw_trace_path ? `raw/${playbook.trace_id}.raw.json` : "None";
    const sessStr =
      playbook.source_session_ids && playbook.source_session_ids.length > 0
        ? playbook.source_session_ids.slice(-3).join(", ")
        : rawTrace
          ? rawTrace.session_id
          : "None";
    return (
      `[${playbook.trace_id}] Session(s): ${sessStr} | Scope: ${playbook.scope} | Status: ${playbook.status} | ` +
      `Intent: ${playbook.problem_intent} | Files: ${filesStr} | ` +
      `Verify: \`${verifyCmd}\` | Confidence: ${Number(playbook.confidence).toFixed(2)} | Raw: ${rawRef}`
    );
  }

  if (lvl === "diff") {
    if (rawTrace) return formatRawTraceDiff(rawTrace);
    return `# Trace ${playbook.trace_id}: Raw trace not loaded to display diff.`;
  }

  if (lvl === "raw") {
    if (rawTrace) return formatRawTraceReplay(rawTrace, true);
    return (
      `Raw trace for ${playbook.trace_id} not found in .tmem/raw/. ` +
      `Playbook critical path: ${(playbook.l0_critical_path || []).length} steps.`
    );
  }

  let sessIds = [...(playbook.source_session_ids || [])];
  if (sessIds.length === 0 && rawTrace && rawTrace.session_id) {
    sessIds = [rawTrace.session_id];
  }

  const docLines = [
    `### 📋 Playbook: \`${playbook.trace_id}\` (${String(playbook.scope || "project").toUpperCase()})`,
    `- **Session ID(s)**: ${sessIds.map((s) => `\`${s}\``).join(", ") || "None"}`,
    `- **Intent**: ${playbook.problem_intent}`,
  ];
  if (playbook.semantic_summary) {
    docLines.push(`- **Summary**: ${playbook.semantic_summary}`);
  }
  if (playbook.root_cause) {
    docLines.push(`- **Root Cause**: ${playbook.root_cause}`);
  }
  docLines.push(
    `- **Created At**: ${playbook.created_at} (Commit: ${playbook.git_commit || "HEAD"})`
  );
  docLines.push(
    `- **Files Touched**: ${(playbook.touched_files || []).join(", ") || "None"}`
  );

  if (playbook.guards && playbook.guards.length > 0) {
    docLines.push("- **Speculative Pre-flight Guards**:");
    for (const g of playbook.guards) {
      docLines.push(
        `  - \`${g.probe_type}\` on \`${g.target}\` (expected: \`${g.expected}\`)`
      );
    }
  }

  if (playbook.l0_critical_path && playbook.l0_critical_path.length > 0) {
    docLines.push("- **L0 Critical Path**:");
    for (const s of playbook.l0_critical_path) {
      const act = s.action;
      const tgt = s.target || s.command || "";
      const syms = s.symbols;
      const symClause = syms && syms.length ? ` (symbols: ${syms.join(", ")})` : "";
      const diffClause = s.diff_summary ? ` [diff: ${s.diff_summary}]` : "";
      const ratClause = s.rationale ? ` — ${s.rationale}` : "";
      docLines.push(`  - \`${act}\`: \`${tgt}\`${symClause}${diffClause}${ratClause}`);
    }
  }

  if (playbook.l1_cochange_topology && playbook.l1_cochange_topology.length > 0) {
    docLines.push("- **L1 Co-Change Topology**:");
    for (const topo of playbook.l1_cochange_topology) {
      docLines.push(`  - ${topo}`);
    }
  }

  if (playbook.l1_verification_cmds && playbook.l1_verification_cmds.length > 0) {
    docLines.push(
      `- **Verification Command**: \`${playbook.l1_verification_cmds[playbook.l1_verification_cmds.length - 1]}\``
    );
  }

  if (playbook.l2_causal_repairs && playbook.l2_causal_repairs.length > 0) {
    docLines.push("- **L2 Universal Causal Repairs**:");
    for (const r of playbook.l2_causal_repairs) {
      docLines.push(
        `  - Error \`${r.error_signature}\` -> Repair \`${r.repair_action}\` -> Verify \`${r.verification_cmd}\``
      );
    }
  }

  if (playbook.raw_trace_path) {
    docLines.push(
      `\n> 🔍 *Raw trace available: \`${playbook.raw_trace_path}\`. Run \`tmem inspect ${playbook.trace_id} --level raw\` for full replay.*`
    );
  }

  return docLines.join("\n");
}
