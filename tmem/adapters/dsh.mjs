/**
 * Native DeepSeek Harness (@deepseek-ai/dsh) adapter for session.v4.jsonl transcripts and Cordis hooks.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import * as zlib from "node:zlib";
import { ActionKind, ActionNode } from "../models.mjs";
import {
  AgentAdapter,
  ParsedSession,
  cleanRationale,
  extractAssistantConclusion,
  extractErrorSignature,
  inferModifiedSymbols,
  isFillerUserIntent,
  normalizeRelPath,
  scrubSecrets,
} from "./base.mjs";

export const DSH_EXIT_CODE_RE = /\[exit code:\s*(-?\d+)\]/i;
const APPLY_PATCH_FILE_RE =
  /^\*\*\*\s+(?!(?:Begin|End)\s+Patch\b|\d+(?:,\d+)?\s+\*{4}\s*$)(?:(?:Update|Add|Delete|Move to)\s+File:\s+)?([^\r\n]+)/;
const UNIFIED_DIFF_GIT_RE = /^diff --git\s+a\/(.+?)\s+b\/(.+)$/;
const UNIFIED_HUNK_RE = /^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/;

function extractDshText(content) {
  if (content === null || content === undefined) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = [];
    for (const item of content) {
      if (typeof item === "string") {
        parts.push(item);
      } else if (item && typeof item === "object") {
        if (item.type === "text" && typeof item.text === "string") {
          parts.push(item.text);
        } else if (typeof item.content === "string") {
          parts.push(item.content);
        }
      }
    }
    return parts.join("\n");
  }
  if (typeof content === "object") {
    if (typeof content.text === "string") return content.text;
    if ("content" in content) return extractDshText(content.content);
  }
  return String(content);
}

function msToIso(tsVal) {
  if (typeof tsVal === "number" && Number.isFinite(tsVal) && tsVal > 0) {
    try {
      return new Date(tsVal).toISOString();
    } catch (_err) {
      return "";
    }
  }
  if (typeof tsVal === "string") return tsVal;
  return "";
}

function stripDiffPathPrefix(rawPath, expectedPrefix, stripPrefix) {
  let cleaned = rawPath.split("\t")[0].trim().replace(/^["']+|["']+$/g, "");
  if (stripPrefix && cleaned.startsWith(expectedPrefix)) {
    cleaned = cleaned.slice(expectedPrefix.length);
  }
  return cleaned;
}

export function parseApplyPatchDetails(patchText) {
  if (!patchText) return [];

  const isCodexPatch =
    patchText.includes("*** Begin Patch") ||
    patchText.split(/\r?\n/).some((ln) => APPLY_PATCH_FILE_RE.test(ln.trim()));

  const results = [];
  let currentFile = null;
  let addedLines = [];
  let lineRanges = [];
  let hunkHeaders = [];
  let pendingOldFile = null;
  let pendingHasAPrefix = false;
  let sawGitDiffHeader = false;

  const flush = () => {
    if (currentFile) {
      results.push([
        currentFile,
        addedLines.join("\n"),
        [...lineRanges],
        hunkHeaders.join("\n"),
      ]);
    }
  };

  for (const rawLine of patchText.split(/\r?\n/)) {
    const stripped = rawLine.trim();
    const mCodex = stripped.match(APPLY_PATCH_FILE_RE);
    if (mCodex) {
      const candidate = mCodex[1].split("\t")[0].trim();
      if (!candidate || candidate === "/dev/null" || candidate.endsWith("****")) {
        continue;
      }
      flush();
      currentFile = candidate;
      addedLines = [];
      lineRanges = [];
      hunkHeaders = [];
      continue;
    }

    if (!isCodexPatch) {
      const mGit = rawLine.match(UNIFIED_DIFF_GIT_RE);
      if (mGit) {
        flush();
        currentFile = mGit[2].trim().replace(/^"+|"+$/g, "");
        addedLines = [];
        lineRanges = [];
        hunkHeaders = [];
        pendingOldFile = mGit[1].trim().replace(/^"+|"+$/g, "");
        pendingHasAPrefix = true;
        sawGitDiffHeader = true;
        continue;
      }

      if (rawLine.startsWith("--- ") && !stripped.endsWith("----")) {
        const oldRaw = rawLine.slice(4).split("\t")[0].trim().replace(/^"+|"+$/g, "");
        pendingHasAPrefix = oldRaw.startsWith("a/");
        pendingOldFile =
          oldRaw === "/dev/null"
            ? null
            : stripDiffPathPrefix(oldRaw, "a/", pendingHasAPrefix);
        continue;
      }

      if (rawLine.startsWith("+++ ") && !stripped.endsWith("++++")) {
        const newRaw = rawLine.slice(4).split("\t")[0].trim().replace(/^"+|"+$/g, "");
        const stripB =
          sawGitDiffHeader ||
          pendingHasAPrefix ||
          (pendingOldFile === null && newRaw.startsWith("b/"));
        const targetPath =
          newRaw === "/dev/null"
            ? pendingOldFile
            : stripDiffPathPrefix(newRaw, "b/", stripB);
        if (targetPath) {
          if (sawGitDiffHeader && currentFile) {
            currentFile = targetPath;
          } else {
            flush();
            currentFile = targetPath;
            addedLines = [];
            lineRanges = [];
            hunkHeaders = [];
          }
        }
        sawGitDiffHeader = false;
        pendingOldFile = null;
        pendingHasAPrefix = false;
        continue;
      }
    }

    if (currentFile !== null) {
      const mHunk = rawLine.match(UNIFIED_HUNK_RE);
      if (mHunk) {
        const startLn = Math.max(1, parseInt(mHunk[1], 10));
        const count = mHunk[2] !== undefined ? parseInt(mHunk[2], 10) : 1;
        const endLn = count === 0 ? startLn : startLn + count - 1;
        lineRanges.push([startLn, endLn]);
        const ctx = (mHunk[3] || "").trim();
        if (ctx) hunkHeaders.push(ctx);
        continue;
      }
      if (rawLine.startsWith("+") && !rawLine.startsWith("+++")) {
        addedLines.push(rawLine.slice(1));
      }
    }
  }

  flush();
  return results;
}

export function parseApplyPatchTargets(patchText) {
  return parseApplyPatchDetails(patchText).map(([filePath, addedBody]) => [
    filePath,
    addedBody,
  ]);
}

function isDshTranscriptFile(name) {
  return name.endsWith(".jsonl") || name.endsWith(".jsonl.zstd");
}

const ZSTD_MAGIC = 0xfd2fb528;
const ZSTD_SKIPPABLE_MASK = 0xfffffff0;
const ZSTD_SKIPPABLE_MAGIC = 0x184d2a50;

/**
 * Find the byte end offset of a single Zstandard frame starting at `offset`
 * (RFC 8878), or -1 if the frame header/blocks are truncated or invalid.
 */
function findZstdFrameEnd(buf, offset) {
  if (offset + 4 > buf.length) return -1;
  const magic = buf.readUInt32LE(offset);
  if ((magic & ZSTD_SKIPPABLE_MASK) === ZSTD_SKIPPABLE_MAGIC) {
    if (offset + 8 > buf.length) return -1;
    const skippableSize = buf.readUInt32LE(offset + 4);
    const end = offset + 8 + skippableSize;
    return end <= buf.length ? end : -1;
  }
  if (magic !== ZSTD_MAGIC) return -1;

  let pos = offset + 4;
  if (pos >= buf.length) return -1;
  const fhd = buf[pos++];
  const fcsFlag = (fhd >> 6) & 0x03;
  const singleSegment = (fhd >> 5) & 0x01;
  const contentChecksum = (fhd >> 2) & 0x01;
  const dictIdFlag = fhd & 0x03;

  const windowDescBytes = singleSegment ? 0 : 1;
  const dictIdBytes = dictIdFlag === 0 ? 0 : dictIdFlag === 1 ? 1 : dictIdFlag === 2 ? 2 : 4;
  const fcsBytes =
    fcsFlag === 0
      ? singleSegment
        ? 1
        : 0
      : fcsFlag === 1
        ? 2
        : fcsFlag === 2
          ? 4
          : 8;

  pos += windowDescBytes + dictIdBytes + fcsBytes;
  if (pos > buf.length) return -1;

  let lastBlock = 0;
  while (lastBlock === 0) {
    if (pos + 3 > buf.length) return -1;
    const header = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16);
    pos += 3;
    lastBlock = header & 0x01;
    const blockType = (header >> 1) & 0x03;
    const blockSize = header >>> 3;
    if (blockType === 3) return -1;
    const payloadBytes = blockType === 1 ? 1 : blockSize;
    pos += payloadBytes;
    if (pos > buf.length) return -1;
  }

  if (contentChecksum) {
    pos += 4;
    if (pos > buf.length) return -1;
  }
  return pos;
}

/**
 * Decompress a single-frame or concatenated multi-frame Zstandard buffer.
 */
export function decompressZstdMultiFrame(buf) {
  if (!buf || buf.length === 0) return Buffer.alloc(0);
  if (typeof zlib.zstdDecompressSync !== "function") return null;

  const chunks = [];
  let offset = 0;
  while (offset < buf.length) {
    const magic = offset + 4 <= buf.length ? buf.readUInt32LE(offset) : 0;
    const frameEnd = findZstdFrameEnd(buf, offset);
    if (frameEnd < 0) {
      if (chunks.length > 0) break;
      return null;
    }
    if ((magic & ZSTD_SKIPPABLE_MASK) !== ZSTD_SKIPPABLE_MAGIC) {
      chunks.push(zlib.zstdDecompressSync(buf.subarray(offset, frameEnd)));
    }
    offset = frameEnd;
  }
  return Buffer.concat(chunks);
}

function readTranscriptText(transcriptPath) {
  if (transcriptPath.endsWith(".zstd")) {
    const rawBuf = readFileSync(transcriptPath);
    const decoded = decompressZstdMultiFrame(rawBuf);
    if (decoded !== null) {
      return decoded.toString("utf-8");
    }
    return execFileSync("zstd", ["-dc", transcriptPath], { encoding: "utf-8" });
  }
  return readFileSync(transcriptPath, "utf-8");
}

function collectJsonlFiles(dir, out = [], seen = new Set()) {
  if (!existsSync(dir)) return out;
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch (_err) {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      collectJsonlFiles(full, out, seen);
    } else if (ent.isFile() && isDshTranscriptFile(ent.name)) {
      const resolved = path.resolve(full);
      if (!seen.has(resolved)) {
        seen.add(resolved);
        out.push(full);
      }
    }
  }
  return out;
}

export class DshAdapter extends AgentAdapter {
  constructor() {
    super();
    this.name = "dsh";
  }

  parseTranscript(transcriptPath, workspaceRoot, minStepIdx = 0) {
    return this.parseTranscriptSpan(transcriptPath, workspaceRoot, { minStepIdx }).session;
  }

  parseTranscriptSpan(
    transcriptPath,
    workspaceRoot,
    { minStepIdx = 0, maxStepIdx = null, seqSet = null } = {}
  ) {
    const parsedPath = path.parse(transcriptPath);
    const parentName = path.basename(parsedPath.dir);
    const fallbackSessionId =
      parsedPath.base.startsWith("session.") && parentName !== "sessions"
        ? parentName
        : parsedPath.name;

    if (!existsSync(transcriptPath)) {
      return {
        session: new ParsedSession({
          agent_name: "dsh",
          session_id: fallbackSessionId,
          workspace_root: workspaceRoot,
        }),
        turns: [],
        diff_snippets: [],
        decisions: [],
        seq_from: 0,
        seq_to: 0,
        shadowed_seqs: [],
      };
    }

    const events = [];
    const rawContent = readTranscriptText(transcriptPath);
    for (const rawLine of rawContent.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      try {
        events.push(JSON.parse(line));
      } catch (_err) {
        // skip malformed line
      }
    }

    return parseDshEventsWithAdapter(this, events, {
      sessionId: fallbackSessionId,
      workspaceRoot,
      minStepIdx,
      maxStepIdx,
      seqSet,
    });
  }

  static _parseArgs(rawArgs) {
    if (rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
      return rawArgs;
    }
    if (typeof rawArgs === "string") {
      const s = rawArgs.trim();
      if (!s) return {};
      try {
        const parsed = JSON.parse(s);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          return parsed;
        }
      } catch (_err) {
        return { command: s };
      }
    }
    return {};
  }

  static _extractExitCode(outText, meta, isError, kind) {
    if (typeof meta.exitCode === "number" && Number.isInteger(meta.exitCode)) {
      return meta.exitCode;
    }
    if (typeof meta.exit_code === "number" && Number.isInteger(meta.exit_code)) {
      return meta.exit_code;
    }
    const m = (outText || "").match(DSH_EXIT_CODE_RE);
    if (m) return parseInt(m[1], 10);
    if (isError) return 1;
    if (kind === ActionKind.ENV_EXEC) return 0;
    return null;
  }

  _mapDshToolCall(stepIdx, toolName, args, workspaceRoot, timestamp, rationale = null) {
    const low = String(toolName).toLowerCase();
    const cleanedArgs = {};
    for (const [k, v] of Object.entries(args || {})) {
      cleanedArgs[k] = typeof v === "string" ? scrubSecrets(v) : v;
    }

    if (low === "apply_patch") {
      const patchInput = String(args.input || args.patch || args.diff || "");
      const details = parseApplyPatchDetails(patchInput);
      if (details.length > 0) {
        return details.map(([rawPath, addedBody, lineRanges, hunkHeaders]) => {
          const relPath = normalizeRelPath(rawPath, workspaceRoot);
          return new ActionNode({
            step_idx: stepIdx,
            kind: ActionKind.MUTATE,
            tool_name: toolName,
            target_file: relPath,
            symbols_touched: inferModifiedSymbols(workspaceRoot, relPath, {
              newSnippet: addedBody,
              lineRanges,
              hunkHeaders,
            }),
            timestamp,
            raw_input: cleanedArgs,
            rationale,
          });
        });
      }
    }

    const fileArg =
      args.file_path ?? args.path ?? args.filePath ?? args.Path ?? args.TargetFile;
    let cmdArg = args.command ?? args.cmd ?? args.CommandLine;
    if (Array.isArray(cmdArg)) {
      cmdArg = cmdArg.map(String).join(" ");
    }

    if (["read", "read_image", "glob", "grep", "read_file", "view_file"].includes(low)) {
      return [
        new ActionNode({
          step_idx: stepIdx,
          kind: ActionKind.READ,
          tool_name: toolName,
          target_file: normalizeRelPath(fileArg, workspaceRoot),
          timestamp,
          raw_input: cleanedArgs,
          rationale,
        }),
      ];
    }

    if (
      ["write", "edit", "str_replace_editor", "write_file", "edit_file", "apply_patch"].includes(
        low
      )
    ) {
      const relPath = normalizeRelPath(fileArg, workspaceRoot);
      const codeBody = String(
        args.content ??
          args.new_string ??
          args.new_str ??
          args.file_text ??
          args.replacement ??
          args.old_string ??
          args.old_str ??
          ""
      );
      return [
        new ActionNode({
          step_idx: stepIdx,
          kind: ActionKind.MUTATE,
          tool_name: toolName,
          target_file: relPath,
          symbols_touched: inferModifiedSymbols(workspaceRoot, relPath, {
            newSnippet: codeBody,
          }),
          timestamp,
          raw_input: cleanedArgs,
          rationale,
        }),
      ];
    }

    if (["bash", "pwsh", "run_command", "shell", "exec"].includes(low) || cmdArg) {
      return [
        new ActionNode({
          step_idx: stepIdx,
          kind: ActionKind.ENV_EXEC,
          tool_name: toolName,
          command: scrubSecrets(String(cmdArg || "")),
          timestamp,
          raw_input: cleanedArgs,
          rationale,
        }),
      ];
    }

    return [
      new ActionNode({
        step_idx: stepIdx,
        kind: ActionKind.OTHER,
        tool_name: toolName,
        timestamp,
        raw_input: cleanedArgs,
        rationale,
      }),
    ];
  }

  discoverLatestTranscript(workspaceRoot, sessionId = null) {
    const home = os.homedir();
    const dshHome = process.env.DSH_HOME || path.join(home, ".dsh");
    const ws = path.resolve(workspaceRoot);
    const searchRoots = [
      path.join(dshHome, "sessions"),
      path.join(ws, ".dsh", "sessions"),
      path.join(home, ".dsh", "sessions"),
      path.join(home, ".local", "share", "dsh"),
    ];

    if (sessionId) {
      for (const root of searchRoots) {
        const sessDir = path.join(root, sessionId);
        try {
          if (existsSync(sessDir) && statSync(sessDir).isDirectory()) {
            for (const fname of [
              "session.v4.jsonl.zstd",
              "session.v4.jsonl",
              "session.v3.jsonl.zstd",
              "session.v3.jsonl",
              "session.jsonl.zstd",
              "session.jsonl",
            ]) {
              const cand = path.join(sessDir, fname);
              if (existsSync(cand)) return cand;
            }
          }
        } catch (_err) {
          // ignore
        }
      }
    }

    const candidates = [];
    const seen = new Set();
    for (const root of searchRoots) {
      collectJsonlFiles(root, candidates, seen);
    }
    if (candidates.length === 0) return null;

    // DSH encodes the workspace path into the session directory name, e.g.
    // /home/kent/e2e/ledger-svc -> "--home-kent-e2e-ledger-svc--". Prefer the
    // transcripts of THIS workspace: picking the globally newest transcript can
    // hand another workspace's session to this workspace's capture.
    const encodedWs = `--${ws.replace(/^\/+|\/+$/g, "").replace(/[\\/]/g, "-")}--`;
    const ownWorkspace = [];
    const foreign = [];
    for (const cand of candidates) {
      (cand.includes(encodedWs) ? ownWorkspace : foreign).push(cand);
    }
    const pool = ownWorkspace.length > 0 ? ownWorkspace : candidates;

    pool.sort((a, b) => {
      const sa = statSync(a);
      const sb = statSync(b);
      if (sb.mtimeMs !== sa.mtimeMs) return sb.mtimeMs - sa.mtimeMs;
      const prio = (p) => {
        const base = path.basename(p);
        if (base.startsWith("session.v4.jsonl")) return base.endsWith(".zstd") ? 5 : 4;
        if (base.startsWith("session.v3.jsonl")) return base.endsWith(".zstd") ? 3 : 2;
        if (base.startsWith("session.jsonl")) return base.endsWith(".zstd") ? 1 : 0;
        return 0;
      };
      return prio(b) - prio(a);
    });
    return pool[0];
  }
}

const DECISION_SIGNAL_RE =
  /(?:不要|必须|改为|统一|保留|禁止|约定|决定|采用|方案|约束|限制|注意|should not|must|never|always|prefer|instead of|decided|constraint|keep|switch to|use\s+\S+)/i;

function extractDecisionsFromText(text, maxItems = 4) {
  if (!text) return [];
  const cleaned = scrubSecrets(String(text))
    .replace(/```[\s\S]*?```/g, "")
    .replace(/<tmem_[\s\S]*?<\/tmem_[^>]+>/g, "");
  const decisions = [];
  const seen = new Set();

  for (const rawLine of cleaned.split(/\r?\n/)) {
    const line = rawLine.trim().replace(/^(?:[-*+]|\d+\.)\s+/, "").trim();
    if (line.length < 10 || line.length > 260) continue;
    if (line.startsWith("> 💡") || line.startsWith("#")) continue;
    if (DECISION_SIGNAL_RE.test(line)) {
      const key = line.toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        decisions.push(line);
        if (decisions.length >= maxItems) break;
      }
    }
  }
  return decisions;
}

function buildCompactDiffSnippet(toolName, args, workspaceRoot) {
  const low = String(toolName || "").toLowerCase();
  if (low === "apply_patch") {
    const patchInput = scrubSecrets(String(args.input || args.patch || args.diff || ""));
    const details = parseApplyPatchDetails(patchInput);
    return details.map(([rawPath]) => ({
      file: normalizeRelPath(rawPath, workspaceRoot) || rawPath,
      tool_name: toolName,
      snippet: patchInput.slice(0, 800),
    }));
  }

  const fileArg =
    args.file_path ?? args.path ?? args.filePath ?? args.Path ?? args.TargetFile;
  const relPath = normalizeRelPath(fileArg, workspaceRoot);
  if (!relPath) return [];

  if (["edit", "edit_file", "str_replace_editor"].includes(low)) {
    const oldStr = scrubSecrets(String(args.old_string ?? args.old_str ?? "")).slice(0, 300);
    const newStr = scrubSecrets(
      String(args.new_string ?? args.new_str ?? args.replacement ?? "")
    ).slice(0, 300);
    if (!oldStr && !newStr) return [];
    const snippet = (oldStr ? `- ${oldStr.replace(/\n/g, "\n- ")}\n` : "") +
      (newStr ? `+ ${newStr.replace(/\n/g, "\n+ ")}` : "");
    return [{ file: relPath, tool_name: toolName, snippet: snippet.slice(0, 700) }];
  }

  if (["write", "write_file"].includes(low)) {
    const body = scrubSecrets(String(args.content ?? args.file_text ?? "")).slice(0, 500);
    if (!body) return [];
    return [{ file: relPath, tool_name: toolName, snippet: `+ ${body}` }];
  }

  return [];
}

function parseDshEventsWithAdapter(
  adapter,
  events,
  { sessionId = "default", workspaceRoot, minStepIdx = 0, maxStepIdx = null, seqSet = null } = {}
) {
  const session = new ParsedSession({
    agent_name: "dsh",
    session_id: sessionId,
    workspace_root: workspaceRoot,
  });

  const callIdToNodes = new Map();
  const seqToNodes = new Map();
  const pendingNodes = [];
  const stepTurnMap = new Map();
  const diffSnippets = [];
  const decisions = [];
  const seenDecisions = new Set();

  const addDecision = (d) => {
    const clean = scrubSecrets(String(d || "")).trim();
    if (!clean) return;
    const low = clean.toLowerCase();
    if (!seenDecisions.has(low)) {
      seenDecisions.add(low);
      decisions.push(clean.slice(0, 260));
    }
  };

  const turns = [];
  let currentTurn = null;
  let autoTurnCounter = 0;

  const ensureTurn = (seqNum, explicitTurnNum = null) => {
    if (explicitTurnNum !== null) {
      if (!currentTurn || currentTurn.turn_idx !== explicitTurnNum) {
        currentTurn = {
          turn_idx: explicitTurnNum,
          seq_from: seqNum,
          seq_to: seqNum,
          user_prompt: "",
          assistant_summary: "",
          decisions: [],
          files: [],
          symbols: [],
          errors: [],
          commands: [],
        };
        turns.push(currentTurn);
      }
      return currentTurn;
    }
    if (!currentTurn) {
      autoTurnCounter += 1;
      currentTurn = {
        turn_idx: autoTurnCounter,
        seq_from: seqNum,
        seq_to: seqNum,
        user_prompt: "",
        assistant_summary: "",
        decisions: [],
        files: [],
        symbols: [],
        errors: [],
        commands: [],
      };
      turns.push(currentTurn);
    }
    return currentTurn;
  };

  const pushUnique = (arr, val) => {
    if (val && !arr.includes(val)) arr.push(val);
  };

  let lineStep = 0;
  let lastAsstText = "";
  let minSeenSeq = Infinity;
  let maxSeenSeq = -Infinity;

  for (const entry of events || []) {
    if (!entry || typeof entry !== "object") continue;

    if ("version" in entry && "id" in entry && !("type" in entry)) {
      if (typeof entry.id === "string" && entry.id) {
        session.session_id = entry.id;
      }
      continue;
    }

    lineStep += 1;
    const eventType = entry.type || entry.role || "";
    const seqVal = entry.seq;
    const stepIdx = typeof seqVal === "number" && Number.isInteger(seqVal) ? seqVal : lineStep;

    if (stepIdx < minStepIdx) continue;
    if (maxStepIdx !== null && stepIdx > maxStepIdx) continue;
    if (seqSet && !["turn/start", "turn/end", "tool/call"].includes(eventType) && !seqSet.has(stepIdx)) {
      continue;
    }

    minSeenSeq = Math.min(minSeenSeq, stepIdx);
    maxSeenSeq = Math.max(maxSeenSeq, stepIdx);

    const timestamp = msToIso(entry.time || entry.created_at || "");
    const data = entry.data && typeof entry.data === "object" ? entry.data : entry;

    if (eventType === "turn/start") {
      const turnNum = typeof data.turn === "number" ? data.turn : autoTurnCounter + 1;
      autoTurnCounter = Math.max(autoTurnCounter, turnNum);
      ensureTurn(stepIdx, turnNum);
      continue;
    }

    if (eventType === "turn/end") {
      if (currentTurn) {
        currentTurn.seq_to = Math.max(currentTurn.seq_to, stepIdx);
      }
      continue;
    }

    if (["user/message", "user", "human", "USER_INPUT"].includes(eventType)) {
      const msgObj = data.message && typeof data.message === "object" ? data.message : data;
      const source = msgObj && typeof msgObj === "object" ? msgObj.source : null;
      const sourceKind = source && typeof source === "object" ? source.kind : "user";
      if (!["user", null, undefined, ""].includes(sourceKind)) continue;

      const rawText =
        msgObj && typeof msgObj === "object" && "content" in msgObj
          ? msgObj.content
          : entry.content || "";
      const text = scrubSecrets(extractDshText(rawText)).trim();
      if (
        text &&
        !text.startsWith("/") &&
        !text.startsWith("<tmem_") &&
        text !== "[REDACTED_SECRET]" &&
        !isFillerUserIntent(text)
      ) {
        session.user_intents.push(text.slice(0, 500));
        if (currentTurn && currentTurn.user_prompt) {
          // Open a new logical turn when no explicit turn/start marker existed
          autoTurnCounter += 1;
          currentTurn = null;
        }
        const turn = ensureTurn(stepIdx, null);
        turn.seq_to = Math.max(turn.seq_to, stepIdx);
        turn.user_prompt = text.slice(0, 400);
        for (const d of extractDecisionsFromText(text, 3)) {
          addDecision(d);
          pushUnique(turn.decisions, d);
        }
      }
      continue;
    }

    if (eventType === "assistant/message" || eventType === "assistant") {
      const msgObj = data.message && typeof data.message === "object" ? data.message : data;
      const asstText = extractDshText(
        msgObj && typeof msgObj === "object" && "content" in msgObj
          ? msgObj.content
          : entry.content || ""
      );
      lastAsstText = asstText;
      const conclusion = extractAssistantConclusion(asstText);
      const turn = ensureTurn(stepIdx, typeof data.turn === "number" ? data.turn : null);
      turn.seq_to = Math.max(turn.seq_to, stepIdx);
      if (conclusion) {
        session.assistant_conclusions.push(conclusion);
        turn.assistant_summary = conclusion;
      }
      for (const d of extractDecisionsFromText(asstText, 3)) {
        addDecision(d);
        pushUnique(turn.decisions, d);
      }
      if (eventType === "assistant/message") continue;
    }

    if (eventType === "tool/call") {
      const callId = String(data.callId || data.id || `seq_${stepIdx}`);
      const toolName = String(data.name || "tool");
      const rawArgs = data.arguments ?? data.args ?? {};
      const args = DshAdapter._parseArgs(rawArgs);
      const toolSum = args.toolSummary || args.description || lastAsstText || "";
      const stepRationale = cleanRationale(toolSum);
      const turn = ensureTurn(stepIdx, typeof data.turn === "number" ? data.turn : null);
      turn.seq_to = Math.max(turn.seq_to, stepIdx);

      const nodes = adapter._mapDshToolCall(
        stepIdx,
        toolName,
        args,
        workspaceRoot,
        timestamp,
        stepRationale
      );
      for (const n of nodes) {
        stepTurnMap.set(n.step_idx, turn.turn_idx);
        if (n.target_file) pushUnique(turn.files, n.target_file);
        for (const sym of n.symbols_touched || []) pushUnique(turn.symbols, sym);
        if (n.command) pushUnique(turn.commands, n.command);
      }
      for (const ds of buildCompactDiffSnippet(toolName, args, workspaceRoot)) {
        diffSnippets.push({
          turn_idx: turn.turn_idx,
          seq: stepIdx,
          ...ds,
        });
      }
      session.actions.push(...nodes);
      callIdToNodes.set(callId, nodes);
      if (typeof seqVal === "number" && Number.isInteger(seqVal)) {
        seqToNodes.set(seqVal, nodes);
      }
      pendingNodes.push(...nodes);
      continue;
    }

    if (eventType === "tool/result") {
      const msgObj = data.message && typeof data.message === "object" ? data.message : {};
      const callId = String(msgObj.callId || data.callId || "");
      let targetNodes = callIdToNodes.get(callId);
      if (!targetNodes) {
        const srcSeqs = entry.sourceEventSeqs || [];
        for (const s of srcSeqs) {
          if (typeof s === "number" && seqToNodes.has(s)) {
            targetNodes = seqToNodes.get(s);
            break;
          }
        }
      }
      if (!targetNodes && pendingNodes.length > 0) {
        targetNodes = [pendingNodes.shift()];
      }
      if (!targetNodes || targetNodes.length === 0) {
        const orphanNode = new ActionNode({
          step_idx: stepIdx,
          kind: ActionKind.ENV_EXEC,
          tool_name: String(msgObj.name || data.toolName || "tool"),
          timestamp,
        });
        session.actions.push(orphanNode);
        const turn = ensureTurn(stepIdx, typeof data.turn === "number" ? data.turn : null);
        stepTurnMap.set(orphanNode.step_idx, turn.turn_idx);
        targetNodes = [orphanNode];
      }

      if (targetNodes && targetNodes.length > 0) {
        const outText = extractDshText(msgObj.content || data.content || "");
        const isError = Boolean(msgObj.isError || data.isError || data.error);
        const meta = data.meta && typeof data.meta === "object" ? data.meta : {};
        const exitCode = DshAdapter._extractExitCode(
          outText,
          meta,
          isError,
          targetNodes[0].kind
        );
        let errSig = null;
        if (
          isError ||
          (exitCode !== null && exitCode !== 0) ||
          outText.includes("Error") ||
          outText.includes("Exception") ||
          outText.includes("FAIL")
        ) {
          const deepScan = (isError || (exitCode !== null && exitCode !== 0)) && outText.length > 8000;
          errSig = extractErrorSignature(outText, { scanLines: deepScan ? 1200 : 40 });
          if (!errSig && isError && data.error && typeof data.error === "object") {
            errSig = extractErrorSignature(String(data.error.message || ""));
          }
        }

        const turn = ensureTurn(stepIdx, typeof data.turn === "number" ? data.turn : null);
        turn.seq_to = Math.max(turn.seq_to, stepIdx);

        for (const tNode of targetNodes) {
          if (exitCode !== null) tNode.exit_code = exitCode;
          if (
            errSig &&
            (isError ||
              (tNode.exit_code !== null && tNode.exit_code !== 0) ||
              tNode.kind === ActionKind.ENV_EXEC)
          ) {
            tNode.error_signature = errSig;
            session.active_errors.push(errSig);
            pushUnique(turn.errors, errSig);
          }
          tNode.output_snippet = scrubSecrets(outText.slice(0, 400));
          tNode.raw_output = scrubSecrets(outText);
        }
      }
      continue;
    }

    const toolCalls =
      entry.tool_calls ||
      entry.toolCalls ||
      (["function_call", "tool_use", "command"].includes(eventType) ? [entry] : []);
    for (const tc of toolCalls) {
      if (!tc || typeof tc !== "object") continue;
      const fn = tc.function && typeof tc.function === "object" ? tc.function : {};
      const tName = String(tc.name || fn.name || tc.tool || tc.type || "tool");
      const args = DshAdapter._parseArgs(
        tc.arguments ?? fn.arguments ?? tc.args ?? tc.input ?? {}
      );
      const turn = ensureTurn(stepIdx, null);
      turn.seq_to = Math.max(turn.seq_to, stepIdx);
      const nodes = adapter._mapDshToolCall(stepIdx, tName, args, workspaceRoot, timestamp);
      for (const node of nodes) {
        stepTurnMap.set(node.step_idx, turn.turn_idx);
        if (node.target_file) pushUnique(turn.files, node.target_file);
        for (const sym of node.symbols_touched || []) pushUnique(turn.symbols, sym);
        if (node.command) pushUnique(turn.commands, node.command);

        if ("exit_code" in entry || "exitCode" in entry) {
          node.exit_code = Number(entry.exit_code ?? entry.exitCode ?? 0);
        }
        const outText = String(entry.output || entry.stdout || entry.stderr || "");
        if (outText) {
          const errSig = extractErrorSignature(outText);
          if (errSig && node.exit_code !== 0) {
            node.error_signature = errSig;
            session.active_errors.push(errSig);
            pushUnique(turn.errors, errSig);
          }
          node.output_snippet = scrubSecrets(outText.slice(0, 400));
          node.raw_output = scrubSecrets(outText);
        }
        session.actions.push(node);
        pendingNodes.push(node);
      }
      for (const ds of buildCompactDiffSnippet(tName, args, workspaceRoot)) {
        diffSnippets.push({
          turn_idx: turn.turn_idx,
          seq: stepIdx,
          ...ds,
        });
      }
    }
  }

  return {
    session,
    turns,
    diff_snippets: diffSnippets,
    decisions,
    step_turn_map: stepTurnMap,
    seq_from: Number.isFinite(minSeenSeq) ? minSeenSeq : 0,
    seq_to: Number.isFinite(maxSeenSeq) ? maxSeenSeq : 0,
  };
}

/**
 * Extract structured span data directly from a live DSH `Session` object (or cached event map)
 * for the given `seqList` (such as `compaction/summary.data.shadowedSeqs` or `compaction/prune`).
 */
export function parseDshLiveSessionSpan(
  session,
  seqList,
  workspaceRoot,
  { cachedEvents = null, exactSeqOnly = false } = {}
) {
  const adapter = new DshAdapter();
  const sessionId = String(session?.id || "default");
  const ws = path.resolve(workspaceRoot || session?.header?.cwd || process.cwd());

  const seqs = Array.isArray(seqList)
    ? seqList.map(Number).filter((n) => Number.isInteger(n) && n >= 0)
    : [];
  if (seqs.length === 0) {
    return parseDshEventsWithAdapter(adapter, [], { sessionId, workspaceRoot: ws });
  }

  const minS = Math.min(...seqs);
  const maxS = Math.max(...seqs);
  const seqSet = new Set(seqs);
  const totalSeq = typeof session?.seq === "number" ? session.seq : maxS + 1;

  const getEv = (seq) => {
    if (cachedEvents && cachedEvents.has(seq)) return cachedEvents.get(seq);
    if (typeof session?.eventAt === "function") {
      try {
        return session.eventAt(seq);
      } catch (_err) {
        return undefined;
      }
    }
    return undefined;
  };

  // First inspect surface events in seqSet to gather referenced sourceEventSeqs and callIds
  const extraCallSeqs = new Set();
  const neededCallIds = new Set();
  for (const s of seqs) {
    const ev = getEv(s);
    if (!ev) continue;
    if (Array.isArray(ev.sourceEventSeqs)) {
      for (const src of ev.sourceEventSeqs) {
        if (typeof src === "number" && Number.isInteger(src)) extraCallSeqs.add(src);
      }
    }
    if (ev.type === "tool/result") {
      const cid = String(ev.data?.message?.callId || ev.data?.callId || "");
      if (cid) neededCallIds.add(cid);
    }
  }

  if (neededCallIds.size > 0) {
    const lookbackStart = Math.max(0, minS - 16);
    for (let s = lookbackStart; s < maxS; s++) {
      const ev = getEv(s);
      if (ev && ev.type === "tool/call") {
        const cid = String(ev.data?.callId || ev.data?.id || "");
        if (cid && neededCallIds.has(cid)) {
          extraCallSeqs.add(s);
        }
      }
    }
  }

  const scanStart = exactSeqOnly
    ? Math.max(0, Math.min(minS, ...(extraCallSeqs.size ? extraCallSeqs : [minS])) - 2)
    : Math.max(0, minS - 2);
  const scanEnd = Math.min(totalSeq - 1, maxS);

  const collected = [];
  const neededSurfaceOrCalls = new Set([...seqSet, ...extraCallSeqs]);
  for (let s = scanStart; s <= scanEnd; s++) {
    const ev = getEv(s);
    if (!ev) continue;
    if (exactSeqOnly) {
      if (
        neededSurfaceOrCalls.has(s) ||
        ev.type === "turn/start" ||
        ev.type === "turn/end"
      ) {
        collected.push(ev);
      }
    } else {
      collected.push(ev);
    }
  }
  let effectiveMax = scanEnd;
  const trailingEnd = maxS + 1 < totalSeq ? getEv(maxS + 1) : undefined;
  if (trailingEnd && trailingEnd.type === "turn/end") {
    collected.push(trailingEnd);
    effectiveMax = maxS + 1;
  }

  // Fallback to disk JSONL transcript if live session object did not expose events
  if (collected.length === 0) {
    const tPath = adapter.discoverLatestTranscript(ws, sessionId);
    if (tPath && existsSync(tPath)) {
      return adapter.parseTranscriptSpan(tPath, ws, {
        minStepIdx: scanStart,
        maxStepIdx: effectiveMax,
        seqList: exactSeqOnly ? Array.from(neededSurfaceOrCalls) : seqs,
      });
    }
  }

  return parseDshEventsWithAdapter(adapter, collected, {
    sessionId,
    workspaceRoot: ws,
    minStepIdx: scanStart,
    maxStepIdx: effectiveMax,
    seqSet: exactSeqOnly ? neededSurfaceOrCalls : seqSet,
  });
}
