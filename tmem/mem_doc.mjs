/**
 * Living Memory Index Generator (MEMORIES.md / MEM.md).
 */

import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  renameSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { computeCochangeStats } from "./cochange.mjs";
import { evaluateGuards } from "./guards.mjs";

const TRAIL_REDIRECT_RE = /(?:\s*(?:\|\s*(?:head|tail|grep|tee)\b.*|2>&1|>\s*\S+))+\s*$/;

export function normalizeCommandTemplate(cmd) {
  let cleaned = String(cmd || "").replace(/[\r\n\t]+/g, " ").trim();
  cleaned = cleaned.replace(TRAIL_REDIRECT_RE, "").trim();
  cleaned = cleaned.replace(/\s+/g, " ");
  return cleaned;
}

function envFactsPath(workspaceRoot) {
  return path.join(workspaceRoot, ".tmem", "env_facts.json");
}

function loadEnvFacts(workspaceRoot) {
  const filePath = envFactsPath(workspaceRoot);
  if (!existsSync(filePath)) return {};
  try {
    const data = JSON.parse(readFileSync(filePath, "utf-8"));
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const out = {};
      for (const [k, v] of Object.entries(data)) {
        if (Array.isArray(v)) out[String(k)] = v.map(String);
      }
      return out;
    }
  } catch (_err) {
    // ignore
  }
  return {};
}

export function recordVerifiedEnvCmd(workspaceRoot, commands, sourceId) {
  if (!workspaceRoot || !commands || commands.length === 0) return;
  const tmemDir = path.join(workspaceRoot, ".tmem");
  mkdirSync(tmemDir, { recursive: true });
  const facts = loadEnvFacts(workspaceRoot);
  let changed = false;

  for (const cmd of commands) {
    const norm = normalizeCommandTemplate(cmd);
    if (!norm || norm.length > 120) continue;
    if (!facts[norm]) facts[norm] = [];
    if (!facts[norm].includes(sourceId)) {
      facts[norm].push(sourceId);
      facts[norm] = facts[norm].slice(-20);
      changed = true;
    }
  }
  if (changed) {
    try {
      writeFileSync(
        envFactsPath(workspaceRoot),
        JSON.stringify(facts, null, 2),
        "utf-8"
      );
    } catch (_err) {
      // ignore
    }
  }
}

export function extractMicroSkills(
  playbooks,
  { minTraces = 2, maxItems = 6, workspaceRoot = null } = {}
) {
  const cmdTraces = new Map();
  const addCmd = (cmd, id) => {
    if (!cmdTraces.has(cmd)) cmdTraces.set(cmd, new Set());
    cmdTraces.get(cmd).add(id);
  };

  if (workspaceRoot) {
    for (const [cmd, srcIds] of Object.entries(loadEnvFacts(workspaceRoot))) {
      const norm = normalizeCommandTemplate(cmd);
      if (norm && norm.length <= 120) {
        for (const sid of srcIds) addCmd(norm, sid);
      }
    }
  }

  for (const pb of playbooks || []) {
    if (pb.status === "invalidated" || pb.confidence < 0.3) continue;
    for (const cmd of pb.l1_verification_cmds || []) {
      const norm = normalizeCommandTemplate(cmd);
      if (norm && norm.length <= 120) addCmd(norm, pb.trace_id);
    }
    for (const rep of pb.l2_causal_repairs || []) {
      if (rep.is_env_or_toolchain && rep.repair_action) {
        const norm = normalizeCommandTemplate(rep.repair_action);
        if (norm && norm.length <= 120) addCmd(norm, pb.trace_id);
      }
    }
  }

  const validated = [];
  for (const [cmd, tids] of cmdTraces.entries()) {
    if (tids.size >= minTraces) {
      validated.push([cmd, tids.size]);
    }
  }
  validated.sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    if (a[0].length !== b[0].length) return a[0].length - b[0].length;
    return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0;
  });
  return validated.slice(0, maxItems).map(([cmd]) => cmd);
}

export function formatMicroSkillsBlock(skills) {
  if (!skills || skills.length === 0) return "";
  const lines = [
    "<tmem_env_facts>",
    "Cross-task verified repository commands & toolchain facts:",
    ...skills.map((s) => `- \`${s}\``),
    "</tmem_env_facts>",
  ];
  return lines.join("\n");
}

export function sanitizeTableCell(text, { maxChars = 120, asCode = false } = {}) {
  if (!text) return asCode ? "`-`" : "-";
  let cleaned = String(text).replace(/[\r\n\t]+/g, " ").trim();
  cleaned = cleaned.replace(/(?<!\\)\|/g, "\\|");
  if (cleaned.length > maxChars) {
    cleaned = cleaned.slice(0, maxChars - 3).trimEnd() + "...";
  }
  if (!cleaned) return asCode ? "`-`" : "-";
  if (asCode) {
    cleaned = cleaned.replace(/`/g, "'");
    return `\`${cleaned}\``;
  }
  return cleaned;
}

export function renderMemoriesMarkdown(playbooks, workspaceRoot) {
  const lines = [
    "# 🧠 Project Execution Memories (`MEMORIES.md`)",
    "",
    "> **Maintained autonomously by [tmem](https://github.com/kentzjh-cmd/tmem)**.",
    "> Zero-token deterministic requirement & behavior summaries compiled from sliced historical DSH sessions (with inline `session_id` lineage), co-change topologies, and causal error repairs.",
    "> Both DSH and human engineers can navigate this index to inspect verified execution paths without re-reading raw session logs or paying LLM summarization tokens.",
    "",
    "## ⚡ Progressive Memory Disclosure Tiers",
    "- **Tier 0 (Catalog Index)**: This `MEMORIES.md` file provides zero-token exploration and instant navigation.",
    "- **Tier 1 (Playbooks)**: Detailed pre-flight guarded playbooks with AST/filesystem assertions in [`.tmem/traces/`](.tmem/traces/).",
    "- **Tier 2 (Raw Execution Traces)**: Full-fidelity forensic logs, tool inputs/outputs, and diff patches in [`.tmem/raw/`](.tmem/raw/).",
    "",
    "---",
    "",
  ];

  const microSkills = extractMicroSkills(playbooks, { workspaceRoot });
  const activePlaybooks = (playbooks || []).filter(
    (pb) => !pb.is_trivial && pb.status !== "invalidated"
  );

  if (microSkills.length > 0) {
    lines.push("## 0. Verified Env & Test Commands (Cross-Task Micro-Skills)", "");
    for (const skill of microSkills) {
      lines.push(`- \`${skill}\``);
    }
    lines.push("", "---", "");
  }

  lines.push("## 1. Active Verified Execution Playbooks (L0/L1)", "");

  if (activePlaybooks.length === 0) {
    lines.push(
      "_No project execution traces recorded yet. Traces will be automatically compiled on `/tmem`, milestone completions, or `git commit`._",
      ""
    );
  } else {
    lines.push(
      "| Trace ID | Tier Status | Problem Intent / Summary | Key Files & Symbols | Verification Command | Confidence | Raw Trace |",
      "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    );

    const rawDir = path.join(workspaceRoot, ".tmem", "raw");
    for (const pb of activePlaybooks) {
      const ev = evaluateGuards(pb, workspaceRoot);
      let tierBadge = "🟢 `L0_FULL`";
      if (pb.is_trivial) tierBadge = "⚪ `TRIVIAL`";
      else if (ev.effective_tier === "L1_TOPOLOGY") tierBadge = "🟡 `L1_TOPOLOGY`";
      else if (ev.effective_tier === "L2_INVARIANT") tierBadge = "🔵 `L2_INVARIANT`";

      const intentTitle = sanitizeTableCell(pb.problem_intent, {
        maxChars: 120,
        asCode: false,
      });
      let intent = `**${intentTitle}**`;
      if (pb.semantic_summary) {
        const cleanSum = sanitizeTableCell(pb.semantic_summary, {
          maxChars: 160,
          asCode: false,
        });
        if (cleanSum && cleanSum !== "-") intent += `<br>_${cleanSum}_`;
      }
      if (pb.root_cause) {
        const cleanRc = sanitizeTableCell(pb.root_cause, {
          maxChars: 120,
          asCode: false,
        });
        if (cleanRc && cleanRc !== "-") intent += `<br>🎯 <i>${cleanRc}</i>`;
      }

      let filesStr =
        (pb.touched_files || [])
          .slice(0, 3)
          .map((f) => `\`${f}\``)
          .join("<br>") || "`-`";
      if ((pb.touched_files || []).length > 3) {
        filesStr += `<br>_+${pb.touched_files.length - 3} more_`;
      }

      const rawCmd =
        pb.l1_verification_cmds && pb.l1_verification_cmds.length > 0
          ? pb.l1_verification_cmds[pb.l1_verification_cmds.length - 1]
          : "";
      const verifyCmd = sanitizeTableCell(rawCmd, { maxChars: 120, asCode: true });

      const rawFile = path.join(rawDir, `${pb.trace_id}.raw.json`);
      const rawLink =
        existsSync(rawFile) || pb.raw_trace_path
          ? `[\`raw\`](.tmem/raw/${pb.trace_id}.raw.json)`
          : "`-`";

      const confPct = `${Math.trunc(Number(pb.confidence) * 100)}%`;
      const sessIds = pb.source_session_ids || [];
      const sessCell = sessIds.length
        ? `<br><sub>session: ${sessIds.slice(-2).map((s) => `\`${s}\``).join(", ")}</sub>`
        : "";

      lines.push(
        `| [\`${pb.trace_id}\`](.tmem/traces/${pb.trace_id}.json)${sessCell} | ${tierBadge} | ${intent} | ${filesStr} | ${verifyCmd} | ${confPct} | ${rawLink} |`
      );
    }
    lines.push("");
  }

  // Section 2: Universal Causal Repairs (L2)
  const allRepairs = [];
  const seenRepairs = new Set();
  for (const pb of activePlaybooks) {
    for (const r of pb.l2_causal_repairs || []) {
      const sigKey = `${r.error_signature}|${r.repair_action}`;
      if (!seenRepairs.has(sigKey)) {
        seenRepairs.add(sigKey);
        allRepairs.push(r);
      }
    }
  }

  lines.push("---", "", "## 2. Universal Causal Invariant Repairs (L2)", "");
  if (allRepairs.length === 0) {
    lines.push("_No error-to-fix invariant pairs extracted yet._", "");
  } else {
    lines.push(
      "| Error Signature | Failed Command | Minimal Repair Action | Verification Command |",
      "| :--- | :--- | :--- | :--- |"
    );
    for (const r of allRepairs) {
      const err = sanitizeTableCell(r.error_signature, { maxChars: 120, asCode: true });
      const failed = sanitizeTableCell(r.failed_command, { maxChars: 120, asCode: true });
      const repair = sanitizeTableCell(r.repair_action, { maxChars: 120, asCode: true });
      const verify = sanitizeTableCell(r.verification_cmd, { maxChars: 120, asCode: true });
      lines.push(`| ${err} | ${failed} | ${repair} | ${verify} |`);
    }
    lines.push("");
  }

  // Section 3: Co-Change Architectural Topologies
  const statPairs = computeCochangeStats(workspaceRoot, activePlaybooks);
  const allTopologies = statPairs.map((p) => p.formatLabel());
  if (allTopologies.length === 0) {
    const seenTopos = new Set();
    for (const pb of activePlaybooks) {
      for (const topo of pb.l1_cochange_topology || []) {
        if (!seenTopos.has(topo)) {
          seenTopos.add(topo);
          allTopologies.push(topo);
        }
      }
    }
  }

  lines.push("---", "", "## 3. Co-Change Architectural Topologies", "");
  if (allTopologies.length === 0) {
    lines.push("_No multi-file co-change patterns detected yet._", "");
  } else {
    for (const topo of allTopologies) {
      lines.push(`- 🔗 ${topo}`);
    }
    lines.push("");
  }

  lines.push(
    "---",
    "",
    "## 🛠️ Memory Commands & Forensic Inspection",
    "",
    "| Command | Description |",
    "| :--- | :--- |",
    "| `tmem inspect <trace_id> --level [index\\|playbook\\|raw\\|diff]` | Progressive disclosure: inspect at index, playbook, raw steps, or diff level |",
    "| `tmem replay <trace_id>` | Step-by-step forensic terminal playback of critical path vs pruned noise |",
    "| `tmem diff <trace_id>` | View unified git diff patch recorded during the session |",
    "| `tmem sync` | Re-index SQLite cache, validate L0 guards, and regenerate `MEMORIES.md` |",
    "| `tmem status` | Show project-level memory statistics |",
    "| `tmem gc` | Run metabolic garbage collection and prune stale paths |",
    "| `/tmem [intent]` | Capture current session execution path into project memory (`.tmem/traces/`) |",
    ""
  );

  return lines.join("\n");
}

export function syncMemoriesFile(workspaceRoot, playbooks) {
  const ws = path.resolve(workspaceRoot);
  const targetFile = path.join(ws, "MEMORIES.md");
  const tempFile = path.join(ws, ".MEMORIES.md.tmp");

  const content = renderMemoriesMarkdown(playbooks, ws);
  writeFileSync(tempFile, content, "utf-8");
  renameSync(tempFile, targetFile);

  const memAlias = path.join(ws, "MEM.md");
  try {
    try {
      const lst = lstatSync(memAlias);
      if (lst) unlinkSync(memAlias);
    } catch (_noLink) {
      // ignore if doesn't exist
    }
    symlinkSync("MEMORIES.md", memAlias);
  } catch (_err) {
    try {
      writeFileSync(memAlias, content, "utf-8");
    } catch (_err2) {
      // ignore
    }
  }

  return targetFile;
}
