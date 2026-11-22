/**
 * Deterministic Backward Trace Slicer (Program Slicing applied to Agent Execution Traces).
 *
 * Zero-LLM compiler that takes a raw agent session + Git ground truth and performs:
 * 1. Ground Truth Sink Identification (via git diff / status or mutated files)
 * 2. Dead Mutation Elimination (DME) & Temporal Destroy-Barrier Pruning
 * 3. Error -> Fix Causal Pair Extraction (L2 Universal Invariants)
 * 4. Read Shrinking (pruning exploratory dead-end reads)
 * 5. Speculative Guard Probe Generation (<5ms AST/file assertions)
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { logger } from "./index.mjs";
import {
  extractLocalImports,
  extractSymbolsForLineRanges,
  extractTracebackFiles,
  inferModifiedSymbols,
  isCodeFile,
  isDocFile,
  isFillerUserIntent,
  isHighEntropyErrorSignature,
  isInlineScriptCommand,
  isMetaNonTaskIntent,
  isSourceCodeFile,
  isTestFile,
  scrubSecrets,
} from "./adapters/base.mjs";
import { findCochangeForFiles } from "./cochange.mjs";
import { checkSymbolInAst, extractSymbolSignature } from "./guards.mjs";
import {
  ActionKind,
  CausalRepairPair,
  GuardProbe,
  RawExecutionTrace,
  TracePlaybook,
} from "./models.mjs";

const TOOLCHAIN_INSTALL_RE =
  /\b(?:pip|pip3|uv\s+pip|uv|poetry|npm|pnpm|yarn|bun|cargo|go|apt|apt-get|brew)\s+(?:install|add|get|mod\s+tidy)\b|\bcorepack\s+enable\b|\bpython(?:3)?\s+setup\.py\s+(?:build|develop|build_ext|install)\b/;

const RE_TEST_SCRIPT_CMD =
  /^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:python\d*(?:\.\d+)?|node|bash|sh)\s+(?!-c\b)[\w./\\-]*?(?:test|tests|reproduce|repro|simulate|check|verify|runtests)[\w./\\-]*\b/i;

const VERIFY_WHITELIST_RE =
  /\b(?:pytest|unittest|tox|nox|coverage|mypy|ruff|flake8|black|isort|pylint|runtests(?:\.py)?|manage\.py\s+test|python(?:3)?\s+setup\.py\s+build_ext\b|node\s+--test\b|npm\s+(?:test|run\s+(?:test|build|check|lint)\w*)|pnpm\s+(?:test|run\s+(?:test|build|check|lint)\w*)|yarn\s+(?:test|run\s+(?:test|build|check|lint)\w*)|bun\s+test|cargo\s+(?:test|check|build|clippy)|go\s+(?:test|build|vet)|tsc|eslint|vitest|jest|mocha|make|cmake|gradle|mvn|dsh\s+plugin\s+\S+)\b/;

const EXPLORATORY_CLAUSE_RE =
  /^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:ls|pwd|cd|cat|head|tail|echo|rm|mv|cp|mkdir|ln|chmod|chown|curl|wget|find|grep|rg|sed|awk|which|whoami|id|uname|ps|kill|sleep|true|false|git\s+(?:status|log|diff|add|commit|push|pull|fetch|remote|branch|checkout|config|clone|show|rev-parse|diff-tree)|npm\s+view)\b/;

const ENV_ASSIGN_PREFIX_RE =
  /^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)+/;

const SWALLOW_EXIT_RE = /(?:\|\||;)\s*(?:true|:)\s*$/;
const SCRATCH_FILE_RE =
  /(?:^|\/)(?:repro|reproduce|simulate|scratch|debug|tmp|temp)(?:[_-][^/]*)?\.(?:py|mjs|js)$|(?:^|\/)(?:scratch|tmp)\//i;
const ROOT_SCRIPT_DRIVER_RE =
  /^(?:test(?:[_-].*)?|repro.*|simulate.*|check[_-].*|verify[_-].*)\.(?:py|mjs|js)$/i;

function normalizeRelPathLocal(pathStr) {
  let cleaned = String(pathStr || "")
    .trim()
    .replace(/^["']+|["']+$/g, "");
  if (cleaned.startsWith("./")) cleaned = cleaned.slice(2);
  return cleaned.replace(/\/+$/, "");
}

function splitShellClauses(cmd) {
  const clauses = [];
  let buf = "";
  let inSingle = false;
  let inDouble = false;
  let escaped = false;

  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i];
    if (escaped) {
      buf += ch;
      escaped = false;
      continue;
    }
    if (ch === "\\" && !inSingle) {
      buf += ch;
      escaped = true;
      continue;
    }
    if (ch === "'" && !inDouble) {
      inSingle = !inSingle;
      buf += ch;
      continue;
    }
    if (ch === '"' && !inSingle) {
      inDouble = !inDouble;
      buf += ch;
      continue;
    }
    if (!inSingle && !inDouble) {
      if (ch === ";" || ch === "\n") {
        const part = buf.trim();
        if (part) clauses.push(part);
        buf = "";
        continue;
      }
      if (ch === "&" && i + 1 < cmd.length && cmd[i + 1] === "&") {
        const part = buf.trim();
        if (part) clauses.push(part);
        buf = "";
        i += 1;
        continue;
      }
    }
    buf += ch;
  }
  const tail = buf.trim();
  if (tail) clauses.push(tail);
  return clauses;
}

function extractKilledTargetsFromCommand(cmd, knownFiles) {
  if (!cmd) return new Set();
  const killed = new Set();
  const normKnown = new Map();
  for (const f of knownFiles) {
    if (f) normKnown.set(normalizeRelPathLocal(f), f);
  }

  for (const part of splitShellClauses(cmd)) {
    if (!part) continue;
    let tokens = part.split(/\s+/).filter(Boolean);
    if (tokens.length === 0) continue;

    let idx = 0;
    if (tokens[0] === "sudo") idx += 1;
    while (idx < tokens.length && tokens[idx].includes("=") && !tokens[idx].startsWith("-")) {
      idx += 1;
    }
    tokens = tokens.slice(idx);
    if (tokens.length === 0) continue;

    const head = path.posix.basename(tokens[0]);
    const candidates = [];

    if (head === "rm" || head === "unlink") {
      let afterSep = false;
      for (const tok of tokens.slice(1)) {
        if (tok === "--") {
          afterSep = true;
          continue;
        }
        if (!afterSep && tok.startsWith("-")) continue;
        candidates.push(tok);
      }
    } else if (
      head === "git" &&
      tokens.length >= 2 &&
      ["checkout", "restore", "reset"].includes(tokens[1])
    ) {
      const sub = tokens[1];
      if (
        sub === "checkout" &&
        tokens.slice(2).some((t) => ["-b", "-B", "--orphan"].includes(t))
      ) {
        continue;
      }
      const afterSep = tokens.slice(2).includes("--");
      let seenSep = false;
      for (const tok of tokens.slice(2)) {
        if (tok === "--") {
          seenSep = true;
          continue;
        }
        if (afterSep && !seenSep) continue;
        if (tok.startsWith("-")) continue;
        if (
          sub === "reset" &&
          ["HEAD", "HEAD~1", "ORIG_HEAD"].includes(tok) &&
          !afterSep
        ) {
          continue;
        }
        candidates.push(tok);
      }
    }

    for (const cand of candidates) {
      const nc = normalizeRelPathLocal(cand);
      if (!nc) continue;
      if (nc === "." && head === "git") {
        for (const f of knownFiles) killed.add(f);
        continue;
      }
      if (normKnown.has(nc)) {
        killed.add(normKnown.get(nc));
        continue;
      }
      for (const [nk, orig] of normKnown.entries()) {
        if (nk.startsWith(`${nc}/`)) killed.add(orig);
      }
    }
  }
  return killed;
}

function runGitSafe(args, cwd) {
  try {
    return {
      ok: true,
      stdout: execFileSync("git", args, {
        cwd,
        encoding: "utf-8",
        timeout: 3000,
        stdio: ["ignore", "pipe", "ignore"],
      }),
    };
  } catch (_err) {
    return { ok: false, stdout: "" };
  }
}

export function getGitGroundTruth(workspaceRoot, targetFiles = null) {
  const ws = path.resolve(workspaceRoot);
  if (!existsSync(path.join(ws, ".git"))) {
    return {
      commitHash: null,
      modifiedFiles: new Set(),
      fileSymbols: {},
      commitSubject: null,
    };
  }

  let commitHash = null;
  let commitSubject = null;
  const modifiedFiles = new Set();
  const fileSymbols = {};

  const revRes = runGitSafe(["rev-parse", "--short", "HEAD"], ws);
  if (revRes.ok && revRes.stdout.trim()) {
    commitHash = revRes.stdout.trim();
  }

  const statusRes = runGitSafe(["status", "--porcelain"], ws);
  const statusOut = statusRes.ok ? statusRes.stdout : "";
  // A dirty worktree means the porcelain entries are *uncommitted* changes. They may
  // belong to an earlier session that never committed, so callers must not treat the
  // whole dirty set as this session's edits.
  const worktreeDirty = Boolean(statusOut.trim());

  if (statusRes.ok && statusOut.trim()) {
    for (const line of statusOut.split(/\r?\n/)) {
      if (line.length > 3) {
        let pathStr = line.slice(3).trim().replace(/^"+|"+$/g, "");
        if (pathStr.includes(" -> ")) {
          const parts = pathStr.split(" -> ");
          pathStr = parts[parts.length - 1];
        }
        if (!pathStr.startsWith(".tmem/")) {
          if (!targetFiles || targetFiles.has(pathStr)) {
            modifiedFiles.add(pathStr);
          }
        }
      }
    }
  } else {
    const subjRes = runGitSafe(["log", "-1", "--pretty=%s"], ws);
    if (subjRes.ok && subjRes.stdout.trim()) {
      const subj = subjRes.stdout.trim();
      if (!subj.startsWith("chore(tmem):")) commitSubject = subj;
    }
    const namesRes = runGitSafe(
      ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
      ws
    );
    if (namesRes.ok) {
      for (const line of namesRes.stdout.split(/\r?\n/)) {
        const p = line.trim();
        if (p && !p.startsWith(".tmem/")) {
          if (!targetFiles || targetFiles.has(p)) {
            modifiedFiles.add(p);
          }
        }
      }
    }
  }

  const diffCmd = !statusOut.trim()
    ? ["diff", "-U0", "HEAD~1", "HEAD"]
    : ["diff", "-U0", "HEAD"];
  if (targetFiles && targetFiles.size > 0) {
    diffCmd.push("--", ...Array.from(targetFiles).sort());
  }
  const diffRes = runGitSafe(diffCmd, ws);
  const fileRanges = new Map();
  if (diffRes.ok && diffRes.stdout) {
    let currentFile = null;
    const hunkRe = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/;
    for (const line of diffRes.stdout.split(/\r?\n/)) {
      if (line.startsWith("+++ b/")) {
        currentFile = line.slice(6).trim();
      } else if (currentFile && line.startsWith("@@ ")) {
        const mH = line.match(hunkRe);
        if (mH) {
          const startLn = Math.max(1, parseInt(mH[1], 10));
          const count = mH[2] !== undefined ? parseInt(mH[2], 10) : 1;
          const endLn = count === 0 ? startLn : startLn + count - 1;
          if (!fileRanges.has(currentFile)) fileRanges.set(currentFile, []);
          fileRanges.get(currentFile).push([startLn, endLn]);
        }
      }
    }
  }

  for (const modFile of modifiedFiles) {
    if (!isCodeFile(modFile)) continue;
    const fullPath = path.join(ws, modFile);
    try {
      if (!existsSync(fullPath) || !statSync(fullPath).isFile()) continue;
      const content = readFileSync(fullPath, "utf-8");
      let ranges = fileRanges.get(modFile);
      if (!ranges) {
        const lineCnt = Math.max(1, content.split(/\r?\n/).length);
        ranges = [[1, lineCnt]];
      }
      const syms = extractSymbolsForLineRanges(content, modFile, ranges);
      if (syms.length > 0) {
        fileSymbols[modFile] = syms;
      }
    } catch (err) {
      logger.debug("getGitGroundTruth file scan error:", err?.message || err);
    }
  }

  return { commitHash, modifiedFiles, fileSymbols, commitSubject, worktreeDirty };
}

export function getGitDiffPatch(workspaceRoot, targetFiles = null) {
  const ws = path.resolve(workspaceRoot);
  if (!existsSync(path.join(ws, ".git"))) return null;

  const statusRes = runGitSafe(["status", "--porcelain"], ws);
  const statusOut = statusRes.ok ? statusRes.stdout : "";
  const cmd = !statusOut.trim() ? ["diff", "HEAD~1", "HEAD"] : ["diff", "HEAD"];
  if (targetFiles && targetFiles.size > 0) {
    cmd.push("--", ...Array.from(targetFiles).sort());
  }
  const diffRes = runGitSafe(cmd, ws);
  if (diffRes.ok && diffRes.stdout.trim()) {
    return scrubSecrets(diffRes.stdout.trim());
  }
  return null;
}

export function extractCompactDiffSummary(diffPatch, targetFile, maxLines = 4) {
  if (!diffPatch || !targetFile) return null;
  let inTarget = false;
  const addedLines = [];
  for (const rawLine of diffPatch.split(/\r?\n/)) {
    if (rawLine.startsWith("diff --git")) {
      inTarget =
        rawLine.includes(`a/${targetFile}`) || rawLine.includes(`b/${targetFile}`);
      continue;
    }
    if (inTarget) {
      if (rawLine.startsWith("+") && !rawLine.startsWith("+++")) {
        const stripped = rawLine.slice(1).trim();
        if (
          stripped &&
          !stripped.startsWith("#") &&
          !stripped.startsWith("//") &&
          !stripped.startsWith("/*") &&
          !stripped.startsWith("*")
        ) {
          addedLines.push(stripped);
          if (addedLines.length >= maxLines) break;
        }
      }
    }
  }
  if (addedLines.length === 0) return null;
  let summary = addedLines.slice(0, maxLines).join(" ; ");
  if (summary.length > 120) summary = summary.slice(0, 117) + "...";
  return `+ ${summary}`;
}

export function assessPlaybookTriviality(playbook) {
  if (playbook.l2_causal_repairs && playbook.l2_causal_repairs.length > 0) {
    return [false, "Contains verified L2 causal error-to-fix repairs"];
  }
  const codeFiles = (playbook.touched_files || []).filter((f) => isCodeFile(f));
  if (codeFiles.length === 0) {
    return [true, "Does not touch any source code files (documentation/metadata only)"];
  }
  const hasVerify =
    (playbook.l1_verification_cmds && playbook.l1_verification_cmds.length > 0) ||
    (playbook.l0_critical_path || []).some((s) => s.action === "ENV_EXEC");
  if (!hasVerify) {
    return [true, "Lacks verified test/build execution command"];
  }
  if (codeFiles.length <= 1) {
    return [
      true,
      `Tautological single-file routine on '${codeFiles[0]}' without error-fix causality`,
    ];
  }
  return [false, `Verified multi-file change (${codeFiles.length} code files)`];
}

export function extractCmdRunnerAndTargets(cmd) {
  if (!cmd) return ["", new Set()];
  let tokens = cmd.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return ["", new Set()];

  let idx = 0;
  while (idx < tokens.length && tokens[idx].includes("=") && !tokens[idx].startsWith("-")) {
    idx += 1;
  }
  tokens = tokens.slice(idx);
  if (tokens.length === 0) return ["", new Set()];

  const exe = path.posix.basename(tokens[0]).toLowerCase();
  let runner = exe;
  let argStart = 1;

  if (["python", "python3", "py"].includes(exe)) {
    if (tokens.length >= 3 && tokens[1] === "-m") {
      const mod = tokens[2].toLowerCase();
      runner = mod.startsWith("unittest") ? "unittest" : mod;
      argStart = 3;
    } else {
      runner = "python";
    }
  } else if (["node"].includes(exe)) {
    if (tokens.length >= 2 && tokens[1] === "--test") {
      runner = "node:test";
      argStart = 2;
    } else {
      runner = "node";
    }
  } else if (["uv", "poetry"].includes(exe) && tokens.length >= 3 && tokens[1] === "run") {
    runner = path.posix.basename(tokens[2]).toLowerCase();
    argStart = 3;
  } else if (
    ["npm", "pnpm", "yarn", "cargo", "go", "dotnet"].includes(exe) &&
    tokens.length >= 2
  ) {
    runner = `${exe}:${tokens[1].toLowerCase()}`;
    argStart = 2;
  }

  const targets = new Set();
  const codeExts = [
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".cpp",
    ".c",
  ];
  for (const tok of tokens.slice(argStart)) {
    if (["discover", "-v", "--verbose"].includes(tok) || tok.startsWith("-")) {
      continue;
    }
    const cleaned = tok.replace(/^["']+|["']+$/g, "");
    if (cleaned.includes("/") || codeExts.some((ext) => cleaned.endsWith(ext))) {
      const filePart = cleaned.split("::", 1)[0].replace(/\/+$/, "");
      if (filePart) targets.add(filePart);
    } else if (runner === "unittest" && cleaned.includes(".")) {
      targets.add(cleaned.replace(/\./g, "/") + ".py");
    } else if (["tests", "test", "src"].includes(cleaned)) {
      targets.add(cleaned);
    }
  }

  return [runner, targets];
}

export function commandsAreSameOrigin(failedCmd, verifyCmd) {
  const [rFail, tFail] = extractCmdRunnerAndTargets(failedCmd);
  const [rVer, tVer] = extractCmdRunnerAndTargets(verifyCmd);
  if (!rFail || !rVer || rFail !== rVer) return false;
  if (tFail.size === 0 || tVer.size === 0) return true;
  for (const tf of tFail) {
    for (const tv of tVer) {
      if (
        tf === tv ||
        path.posix.basename(tf) === path.posix.basename(tv) ||
        tf.startsWith(`${tv}/`) ||
        tv.startsWith(`${tf}/`)
      ) {
        return true;
      }
    }
  }
  return false;
}

export class BackwardTraceSlicer {
  constructor() {
    this.last_sliced_step_indices = [];
    this.last_git_diff = null;
    this._last_repair_fix_indices = [];
  }

  _identifyScratchFiles(rawActions, candidateFiles, importerMap) {
    if (candidateFiles.size <= 1) return new Set();

    const hasPermanentRepoFile = Array.from(candidateFiles).some(
      (f) =>
        f.includes("/") &&
        !SCRATCH_FILE_RE.test(f) &&
        (isSourceCodeFile(f) || isTestFile(f))
    );

    const firstRead = new Map();
    const firstMutate = new Map();
    const executedScripts = new Set();

    for (const act of rawActions) {
      if (act.kind === ActionKind.READ && act.target_file) {
        if (!firstRead.has(act.target_file)) {
          firstRead.set(act.target_file, act.step_idx);
        }
      } else if (
        act.kind === ActionKind.MUTATE &&
        act.target_file &&
        (act.exit_code === 0 || act.exit_code === null || act.exit_code === undefined) &&
        !act.error_signature
      ) {
        if (!firstMutate.has(act.target_file)) {
          firstMutate.set(act.target_file, act.step_idx);
        }
      } else if (act.kind === ActionKind.ENV_EXEC && act.command) {
        const [, cmdTargets] = extractCmdRunnerAndTargets(act.command);
        for (const t of cmdTargets) executedScripts.add(t);
      }
    }

    const scratch = new Set();
    for (const f of candidateFiles) {
      const imp = importerMap.get(f);
      if (imp && imp.size > 0) continue;
      if (SCRATCH_FILE_RE.test(f)) {
        scratch.add(f);
        continue;
      }
      const fRead = firstRead.get(f);
      const fMut = firstMutate.has(f) ? firstMutate.get(f) : 1e9;
      if (
        hasPermanentRepoFile &&
        !f.includes("/") &&
        (fRead === undefined || fRead > fMut) &&
        (ROOT_SCRIPT_DRIVER_RE.test(f) || executedScripts.has(f))
      ) {
        scratch.add(f);
      }
    }

    if (scratch.size >= candidateFiles.size) return new Set();
    return scratch;
  }

  /**
   * Identify and prune trailing MUTATE actions that occur after the last passing
   * verification command (`VERIFY_GATE` with `exit_code === 0`), preventing unverified
   * experimental edits at the end of a session from polluting Playbook L0 paths.
   */
  pruneTrailingUnverifiedActions(actions) {
    if (!Array.isArray(actions) || actions.length === 0) {
      return { actions: [], prunedSteps: [], lastVerifyStep: -1 };
    }
    let lastVerifyStep = -1;
    let hasPreVerifyMutate = false;
    let firstMutateStep = Infinity;
    for (const act of actions) {
      if (
        act.kind === ActionKind.MUTATE &&
        act.target_file &&
        !act.target_file.startsWith(".tmem/") &&
        (act.exit_code === 0 || act.exit_code === null || act.exit_code === undefined) &&
        !act.error_signature
      ) {
        firstMutateStep = Math.min(firstMutateStep, act.step_idx);
      } else if (
        act.kind === ActionKind.ENV_EXEC &&
        act.command &&
        (act.exit_code === 0 || act.exit_code === null || act.exit_code === undefined) &&
        !act.error_signature
      ) {
        const canonicalCmd = this._extractCanonicalVerificationCmd(act.command);
        if (!canonicalCmd) continue;
        const clauses = canonicalCmd
          .split("&&")
          .map((c) => c.trim().replace(ENV_ASSIGN_PREFIX_RE, "").trim())
          .filter(Boolean);
        const allInstall =
          clauses.length > 0 && clauses.every((c) => TOOLCHAIN_INSTALL_RE.test(c));
        if (!allInstall && act.step_idx > firstMutateStep) {
          hasPreVerifyMutate = true;
          lastVerifyStep = Math.max(lastVerifyStep, act.step_idx);
        }
      }
    }

    if (!hasPreVerifyMutate || lastVerifyStep < 0) {
      return { actions, prunedSteps: [], lastVerifyStep: -1 };
    }

    const kept = [];
    const prunedSteps = [];
    for (const act of actions) {
      if (
        act.kind === ActionKind.MUTATE &&
        act.target_file &&
        !act.target_file.startsWith(".tmem/") &&
        act.step_idx > lastVerifyStep
      ) {
        prunedSteps.push({
          step_idx: act.step_idx,
          target_file: act.target_file,
          last_verify_step: lastVerifyStep,
        });
        continue;
      }
      kept.push(act);
    }
    return { actions: kept, prunedSteps, lastVerifyStep };
  }

  sliceSession(session, useGitGroundTruth = true, { pruneTrailingUnverified = false } = {}) {
    const allRawActions = session.actions || [];
    if (allRawActions.length === 0) {
      this.last_sliced_step_indices = [];
      this.last_git_diff = null;
      return null;
    }

    const { actions: rawActions, prunedSteps: prunedTrailingSteps } = pruneTrailingUnverified
      ? this.pruneTrailingUnverifiedActions(allRawActions)
      : { actions: allRawActions, prunedSteps: [] };

    const knownFiles = new Set();
    for (const a of rawActions) {
      if (a.target_file && !a.target_file.startsWith(".tmem/")) {
        knownFiles.add(a.target_file);
      }
    }

    const lastKillStep = new Map();
    const lastValidMutateStep = new Map();

    for (const act of rawActions) {
      if (
        act.kind === ActionKind.MUTATE &&
        act.target_file &&
        !act.target_file.startsWith(".tmem/") &&
        (act.exit_code === 0 || act.exit_code === null || act.exit_code === undefined) &&
        !act.error_signature
      ) {
        const prevM = lastValidMutateStep.get(act.target_file) ?? -1;
        if (act.step_idx > prevM) {
          lastValidMutateStep.set(act.target_file, act.step_idx);
        }
      } else if (
        act.kind === ActionKind.ENV_EXEC &&
        act.command &&
        (act.exit_code === 0 || act.exit_code === null || act.exit_code === undefined)
      ) {
        for (const kf of extractKilledTargetsFromCommand(act.command, knownFiles)) {
          const prevK = lastKillStep.get(kf) ?? -1;
          if (act.step_idx > prevK) lastKillStep.set(kf, act.step_idx);
        }
      }
    }

    const sessionMutatedFiles = new Set();
    for (const [f, mStep] of lastValidMutateStep.entries()) {
      if (mStep > (lastKillStep.get(f) ?? -1)) {
        sessionMutatedFiles.add(f);
      }
    }
    const killedFiles = new Set();
    for (const [f, kStep] of lastKillStep.entries()) {
      if (kStep >= (lastValidMutateStep.get(f) ?? -1)) {
        killedFiles.add(f);
      }
    }

    const {
      commitHash,
      modifiedFiles: gitFiles,
      fileSymbols: gitSymbols,
      commitSubject,
      worktreeDirty,
    } = useGitGroundTruth
      ? getGitGroundTruth(
          session.workspace_root,
          sessionMutatedFiles.size > 0 ? sessionMutatedFiles : null
        )
      : {
          commitHash: null,
          modifiedFiles: new Set(),
          fileSymbols: {},
          commitSubject: null,
          worktreeDirty: false,
        };

    const ws = path.resolve(session.workspace_root);
    let effectiveFiles = new Set();
    if (sessionMutatedFiles.size > 0) {
      if (gitFiles.size > 0) {
        for (const f of sessionMutatedFiles) {
          if (gitFiles.has(f)) effectiveFiles.add(f);
        }
        if (effectiveFiles.size === 0) {
          for (const f of sessionMutatedFiles) {
            if (existsSync(path.join(ws, f))) effectiveFiles.add(f);
          }
        }
      } else {
        for (const f of sessionMutatedFiles) {
          if (existsSync(path.join(ws, f))) effectiveFiles.add(f);
        }
      }
      if (effectiveFiles.size === 0) {
        effectiveFiles = new Set(sessionMutatedFiles);
      }
    } else if (gitFiles.size > 0 && !worktreeDirty) {
      // Clean worktree: git ground truth comes from the session's own last commit,
      // so it is already session-scoped.
      effectiveFiles = new Set(gitFiles);
    } else {
      // The worktree is dirty but this span carries no recognized change action.
      // Falling back to the whole dirty set would attribute a previous session's
      // uncommitted edits to this session (cross-session leakage), so restrict
      // attribution to files this span actually referenced.
      effectiveFiles = new Set();
      for (const a of rawActions) {
        if (a.target_file && gitFiles.has(a.target_file)) {
          effectiveFiles.add(a.target_file);
        }
      }
    }

    const candidateReadFiles = new Set();
    for (const a of rawActions) {
      if (a.kind === ActionKind.READ && a.target_file) {
        candidateReadFiles.add(a.target_file);
      }
    }
    const knownImportTargets = new Set([...candidateReadFiles, ...effectiveFiles]);
    const astImportedFiles = new Set();
    const importerMap = new Map();

    for (const modFile of effectiveFiles) {
      if (isCodeFile(modFile)) {
        const fullMod = path.join(ws, modFile);
        try {
          if (existsSync(fullMod) && statSync(fullMod).isFile()) {
            const modText = readFileSync(fullMod, "utf-8");
            const imported = extractLocalImports(modText, modFile, knownImportTargets);
            for (const dep of imported) {
              astImportedFiles.add(dep);
              if (!importerMap.has(dep)) importerMap.set(dep, new Set());
              importerMap.get(dep).add(modFile);
            }
          }
        } catch (_err) {
          // ignore
        }
      }
    }

    let scratchFiles = new Set();
    if (gitFiles.size === 0 && effectiveFiles.size > 0) {
      scratchFiles = this._identifyScratchFiles(rawActions, effectiveFiles, importerMap);
      if (scratchFiles.size > 0) {
        effectiveFiles = new Set(
          Array.from(effectiveFiles).filter((f) => !scratchFiles.has(f))
        );
      }
    }

    const deadTargetFiles = new Set([...killedFiles, ...scratchFiles]);
    const effectiveLastMutateStep = new Map();
    let maxEffectiveMutateStep = -1;
    for (const f of effectiveFiles) {
      if (lastValidMutateStep.has(f)) {
        const step = lastValidMutateStep.get(f);
        effectiveLastMutateStep.set(f, step);
        if (step > maxEffectiveMutateStep) maxEffectiveMutateStep = step;
      }
    }

    const gitDiffPatch = useGitGroundTruth
      ? getGitDiffPatch(
          session.workspace_root,
          effectiveFiles.size > 0 ? effectiveFiles : null
        )
      : null;
    this.last_git_diff = gitDiffPatch;

    const perFileSymbols = {};
    for (const [f, syms] of Object.entries(gitSymbols)) {
      if (syms && syms.length > 0) perFileSymbols[f] = [...syms];
    }

    const [causalRepairs, repairStepIndices] = this._extractCausalRepairsWithIndices(
      rawActions,
      effectiveFiles,
      {
        workspaceRoot: session.workspace_root,
        deadFiles: deadTargetFiles,
        lastKillStep,
        fileSymbols: perFileSymbols,
      }
    );

    const readSteps = [];
    const readStepIndices = [];
    const mutateSteps = [];
    const mutateStepIndices = [];
    const seenReads = new Set();
    const seenMutations = new Set();
    const mutateMap = new Map();
    const touchedSymbols = new Set();

    for (const symList of Object.values(gitSymbols)) {
      for (const s of symList) touchedSymbols.add(s);
    }

    const genericStems = new Set([
      "__init__",
      "utils",
      "util",
      "helpers",
      "helper",
      "common",
      "config",
      "constants",
      "types",
      "base",
      "models",
      "test",
      "tests",
    ]);
    const effectiveModules = new Set();
    for (const f of effectiveFiles) {
      if (!f) continue;
      const stem = path.posix.parse(f).name;
      if (stem.length >= 4 && !genericStems.has(stem)) {
        effectiveModules.add(stem);
      }
    }

    const consumerLastMutate = new Map();
    for (const [dep, importers] of importerMap.entries()) {
      let maxM = -1;
      for (const c of importers) {
        if (effectiveLastMutateStep.has(c)) {
          maxM = Math.max(maxM, effectiveLastMutateStep.get(c));
        }
      }
      consumerLastMutate.set(dep, maxM);
    }

    const tracebackDepLastMutate = new Map();
    for (const act of rawActions) {
      if (
        act.kind === ActionKind.ENV_EXEC &&
        ((act.exit_code !== 0 && act.exit_code !== null && act.exit_code !== undefined) ||
          Boolean(act.error_signature))
      ) {
        let laterMutate = -1;
        for (const m of effectiveLastMutateStep.values()) {
          if (m > act.step_idx && m > laterMutate) laterMutate = m;
        }
        if (laterMutate >= 0) {
          for (const tf of extractTracebackFiles(
            act.raw_output || act.output_snippet,
            session.workspace_root
          )) {
            if (laterMutate > (tracebackDepLastMutate.get(tf) ?? -1)) {
              tracebackDepLastMutate.set(tf, laterMutate);
            }
          }
        }
      }
    }

    const envInstallEntries = [];
    const installCmdsSeen = new Set();
    const latestVerifyByCmd = new Map();
    const fallbackVerifyByCmd = new Map();

    for (const action of rawActions) {
      if (action.kind === ActionKind.READ && action.target_file) {
        if (action.step_idx <= (lastKillStep.get(action.target_file) ?? -1)) continue;
        const targetStem = path.posix.parse(action.target_file).name;
        const isDirect =
          effectiveFiles.has(action.target_file) &&
          action.step_idx <= (effectiveLastMutateStep.get(action.target_file) ?? -1);
        const isAstImport =
          astImportedFiles.has(action.target_file) &&
          action.step_idx <= (consumerLastMutate.get(action.target_file) ?? -1);
        const isTracebackDep =
          tracebackDepLastMutate.has(action.target_file) &&
          action.step_idx <= tracebackDepLastMutate.get(action.target_file);
        const isSpecificStem = targetStem.length >= 4 && !genericStems.has(targetStem);
        const isModuleDep =
          isSpecificStem &&
          !deadTargetFiles.has(action.target_file) &&
          action.step_idx <= maxEffectiveMutateStep &&
          (effectiveModules.has(targetStem) ||
            Array.from(effectiveModules).some(
              (m) => targetStem === `test_${m}` || m === `test_${targetStem}`
            ));

        if (
          (isDirect || isAstImport || isTracebackDep || isModuleDep) &&
          !seenReads.has(action.target_file)
        ) {
          seenReads.add(action.target_file);
          readStepIndices.push(action.step_idx);
          const stepEntry = {
            step: action.step_idx,
            action: ActionKind.READ,
            target: action.target_file,
          };
          if (action.rationale) stepEntry.rationale = action.rationale;
          readSteps.push(stepEntry);
        }
      } else if (action.kind === ActionKind.MUTATE && action.target_file) {
        if (
          (action.exit_code !== 0 && action.exit_code !== null && action.exit_code !== undefined) ||
          action.error_signature
        ) {
          continue;
        }
        if (action.step_idx <= (lastKillStep.get(action.target_file) ?? -1)) continue;
        if (effectiveFiles.has(action.target_file)) {
          for (const s of action.symbols_touched || []) touchedSymbols.add(s);
          if (action.symbols_touched && action.symbols_touched.length > 0) {
            if (!perFileSymbols[action.target_file]) {
              perFileSymbols[action.target_file] = [];
            }
            for (const sym of action.symbols_touched) {
              if (!perFileSymbols[action.target_file].includes(sym)) {
                perFileSymbols[action.target_file].push(sym);
              }
            }
          }
          mutateStepIndices.push(action.step_idx);
          if (!seenMutations.has(action.target_file)) {
            seenMutations.add(action.target_file);
            const fileSyms = Array.from(
              new Set([
                ...(action.symbols_touched || []),
                ...(gitSymbols[action.target_file] || []),
              ])
            ).sort();
            const diffSum = extractCompactDiffSummary(gitDiffPatch, action.target_file);
            const stepEntry = {
              step: action.step_idx,
              action: ActionKind.MUTATE,
              target: action.target_file,
            };
            if (fileSyms.length > 0) stepEntry.symbols = fileSyms;
            if (action.rationale) stepEntry.rationale = action.rationale;
            if (diffSum) stepEntry.diff_summary = diffSum;
            mutateSteps.push(stepEntry);
            mutateMap.set(action.target_file, stepEntry);
          } else {
            const existingEntry = mutateMap.get(action.target_file);
            const mergedSyms = Array.from(
              new Set([
                ...(existingEntry.symbols || []),
                ...(action.symbols_touched || []),
              ])
            ).sort();
            if (mergedSyms.length > 0) existingEntry.symbols = mergedSyms;
            if (action.rationale && !existingEntry.rationale) {
              existingEntry.rationale = action.rationale;
            }
          }
        }
      } else if (action.kind === ActionKind.ENV_EXEC && action.command) {
        if (
          (action.exit_code === 0 || action.exit_code === null || action.exit_code === undefined) &&
          !action.error_signature
        ) {
          const canonicalCmd = this._extractCanonicalVerificationCmd(action.command);
          if (!canonicalCmd) continue;
          const [, cmdTargets] = extractCmdRunnerAndTargets(canonicalCmd);
          const isDeadTargetCmd =
            cmdTargets.size > 0 &&
            Array.from(cmdTargets).every(
              (t) =>
                deadTargetFiles.has(t) ||
                action.step_idx <= (lastKillStep.get(t) ?? -1)
            );

          const clauses = canonicalCmd
            .split("&&")
            .map((c) => c.trim().replace(ENV_ASSIGN_PREFIX_RE, "").trim())
            .filter(Boolean);
          const allInstall =
            clauses.length > 0 && clauses.every((c) => TOOLCHAIN_INSTALL_RE.test(c));

          const stepEntry = {
            step: action.step_idx,
            action: ActionKind.ENV_EXEC,
            command: canonicalCmd,
          };
          if (action.rationale) stepEntry.rationale = action.rationale;

          if (allInstall) {
            if (!installCmdsSeen.has(canonicalCmd)) {
              installCmdsSeen.add(canonicalCmd);
              envInstallEntries.push([action.step_idx, canonicalCmd, stepEntry]);
            }
          } else if (isDeadTargetCmd) {
            let earliestKill = 1e9;
            for (const t of cmdTargets) {
              earliestKill = Math.min(earliestKill, lastKillStep.get(t) ?? 1e9);
            }
            if (effectiveFiles.size > 0 && action.step_idx < earliestKill) {
              fallbackVerifyByCmd.delete(canonicalCmd);
              fallbackVerifyByCmd.set(canonicalCmd, [action.step_idx, stepEntry]);
            }
          } else {
            latestVerifyByCmd.delete(canonicalCmd);
            latestVerifyByCmd.set(canonicalCmd, [action.step_idx, stepEntry]);
          }
        }
      }
    }

    const effectiveVerifyMap =
      latestVerifyByCmd.size > 0 ? latestVerifyByCmd : fallbackVerifyByCmd;
    const allVerifyItems = Array.from(effectiveVerifyMap.entries()).map(
      ([cmd, [idx, entry]]) => [idx, cmd, entry]
    );
    const postEditVerify = allVerifyItems.filter(
      (item) => item[0] >= maxEffectiveMutateStep
    );
    const chosenVerifyItems = (
      postEditVerify.length > 0 ? postEditVerify : allVerifyItems
    ).slice(-2);

    const lastActiveStep = Math.max(
      maxEffectiveMutateStep,
      ...chosenVerifyItems.map((item) => item[0]),
      -1
    );
    const chosenInstallItems = envInstallEntries
      .filter((item) => lastActiveStep < 0 || item[0] <= lastActiveStep)
      .slice(0, 3);

    const chosenEnvItems = [...chosenInstallItems, ...chosenVerifyItems];
    const envSteps = chosenEnvItems.map((item) => item[2]);
    const slicedEnvIndices = chosenEnvItems.map((item) => item[0]);
    const verificationCmds = [
      ...envInstallEntries.map((item) => item[1]),
      ...allVerifyItems.map((item) => item[1]),
    ];

    const slicedSteps = [...readSteps, ...mutateSteps, ...envSteps].sort(
      (a, b) => Number(a.step || 0) - Number(b.step || 0)
    );

    const collectedIndices = new Set([
      ...readStepIndices,
      ...mutateStepIndices,
      ...slicedEnvIndices,
      ...repairStepIndices,
    ]);
    this.last_sliced_step_indices = Array.from(collectedIndices).sort((a, b) => a - b);

    if (slicedSteps.length === 0 && causalRepairs.length === 0) {
      return null;
    }

    const sortedFiles = Array.from(effectiveFiles).sort();
    const guards = this._buildGuards(session.workspace_root, sortedFiles, perFileSymbols);

    let cochangeTopology = findCochangeForFiles(
      session.workspace_root,
      new Set(sortedFiles)
    );
    if (cochangeTopology.length === 0) {
      const focusedFiles = sortedFiles.filter(
        (f) => isSourceCodeFile(f) || isTestFile(f)
      );
      if (focusedFiles.length >= 2 && focusedFiles.length <= 5) {
        cochangeTopology.push(`Co-edited in session: ${focusedFiles.join(" <-> ")}`);
      }
    }

    const behaviorParts = [];
    if (readSteps.length > 0) behaviorParts.push(`read ${readSteps.length} file(s)`);
    if (mutateSteps.length > 0) behaviorParts.push(`edited ${mutateSteps.length} file(s)`);
    if (causalRepairs.length > 0)
      behaviorParts.push(`repaired ${causalRepairs.length} error(s)`);
    if (verificationCmds.length > 0)
      behaviorParts.push(`verified (\`${verificationCmds[verificationCmds.length - 1]}\`)`);
    const behaviorDigest = behaviorParts.length
      ? `Behavior: ${behaviorParts.join(" -> ")}`
      : "";

    const [problemIntent, semanticSummary] = this._synthesizeSemanticTopic({
      session,
      sortedFiles,
      sortedSymbols: Array.from(touchedSymbols).sort(),
      commitSubject,
      behaviorDigest,
    });
    if (problemIntent === "non_task") {
      return null;
    }

    const errorSigs = Array.from(
      new Set([
        ...causalRepairs.map((r) => r.error_signature).filter(Boolean),
        ...(session.active_errors || []),
      ])
    ).sort();

    const rootCause =
      causalRepairs.length > 0
        ? `Error '${causalRepairs[0].error_signature}' resolved by '${causalRepairs[0].repair_action}'`
        : null;

    const fingerprint = `${problemIntent}|${sortedFiles.join("|")}|${errorSigs.join("|")}`;
    const traceId =
      "tr_" + createHash("sha256").update(fingerprint, "utf-8").digest("hex").slice(0, 10);

    const playbook = new TracePlaybook({
      trace_id: traceId,
      agent_source: session.agent_name,
      scope: "project",
      created_at: new Date().toISOString(),
      git_commit: commitHash,
      schema_version: 3,
      source_session_ids: session.session_id ? [session.session_id] : [],
      problem_intent: problemIntent.slice(0, 300),
      semantic_summary: semanticSummary.slice(0, 350),
      error_signatures: errorSigs,
      touched_files: sortedFiles,
      touched_symbols: Array.from(touchedSymbols).sort(),
      guards,
      l0_critical_path: slicedSteps,
      l1_cochange_topology: cochangeTopology,
      l1_verification_cmds: verificationCmds.slice(-3),
      l2_causal_repairs: causalRepairs,
      root_cause: rootCause,
      pitfalls: [],
      raw_steps_count: rawActions.length,
      sliced_steps_count: slicedSteps.length,
    });

    const [isTrivial, trivialReason] = assessPlaybookTriviality(playbook);
    playbook.is_trivial = isTrivial;
    if (isTrivial) {
      playbook.pitfalls.push(`Trivial trace: ${trivialReason}`);
    }
    for (const p of prunedTrailingSteps) {
      playbook.pitfalls.push(
        `Pruned unverified trailing edit on '${p.target_file}' at step ${p.step_idx} (after last passing verify at step ${p.last_verify_step})`
      );
    }

    let conf = 0.3;
    if (verificationCmds.length > 0) conf += 0.15;
    if (causalRepairs.length > 0) conf += 0.15;
    if (sortedFiles.length > 1 || touchedSymbols.size > 0) conf += 0.1;

    conf = isTrivial ? Math.min(conf, 0.4) : Math.min(conf, 0.7);
    playbook.confidence = Math.round(conf * 100) / 100;
    return playbook;
  }

  slice(session, useGitGroundTruth = true) {
    return this.sliceSession(session, useGitGroundTruth);
  }

  _extractCausalRepairs(actions, effectiveFiles, workspaceRoot = "") {
    const [repairs] = this._extractCausalRepairsWithIndices(actions, effectiveFiles, {
      workspaceRoot,
    });
    return repairs;
  }

  _extractCausalRepairsWithIndices(
    actions,
    effectiveFiles,
    {
      workspaceRoot = "",
      deadFiles = null,
      lastKillStep = null,
      fileSymbols = null,
    } = {}
  ) {
    const repairs = [];
    const stepIndices = [];
    this._last_repair_fix_indices = [];
    const seenSigs = new Set();
    const deadSet = deadFiles || new Set();
    const killMap = lastKillStep || new Map();
    const symMap = fileSymbols || {};

    for (let i = 0; i < actions.length; i++) {
      const act = actions[i];
      const isFailure =
        act.kind === ActionKind.ENV_EXEC &&
        ((act.exit_code !== 0 && act.exit_code !== null && act.exit_code !== undefined) ||
          Boolean(act.error_signature));
      if (!isFailure) continue;

      if (isInlineScriptCommand(act.command)) continue;

      const failedCanonical = this._extractCanonicalVerificationCmd(act.command || "");
      if (!failedCanonical) continue;

      const [, failTargets] = extractCmdRunnerAndTargets(failedCanonical);
      if (
        failTargets.size > 0 &&
        Array.from(failTargets).every(
          (t) => deadSet.has(t) || act.step_idx <= (killMap.get(t) ?? -1)
        )
      ) {
        continue;
      }
      const failedCmd = failedCanonical;

      const errSig = act.error_signature;
      if (!errSig || !isHighEntropyErrorSignature(errSig)) continue;
      if (seenSigs.has(errSig)) continue;

      const interveningFixes = [];
      const interveningIndices = [];
      const interveningMutatedFiles = new Set();
      let isEnv = false;
      let verifiedCmd = "";
      let verifyIdx = null;

      for (let j = i + 1; j < Math.min(actions.length, i + 36); j++) {
        const nxt = actions[j];
        if (
          nxt.kind === ActionKind.MUTATE &&
          effectiveFiles.has(nxt.target_file) &&
          (nxt.exit_code === 0 || nxt.exit_code === null || nxt.exit_code === undefined) &&
          !nxt.error_signature &&
          nxt.step_idx > (killMap.get(nxt.target_file) ?? -1)
        ) {
          interveningMutatedFiles.add(nxt.target_file);
          let nodeSyms = [...(nxt.symbols_touched || [])];
          if (nodeSyms.length === 0 && nxt.target_file) {
            if (symMap[nxt.target_file] && symMap[nxt.target_file].length > 0) {
              nodeSyms = [...symMap[nxt.target_file]];
            } else if (workspaceRoot && nxt.raw_input) {
              const rawIn = nxt.raw_input;
              const snip = String(
                rawIn.content ??
                  rawIn.new_string ??
                  rawIn.new_str ??
                  rawIn.file_text ??
                  rawIn.replacement ??
                  ""
              );
              if (snip) {
                nodeSyms = inferModifiedSymbols(workspaceRoot, nxt.target_file, {
                  newSnippet: snip,
                });
              }
            }
          }
          const symNote =
            nodeSyms.length > 0 ? ` (${nodeSyms.slice(0, 3).join(", ")})` : "";
          const fixDesc = `edit ${nxt.target_file}${symNote}`;
          if (!interveningFixes.includes(fixDesc)) {
            interveningFixes.push(fixDesc);
            interveningIndices.push(nxt.step_idx);
          }
        } else if (nxt.kind === ActionKind.ENV_EXEC && nxt.command) {
          const installClause = this._extractToolchainInstallClause(nxt.command);
          if (
            installClause &&
            (nxt.exit_code === 0 || nxt.exit_code === null || nxt.exit_code === undefined)
          ) {
            const fixDesc = `run \`${installClause}\``;
            if (!interveningFixes.includes(fixDesc)) {
              interveningFixes.push(fixDesc);
              interveningIndices.push(nxt.step_idx);
            }
            isEnv = true;
          } else if (
            nxt.exit_code === 0 &&
            !nxt.error_signature &&
            interveningFixes.length > 0
          ) {
            const candVerify = this._extractCanonicalVerificationCmd(nxt.command);
            if (candVerify && commandsAreSameOrigin(failedCmd, candVerify)) {
              verifiedCmd = candVerify;
              verifyIdx = nxt.step_idx;
              break;
            }
          }
        }
      }

      if (interveningFixes.length > 0 && verifiedCmd) {
        let hasSymbolOrMultifile = true;
        if (!isEnv) {
          if (Array.from(interveningMutatedFiles).every((f) => isDocFile(f))) {
            continue;
          }
          const tbFiles = extractTracebackFiles(
            act.raw_output || act.output_snippet,
            workspaceRoot
          );
          if (
            tbFiles.size > 0 &&
            !Array.from(tbFiles).some((f) => interveningMutatedFiles.has(f))
          ) {
            continue;
          }
          if (Array.from(tbFiles).some((f) => isSourceCodeFile(f))) {
            if (
              Array.from(interveningMutatedFiles).every(
                (f) => isTestFile(f) || isDocFile(f)
              )
            ) {
              continue;
            }
          }
          hasSymbolOrMultifile = interveningFixes.some(
            (fix) => fix.includes("(") || effectiveFiles.size > 1
          );
          const hasSourceFix = Array.from(interveningMutatedFiles).some((f) =>
            isSourceCodeFile(f)
          );
          if (!(hasSymbolOrMultifile || hasSourceFix)) continue;
        }

        seenSigs.add(errSig);
        repairs.push(
          new CausalRepairPair({
            error_signature: errSig,
            failed_command: failedCmd,
            repair_action: interveningFixes.slice(0, 3).join(" -> "),
            verification_cmd: verifiedCmd,
            is_env_or_toolchain: isEnv,
          })
        );
        this._last_repair_fix_indices.push([...interveningIndices]);
        if (hasSymbolOrMultifile || isEnv) {
          stepIndices.push(act.step_idx, ...interveningIndices);
          if (verifyIdx !== null) stepIndices.push(verifyIdx);
        } else {
          stepIndices.push(...interveningIndices);
        }
      }
    }

    return [repairs, stepIndices];
  }

  buildRawTrace(session, playbook, diffPatch = null) {
    const patch = diffPatch || this.last_git_diff;
    return new RawExecutionTrace({
      trace_id: playbook.trace_id,
      session_id: session.session_id,
      agent_source: session.agent_name,
      workspace_root: session.workspace_root,
      created_at: playbook.created_at,
      git_commit: playbook.git_commit,
      user_intents: [...(session.user_intents || [])],
      assistant_conclusions: [...(session.assistant_conclusions || [])],
      total_steps: (session.actions || []).length,
      actions: (session.actions || []).map((a) =>
        typeof a.toDict === "function" ? a.toDict() : a
      ),
      sliced_step_indices: [...this.last_sliced_step_indices],
      diff_patch: patch,
      metadata: {
        raw_steps_count: (session.actions || []).length,
        sliced_steps_count: (playbook.l0_critical_path || []).length,
        causal_repairs_count: (playbook.l2_causal_repairs || []).length,
      },
    });
  }

  _buildGuards(workspaceRoot, files, perFileSymbols) {
    const guards = [];
    const ws = path.resolve(workspaceRoot);
    const orderedFiles = [...files]
      .sort((a, b) => {
        const ra = isSourceCodeFile(a) ? 0 : isTestFile(a) ? 1 : 2;
        const rb = isSourceCodeFile(b) ? 0 : isTestFile(b) ? 1 : 2;
        if (ra !== rb) return ra - rb;
        return a < b ? -1 : a > b ? 1 : 0;
      })
      .slice(0, 8);

    for (const f of orderedFiles) {
      guards.push(new GuardProbe({ probe_type: "file_exists", target: f }));
      if (!isCodeFile(f)) continue;
      const candSyms = perFileSymbols[f] || [];
      if (candSyms.length === 0) continue;
      const fullPath = path.join(ws, f);
      try {
        if (existsSync(fullPath) && statSync(fullPath).isFile()) {
          const text = readFileSync(fullPath, "utf-8");
          let addedForFile = 0;
          for (const sym of candSyms) {
            if (checkSymbolInAst(text, f, sym)) {
              guards.push(
                new GuardProbe({
                  probe_type: "symbol_present",
                  target: f,
                  expected: sym,
                })
              );
              const sig = extractSymbolSignature(text, f, sym);
              if (sig) {
                guards.push(
                  new GuardProbe({
                    probe_type: "signature_match",
                    target: f,
                    expected: `${sym}:${sig}`,
                  })
                );
              }
              addedForFile += 1;
              if (addedForFile >= 3) break;
            }
          }
        }
      } catch (_err) {
        // ignore
      }
    }
    return guards;
  }

  _synthesizeSemanticTopic({
    session,
    sortedFiles,
    sortedSymbols,
    commitSubject,
    behaviorDigest = "",
  }) {
    const cleanIntents = (session.user_intents || [])
      .map((u) => u.trim())
      .filter((u) => !isFillerUserIntent(u) && !u.startsWith("/") && !u.startsWith("<tmem"));

    let rawUser = "";
    if (cleanIntents.length > 0) {
      if (cleanIntents.length === 1) {
        rawUser = cleanIntents[0];
      } else {
        const firstU = cleanIntents[0];
        const lastU = cleanIntents[cleanIntents.length - 1];
        if (firstU.length >= 20 && lastU.length >= 20 && firstU !== lastU) {
          rawUser = `${firstU} — ${lastU}`;
        } else {
          rawUser = cleanIntents.reduce((a, b) => (b.length > a.length ? b : a), cleanIntents[0]);
        }
      }
    } else if (session.user_intents && session.user_intents.length > 0) {
      rawUser = session.user_intents[session.user_intents.length - 1].trim();
    }

    if (
      isMetaNonTaskIntent(rawUser) ||
      cleanIntents.some((u) => isMetaNonTaskIntent(u))
    ) {
      return ["non_task", "non_task"];
    }

    let problemIntent;
    if (rawUser) problemIntent = rawUser;
    else if (commitSubject) problemIntent = commitSubject;
    else problemIntent = `Modify ${sortedFiles.slice(0, 3).join(", ")}`;

    const summaryParts = [];
    if (commitSubject && commitSubject !== problemIntent) {
      summaryParts.push(commitSubject);
    }
    if (session.assistant_conclusions && session.assistant_conclusions.length > 0) {
      summaryParts.push(
        session.assistant_conclusions[session.assistant_conclusions.length - 1]
      );
    }
    if (sortedSymbols.length > 0) {
      summaryParts.push(`Key symbols: ${sortedSymbols.slice(0, 6).join(", ")}`);
    } else if (sortedFiles.length > 0) {
      summaryParts.push(`Key modules: ${sortedFiles.slice(0, 4).join(", ")}`);
    }
    if (behaviorDigest) summaryParts.push(behaviorDigest);

    return [problemIntent, summaryParts.join(" | ")];
  }

  _extractToolchainInstallClause(cmd) {
    if (!cmd) return null;
    for (const part of splitShellClauses(cmd)) {
      if (!part || part.includes("tmem.cli") || part.includes("cli.mjs")) continue;
      if (TOOLCHAIN_INSTALL_RE.test(part)) return part;
    }
    return null;
  }

  _extractCanonicalVerificationCmd(cmd) {
    if (!cmd) return null;
    const stripped = String(cmd).trim();
    if (stripped.includes("for pkg in ")) return null;

    const keptClauses = [];
    for (const part of splitShellClauses(stripped)) {
      if (!part) continue;
      if (SWALLOW_EXIT_RE.test(part)) continue;
      const basePart = part.split(/\|\s*(?:grep|head|tail|cat)\b/)[0].trim();
      if (!basePart) continue;
      if (basePart.includes("tmem.cli") || /\btmem\b.*cli\.mjs\b/.test(basePart)) continue;
      if (EXPLORATORY_CLAUSE_RE.test(basePart)) continue;
      const cmdForSyntax = basePart.replace(ENV_ASSIGN_PREFIX_RE, "").trim();
      if (!cmdForSyntax) continue;
      if (isInlineScriptCommand(cmdForSyntax)) continue;
      if (
        /\s+=\s+/.test(cmdForSyntax) ||
        /^(?:[A-Za-z_]\w*\s*=\s*|print\(|def\s+|class\s+|import\s+|from\s+)/.test(
          cmdForSyntax
        )
      ) {
        continue;
      }
      if (
        VERIFY_WHITELIST_RE.test(cmdForSyntax) ||
        TOOLCHAIN_INSTALL_RE.test(cmdForSyntax) ||
        RE_TEST_SCRIPT_CMD.test(basePart)
      ) {
        if (!keptClauses.includes(basePart)) {
          keptClauses.push(basePart);
        }
      }
    }
    if (keptClauses.length === 0) return null;
    return keptClauses.join(" && ");
  }
}
