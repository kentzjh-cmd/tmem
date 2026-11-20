/**
 * Base protocol and utilities for multi-agent transcript & hook adapters.
 */

import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import {
  extractTsLocalImports,
  extractTsSymbolsForLineRanges,
  extractTsSymbolsFromText,
  parsePythonStructure,
} from "../ast_parser.mjs";

export const ERROR_PATTERNS = [
  /([A-Za-z0-9_.]*(?:Error|Exception|Fault|Panic)[A-Za-z0-9_.:]*[^\n]{0,120})/,
  /(error\[E\d+\]:[^\n]{0,120})/,
  /(Cannot find module\s+['"][^'"]+['"][^\n]{0,80})/,
  /(ModuleNotFoundError:[^\n]{0,100})/,
  /(ImportError:[^\n]{0,100})/,
  /(linker `[^`]+` not found)/,
];

const GENERIC_FAILURE_SUMMARY_RE =
  /^(?:FAILED\s*\([^)]*\)|FAIL:\s*\S+|ERRORS?!?\s*\([^)]*\)|Process exited with code \d+|Command failed \(exit \d+\)|Exit code \d+|.*tests? failed.*)$/i;

const LOW_ENTROPY_SIGNATURES = new Set([
  "error",
  "failed",
  "failure",
  "test failed",
  "tests failed",
  "command failed",
  "exit code 1",
  "exit code 2",
  "exit code 127",
]);

const ERROR_ARCHETYPE_KEYWORDS = [
  "Error",
  "Exception",
  "Fault",
  "Panic",
  "error[E",
  "Cannot find module",
  "not found",
  "unresolved",
  "undefined",
  "Assertion",
  "syntax error",
];

export function isHighEntropyErrorSignature(sig) {
  if (!sig) return false;
  const s = String(sig).trim();
  if (s.length < 8) return false;
  if (LOW_ENTROPY_SIGNATURES.has(s.toLowerCase())) return false;
  if (GENERIC_FAILURE_SUMMARY_RE.test(s)) return false;
  if (s.startsWith("Command failed (exit ") || s.startsWith("Process exited with code ")) {
    return false;
  }
  if (/^FAILED\b/i.test(s) || /^FAIL:\s*/i.test(s)) return false;
  return ERROR_ARCHETYPE_KEYWORDS.some((kw) => s.includes(kw));
}

const SECRET_REDACT_RE =
  /\b(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{35})\b/g;

export function scrubSecrets(text) {
  if (!text) return "";
  return String(text).replace(SECRET_REDACT_RE, "[REDACTED_SECRET]");
}

export function cleanRationale(text, maxChars = 160) {
  if (!text) return null;
  const scrubbed = scrubSecrets(text).trim();
  let compact = scrubbed.replace(/\s+/g, " ").trim();
  compact = compact.replace(/^(?:#+\s*|[-*]\s*)/, "").trim();
  if (!compact || compact.length < 3) return null;
  if (compact.length > maxChars) {
    compact = compact.slice(0, maxChars - 3).trimEnd() + "...";
  }
  return compact;
}

export class ParsedSession {
  constructor({
    agent_name,
    session_id,
    workspace_root,
    user_intents = [],
    assistant_conclusions = [],
    actions = [],
    active_errors = [],
  } = {}) {
    this.agent_name = agent_name;
    this.session_id = session_id;
    this.workspace_root = workspace_root;
    this.user_intents = [...user_intents];
    this.assistant_conclusions = [...assistant_conclusions];
    this.actions = [...actions];
    this.active_errors = [...active_errors];
  }
}

export const FILLER_INTENT_SET = new Set([
  "done",
  "ok",
  "okay",
  "yes",
  "y",
  "go",
  "continue",
  "继续",
  "可以",
  "好的",
  "好",
  "行",
  "确认",
  "没问题",
]);

export function isFillerUserIntent(text) {
  const s = String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[.!！。，,]+$/g, "");
  if (!s) return true;
  return FILLER_INTENT_SET.has(s);
}

export function extractAssistantConclusion(text) {
  if (!text) return null;
  let cleaned = String(text).replace(/```[\s\S]*?```/g, "");
  cleaned = cleaned.replace(/<!--[\s\S]*?-->/g, "");
  cleaned = cleaned.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");

  const meaningfulLines = [];
  for (const rawLine of cleaned.split(/\r?\n/)) {
    let ln = rawLine.trim();
    if (
      !ln ||
      ln.startsWith("> 💡") ||
      ln.startsWith("---") ||
      ln.startsWith("```") ||
      ln.startsWith("<tmem_")
    ) {
      continue;
    }
    if (ln.includes("是否需要将本次有效执行路径沉淀为记忆")) continue;
    ln = ln.replace(/^(?:#+\s*|\d+\.\s*|[-*]\s*)/, "").trim();
    if (ln.length >= 12) {
      meaningfulLines.push(ln);
      if (meaningfulLines.length >= 2) break;
    }
  }
  if (meaningfulLines.length === 0) return null;
  return scrubSecrets(meaningfulLines.join("；")).slice(0, 240);
}

export function normalizeRelPath(rawPath, workspaceRoot) {
  if (!rawPath) return null;
  let cleaned = String(rawPath).trim().replace(/^["']+|["']+$/g, "");
  if (!cleaned) return null;
  try {
    const ws = path.resolve(workspaceRoot || process.cwd());
    if (path.isAbsolute(cleaned)) {
      const resolved = path.resolve(cleaned);
      if (resolved === ws || resolved.startsWith(ws + path.sep)) {
        return path.relative(ws, resolved).replace(/\\/g, "/");
      }
    }
    while (cleaned.startsWith("./")) {
      cleaned = cleaned.slice(2);
    }
    return cleaned.replace(/\\/g, "/");
  } catch (_err) {
    return cleaned;
  }
}

export function extractErrorSignature(text, { scanLines = 40 } = {}) {
  if (!text) return null;
  const rawLines = String(text)
    .split(/\r?\n/)
    .map((ln) => ln.trim())
    .filter(Boolean);
  if (rawLines.length === 0) return null;

  const cleanedLines = rawLines.map((ln) => {
    if (ln.startsWith("E   ") || ln.startsWith("E ")) {
      return ln.slice(2).trim();
    }
    return ln;
  });

  const matchLine = (ln) => {
    for (const pat of ERROR_PATTERNS) {
      const m = ln.match(pat);
      if (m) {
        const sig = m[1].trim();
        if (!sig.includes("=>") && isHighEntropyErrorSignature(sig)) {
          return sig.slice(0, 160);
        }
      }
    }
    return null;
  };

  // Prioritize the last 40 lines (standard traceback tail)
  const tailStart = Math.max(0, cleanedLines.length - 40);
  for (let i = cleanedLines.length - 1; i >= tailStart; i--) {
    const found = matchLine(cleanedLines[i]);
    if (found) return found;
  }

  // Optional deeper scan for oversized (>8KB) failing logs where errors sit in the middle
  const maxScan = Math.max(40, Number(scanLines || 40));
  if (maxScan > 40 && tailStart > 0) {
    const midStart = Math.max(0, cleanedLines.length - maxScan);
    for (let i = tailStart - 1; i >= midStart; i--) {
      const found = matchLine(cleanedLines[i]);
      if (found) return found;
    }
  }
  return null;
}

export const NON_CODE_EXTENSIONS = new Set([
  ".md",
  ".markdown",
  ".txt",
  ".rst",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".cfg",
  ".lock",
  ".csv",
  ".svg",
  ".html",
  ".css",
]);

export const SYMBOL_STOPWORDS = new Set([
  "for", "if", "else", "elif", "while", "return", "in", "to", "of", "on",
  "from", "with", "by", "as", "is", "at", "be", "or", "and", "not", "the",
  "that", "this", "self", "super", "new", "var", "let", "const", "async",
  "await", "true", "false", "null", "none",
]);

export const DOC_EXTENSIONS = new Set([
  ".md",
  ".markdown",
  ".rst",
  ".txt",
  ".adoc",
  ".pdf",
  ".html",
  ".htm",
]);

const TEST_FILE_PATTERNS = [
  /(?:^|[/\\])(?:tests?|testing|__tests__)(?:[/\\]|$)/i,
  /(?:^|[/\\])test_[^/\\]+\.[a-zA-Z0-9]+$/i,
  /_[a-zA-Z0-9]+_test\.[a-zA-Z0-9]+$/i,
  /[-_.](?:test|spec)\.[a-zA-Z0-9]+$/i,
];

const INLINE_SCRIPT_PATTERNS = [
  /\b(?:python[0-9.]*|pypy[0-9.]*)\s+-c\b/i,
  /\b(?:node|deno|bun)\s+-e\b/i,
  /\b(?:bash|sh|zsh)\s+-c\b/i,
  /\b(?:ruby|perl)\s+-e\b/i,
  /<<-?\s*['"]?(?:EOF|END|STOP)['"]?/i,
];

const META_NON_TASK_PATTERNS = [
  /(?:评审|评价|打分|评分|审查|审计|不留情面)/i,
  /\b(?:review|audit|critique|evaluate|scoring)\b/i,
  /(?:rate\s+(?:the\s+|this\s+)?(?:project|code|repo|codebase)|rating\s+(?:the\s+|this\s+)?project)/i,
  /(?:优化项目|根据反馈优化|优化工程|优化仓库|重构项目)/i,
  /^(?:explain|describe|what is|how does|介绍|总结|讲讲)\b/i,
];

export function isTestFile(filePath) {
  if (!filePath) return false;
  const p = String(filePath).trim();
  return TEST_FILE_PATTERNS.some((pat) => pat.test(p));
}

export function isDocFile(filePath) {
  if (!filePath) return false;
  const clean = String(filePath).trim();
  const ext = path.extname(clean).toLowerCase();
  if (DOC_EXTENSIONS.has(ext)) return true;
  const parts = clean.split(/[/\\]+/);
  return parts.some((part) => ["docs", "doc", "documentation"].includes(part.toLowerCase()));
}

export function isCodeFile(filePath) {
  if (!filePath) return true;
  const base = path.basename(filePath);
  const ext = path.extname(filePath);
  if (base.startsWith(".") && ext === "") return false;
  return !NON_CODE_EXTENSIONS.has(ext.toLowerCase());
}

export function isSourceCodeFile(filePath) {
  if (!filePath) return false;
  if (isDocFile(filePath) || isTestFile(filePath)) return false;
  return isCodeFile(filePath);
}

export function isInlineScriptCommand(cmd) {
  if (!cmd) return false;
  const s = String(cmd).trim();
  if (s.includes("\n")) return true;
  return INLINE_SCRIPT_PATTERNS.some((pat) => pat.test(s));
}

export function isMetaNonTaskIntent(text) {
  if (!text) return false;
  const s = String(text).trim();
  return META_NON_TASK_PATTERNS.some((pat) => pat.test(s));
}

const TRACEBACK_FILE_PATTERNS = [
  /File "([^"]+)", line \d+/g,
  /^\s*([A-Za-z0-9_./\\-]+\.[a-zA-Z0-9_]+):(\d+):(?:\s*(?:in |error|fatal|warning))?/gm,
  /^\s*(?:FAIL|ERROR):\s+[A-Za-z0-9_.]+\s+\(([A-Za-z0-9_./\\-]+\.[a-zA-Z0-9_]+)\)/gm,
];

export function extractTracebackFiles(text, workspaceRoot = "") {
  if (!text) return new Set();
  const found = new Set();
  for (const pat of TRACEBACK_FILE_PATTERNS) {
    pat.lastIndex = 0;
    let m;
    while ((m = pat.exec(text)) !== null) {
      const rawF = m[1].trim();
      if (
        ["<stdin>", "<string>", "site-packages", "/usr/lib", ".local/lib"].some((pfx) =>
          rawF.includes(pfx)
        )
      ) {
        continue;
      }
      const norm = normalizeRelPath(rawF, workspaceRoot);
      if (norm && !norm.startsWith("/")) {
        found.add(norm);
      }
    }
  }
  return found;
}

function dedentText(text) {
  const lines = String(text).split(/\r?\n/);
  let minIndent = Infinity;
  for (const ln of lines) {
    if (!ln.trim()) continue;
    const indent = ln.length - ln.trimStart().length;
    if (indent < minIndent) minIndent = indent;
  }
  if (!Number.isFinite(minIndent) || minIndent === 0) return text;
  return lines.map((ln) => (ln.trim() ? ln.slice(minIndent) : "")).join("\n");
}

export function extractSymbolsFromText(text, filePath = null) {
  if (!text || !isCodeFile(filePath)) return [];
  const symbols = new Set();

  if (filePath && filePath.toLowerCase().endsWith(".py")) {
    for (const candidate of [text, dedentText(text)]) {
      const { definitions } = parsePythonStructure(candidate);
      for (const d of definitions) {
        if (!SYMBOL_STOPWORDS.has(d.leaf.toLowerCase())) {
          symbols.add(d.leaf);
        }
      }
      if (symbols.size > 0) return Array.from(symbols).sort();
    }
  }

  const tsSyms = extractTsSymbolsFromText(text, filePath, SYMBOL_STOPWORDS);
  if (tsSyms.length > 0) return tsSyms;

  const fallbackRe =
    /(?:^|[\s;{}()])(?:def|class|fn|func|function|interface|struct)\s+([A-Za-z_][A-Za-z0-9_]{2,})\s*(?:[(<:{]|$)/gm;
  let m;
  while ((m = fallbackRe.exec(text)) !== null) {
    const cand = m[1];
    if (!SYMBOL_STOPWORDS.has(cand.toLowerCase())) {
      symbols.add(cand);
    }
  }
  return Array.from(symbols).sort();
}

function rangesOverlap(aStart, aEnd, ranges) {
  for (const [rStart, rEnd] of ranges) {
    if (aStart <= rEnd && rStart <= aEnd) return true;
  }
  return false;
}

export function extractPythonSymbolsForLineRanges(content, lineRanges) {
  if (!content || !lineRanges || lineRanges.length === 0) return [];
  const { definitions } = parsePythonStructure(content);
  const found = new Set();

  // Group methods under top-level classes
  const classes = definitions.filter((d) => d.kind === "class" && !d.qualified.includes("."));
  const topFunctions = definitions.filter(
    (d) => d.kind === "function" && !d.qualified.includes(".")
  );

  for (const fn of topFunctions) {
    if (rangesOverlap(fn.startLine, fn.endLine, lineRanges)) {
      found.add(fn.leaf);
    }
  }

  for (const cls of classes) {
    if (!rangesOverlap(cls.startLine, cls.endLine, lineRanges)) continue;
    let methodMatched = false;
    const prefix = `${cls.leaf}.`;
    for (const d of definitions) {
      if (d.kind === "function" && d.qualified.startsWith(prefix)) {
        if (rangesOverlap(d.startLine, d.endLine, lineRanges)) {
          found.add(d.qualified);
          methodMatched = true;
        }
      }
    }
    if (!methodMatched) {
      found.add(cls.leaf);
    }
  }

  return Array.from(found).sort();
}

export function extractPythonLocalImports(content, filePath, candidateFiles) {
  if (!content || !candidateFiles || candidateFiles.size === 0) return new Set();
  const importedModules = new Set();
  const dirPosix = path.posix.dirname(filePath.replace(/\\/g, "/"));
  const curParts = dirPosix === "." ? [] : dirPosix.split("/").filter(Boolean);

  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, "").trim();
    if (!line) continue;

    const mImp = line.match(/^import\s+(.+)$/);
    if (mImp) {
      for (const part of mImp[1].split(",")) {
        const mod = part.trim().split(/\s+as\s+/)[0].trim();
        if (mod) importedModules.add(mod);
      }
      continue;
    }

    const mFrom = line.match(/^from\s+(\.*)([A-Za-z0-9_.]*)\s+import\s+(.+)$/);
    if (mFrom) {
      const level = mFrom[1].length;
      const mod = mFrom[2] || "";
      const namesPart = mFrom[3].replace(/[()]/g, "");
      const names = namesPart
        .split(",")
        .map((s) => s.trim().split(/\s+as\s+/)[0].trim())
        .filter((s) => s && s !== "*");

      if (level > 0) {
        const keepCount = Math.max(0, curParts.length - (level - 1));
        const baseParts = curParts.slice(0, keepCount);
        const fullParts = mod ? [...baseParts, mod] : baseParts;
        const fullMod = fullParts.join(".");
        if (fullMod) importedModules.add(fullMod);
        for (const nm of names) {
          importedModules.add(fullMod ? `${fullMod}.${nm}` : nm);
        }
      } else if (mod) {
        importedModules.add(mod);
        for (const nm of names) {
          importedModules.add(`${mod}.${nm}`);
        }
      }
    }
  }

  const matched = new Set();
  for (const cand of candidateFiles) {
    if (!cand.endsWith(".py")) continue;
    const normCand = cand.replace(/\\/g, "/");
    const parsed = path.posix.parse(normCand);
    const modName =
      parsed.name === "__init__"
        ? parsed.dir.split("/").filter(Boolean).join(".")
        : [...(parsed.dir ? parsed.dir.split("/").filter(Boolean) : []), parsed.name].join(".");
    if (
      modName &&
      Array.from(importedModules).some(
        (m) => m === modName || m.startsWith(`${modName}.`)
      )
    ) {
      matched.add(cand);
    }
  }
  return matched;
}

export function extractSymbolsForLineRanges(content, filePath, lineRanges) {
  if (!content || !lineRanges || lineRanges.length === 0) return [];
  if (filePath && filePath.toLowerCase().endsWith(".py")) {
    return extractPythonSymbolsForLineRanges(content, lineRanges);
  }
  return extractTsSymbolsForLineRanges(content, filePath, lineRanges);
}

function locateSnippetLineRanges(diskContent, snippet) {
  const norm = String(snippet || "").replace(/^[\r\n]+|[\r\n]+$/g, "");
  if (!norm.trim()) return [];

  const isMultiline = norm.includes("\n");
  const countOccurrences = (hay, needle) => {
    if (!needle) return 0;
    let c = 0;
    let pos = 0;
    while ((pos = hay.indexOf(needle, pos)) !== -1) {
      c++;
      pos += needle.length;
    }
    return c;
  };

  if (isMultiline || countOccurrences(diskContent, norm) === 1) {
    const idx = diskContent.indexOf(norm);
    if (idx >= 0) {
      const startLn = diskContent.slice(0, idx).split("\n").length;
      const endLn = startLn + (norm.split("\n").length - 1);
      return [[startLn, endLn]];
    }
  }

  const diskLines = diskContent.split(/\r?\n/);
  const lineToNums = new Map();
  diskLines.forEach((rawLn, idx) => {
    const key = rawLn.trim();
    if (key.length >= 8) {
      if (!lineToNums.has(key)) lineToNums.set(key, []);
      lineToNums.get(key).push(idx + 1);
    }
  });

  const ranges = [];
  for (const rawSnipLn of norm.split(/\r?\n/)) {
    const key = rawSnipLn.trim();
    if (key.length < 8) continue;
    const matches = lineToNums.get(key);
    if (matches && matches.length === 1) {
      ranges.push([matches[0], matches[0]]);
      if (ranges.length >= 8) break;
    }
  }
  return ranges;
}

export function inferModifiedSymbols(
  workspaceRoot,
  filePath,
  { newSnippet = "", lineRanges = null, hunkHeaders = "" } = {}
) {
  if (!filePath || !isCodeFile(filePath)) return [];

  let diskContent = "";
  if (workspaceRoot) {
    try {
      const fullPath = path.join(workspaceRoot, filePath);
      if (existsSync(fullPath) && statSync(fullPath).isFile()) {
        diskContent = readFileSync(fullPath, "utf-8");
      }
    } catch (_err) {
      diskContent = "";
    }
  }

  if (diskContent) {
    let targetRanges = Array.isArray(lineRanges) ? [...lineRanges] : [];
    if (targetRanges.length === 0 && newSnippet) {
      targetRanges = locateSnippetLineRanges(diskContent, newSnippet);
    }
    if (targetRanges.length > 0) {
      const syms = extractSymbolsForLineRanges(diskContent, filePath, targetRanges);
      if (syms.length > 0) return syms;
    }
  }

  if (newSnippet) {
    const syms = extractSymbolsFromText(newSnippet, filePath);
    if (syms.length > 0) return syms;
  }

  if (hunkHeaders) {
    const syms = extractSymbolsFromText(hunkHeaders, filePath);
    if (syms.length > 0) return syms;
  }

  return [];
}

export function extractLocalImports(content, filePath, candidateFiles) {
  if (!content || !candidateFiles || candidateFiles.size === 0 || !filePath) {
    return new Set();
  }
  if (filePath.toLowerCase().endsWith(".py")) {
    return extractPythonLocalImports(content, filePath, candidateFiles);
  }
  return extractTsLocalImports(content, filePath, candidateFiles) || new Set();
}

export class AgentAdapter {
  constructor() {
    this.name = "base";
  }

  parseTranscript(_transcriptPath, _workspaceRoot, _minStepIdx = 0) {
    throw new Error("Not implemented");
  }

  discoverLatestTranscript(_workspaceRoot) {
    throw new Error("Not implemented");
  }
}
