/**
 * Native DeepSeek Harness (@deepseek-ai/dsh) adapter for session.v4.jsonl transcripts and Cordis hooks.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
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
    } else if (ent.isFile() && ent.name.endsWith(".jsonl")) {
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
    const parsedPath = path.parse(transcriptPath);
    const parentName = path.basename(parsedPath.dir);
    const sessionId =
      parsedPath.base.startsWith("session.") && parentName !== "sessions"
        ? parentName
        : parsedPath.name;

    const session = new ParsedSession({
      agent_name: "dsh",
      session_id: sessionId,
      workspace_root: workspaceRoot,
    });

    if (!existsSync(transcriptPath)) return session;

    const callIdToNodes = new Map();
    const seqToNodes = new Map();
    const pendingNodes = [];
    let lineStep = 0;
    let lastAsstText = "";

    const rawContent = readFileSync(transcriptPath, "utf-8");
    for (const rawLine of rawContent.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      let entry;
      try {
        entry = JSON.parse(line);
      } catch (_err) {
        continue;
      }

      if (
        entry &&
        typeof entry === "object" &&
        "version" in entry &&
        "id" in entry &&
        !("type" in entry)
      ) {
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

      const timestamp = msToIso(entry.time || entry.created_at || "");
      const data = entry.data && typeof entry.data === "object" ? entry.data : entry;

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
        if (conclusion) session.assistant_conclusions.push(conclusion);
        if (eventType === "assistant/message") continue;
      }

      if (eventType === "tool/call") {
        const callId = String(data.callId || data.id || `seq_${stepIdx}`);
        const toolName = String(data.name || "tool");
        const rawArgs = data.arguments ?? data.args ?? {};
        const args = DshAdapter._parseArgs(rawArgs);
        const toolSum = args.toolSummary || args.description || lastAsstText || "";
        const stepRationale = cleanRationale(toolSum);
        const nodes = this._mapDshToolCall(
          stepIdx,
          toolName,
          args,
          workspaceRoot,
          timestamp,
          stepRationale
        );
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
            errSig = extractErrorSignature(outText);
            if (!errSig && isError && data.error && typeof data.error === "object") {
              errSig = extractErrorSignature(String(data.error.message || ""));
            }
          }

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
        const nodes = this._mapDshToolCall(stepIdx, tName, args, workspaceRoot, timestamp);
        for (const node of nodes) {
          if ("exit_code" in entry || "exitCode" in entry) {
            node.exit_code = Number(entry.exit_code ?? entry.exitCode ?? 0);
          }
          const outText = String(entry.output || entry.stdout || entry.stderr || "");
          if (outText) {
            const errSig = extractErrorSignature(outText);
            if (errSig && node.exit_code !== 0) {
              node.error_signature = errSig;
              session.active_errors.push(errSig);
            }
            node.output_snippet = scrubSecrets(outText.slice(0, 400));
          }
          session.actions.push(node);
          pendingNodes.push(node);
        }
      }
    }

    return session;
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
            for (const fname of ["session.v4.jsonl", "session.v3.jsonl", "session.jsonl"]) {
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

    candidates.sort((a, b) => {
      const sa = statSync(a);
      const sb = statSync(b);
      if (sb.mtimeMs !== sa.mtimeMs) return sb.mtimeMs - sa.mtimeMs;
      const prio = (p) => {
        const base = path.basename(p);
        return base === "session.v4.jsonl" ? 2 : base === "session.v3.jsonl" ? 1 : 0;
      };
      return prio(b) - prio(a);
    });
    return candidates[0];
  }
}
