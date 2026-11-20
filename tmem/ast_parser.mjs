/**
 * Pure Node.js Multi-Language Structural Symbol & Import Parser for tmem.
 *
 * Zero external dependencies:
 * - Python (.py/.pyw): indentation + triple-quoted string/comment-aware parser
 *   extracting classes, methods (`Class.method`), functions, top-level/class assignments,
 *   exact `[startLine, endLine]` intervals, and static `import`/`from ... import` dependencies.
 * - JS/TS/TSX (.js/.jsx/.mjs/.cjs/.ts/.tsx): comment/string/brace-aware structural parser
 *   extracting classes, methods, functions, arrow/function expressions, interfaces/types/enums,
 *   line ranges, and static `import`/`export`/`require` local imports.
 * - Go/Rust/Java/C/C++/Ruby/PHP/C#: brace/keyword structural definition extraction.
 */

import path from "node:path";

export const EXT_TO_LANG = Object.freeze({
  ".py": "python",
  ".pyw": "python",
  ".js": "javascript",
  ".jsx": "javascript",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".go": "go",
  ".rs": "rust",
  ".java": "java",
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".cc": "cpp",
  ".cxx": "cpp",
  ".hpp": "cpp",
  ".hh": "cpp",
  ".rb": "ruby",
  ".php": "php",
  ".cs": "c_sharp",
});

const JS_TS_RESOLVE_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"];

export function detectLanguage(filePath) {
  if (!filePath) return null;
  const ext = path.extname(String(filePath)).toLowerCase();
  return EXT_TO_LANG[ext] || null;
}

function rangesOverlap(aStart, aEnd, ranges) {
  for (const [rStart, rEnd] of ranges) {
    if (aStart <= rEnd && rStart <= aEnd) return true;
  }
  return false;
}

/**
 * Mask string literals and comments in Python source while preserving line structure
 * and keeping identifier/definition headers intact outside strings & comments.
 */
function sanitizePythonLines(content) {
  const lines = content.split(/\r?\n/);
  const sanitized = [];
  let inTriple = null; // `"""` or `'''`

  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];
    let out = "";
    let i = 0;
    let inQuote = null;

    while (i < line.length) {
      if (inTriple) {
        if (line[i] === "\\") {
          out += "  ";
          i += 2;
          continue;
        }
        if (line.startsWith(inTriple, i)) {
          out += "   ";
          i += 3;
          inTriple = null;
        } else {
          out += " ";
          i += 1;
        }
        continue;
      }

      if (inQuote) {
        if (line[i] === "\\") {
          out += "  ";
          i += 2;
          continue;
        }
        if (line[i] === inQuote) {
          out += " ";
          inQuote = null;
        } else {
          out += " ";
        }
        i += 1;
        continue;
      }

      if (line[i] === "#") {
        // Rest of line is comment
        break;
      }

      if (line.startsWith('"""', i) || line.startsWith("'''", i)) {
        inTriple = line.slice(i, i + 3);
        out += "   ";
        i += 3;
        continue;
      }

      if (line[i] === '"' || line[i] === "'") {
        inQuote = line[i];
        out += " ";
        i += 1;
        continue;
      }

      out += line[i];
      i += 1;
    }
    sanitized.push(out);
  }
  return sanitized;
}

function countParenDelta(line) {
  let delta = 0;
  for (const ch of line) {
    if (ch === "(" || ch === "[" || ch === "{") delta++;
    else if (ch === ")" || ch === "]" || ch === "}") delta--;
  }
  return delta;
}

/**
 * Parse Python definitions (classes, methods, functions, assignments) with 1-indexed line ranges.
 * Returns { definitions: [{ qualified, leaf, kind, startLine, endLine, indent }], assignments: [{ qualified, name, line }] }
 */
export function parsePythonStructure(content) {
  if (!content) return { definitions: [], assignments: [] };
  const cleanLines = sanitizePythonLines(content);
  const definitions = [];
  const assignments = [];
  const stack = []; // { name, kind, indent, startLine, defObj }
  let parenDepth = 0;

  const closeBlocksAbove = (indent, endLine) => {
    while (stack.length > 0 && indent <= stack[stack.length - 1].indent) {
      const top = stack.pop();
      top.defObj.endLine = Math.max(top.startLine, endLine);
    }
  };

  for (let idx = 0; idx < cleanLines.length; idx++) {
    const lineNum = idx + 1;
    const raw = cleanLines[idx];
    const trimmed = raw.trim();
    const prevParen = parenDepth;
    parenDepth = Math.max(0, parenDepth + countParenDelta(raw));

    if (!trimmed || prevParen > 0) {
      continue;
    }

    const indent = raw.length - raw.trimStart().length;
    closeBlocksAbove(indent, lineNum - 1);

    const clsMatch = trimmed.match(/^class\s+([A-Za-z_][A-Za-z0-9_]*)\b/);
    if (clsMatch) {
      const name = clsMatch[1];
      const containerNames = stack.filter((s) => s.kind === "class").map((s) => s.name);
      const qualified = containerNames.length ? `${containerNames.join(".")}.${name}` : name;
      const defObj = {
        qualified,
        leaf: name,
        kind: "class",
        startLine: lineNum,
        endLine: lineNum,
        indent,
      };
      definitions.push(defObj);
      stack.push({ name, kind: "class", indent, startLine: lineNum, defObj });
      continue;
    }

    const fnMatch = trimmed.match(/^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/);
    if (fnMatch) {
      const name = fnMatch[1];
      const containerNames = stack.map((s) => s.name);
      const qualified = containerNames.length ? `${containerNames.join(".")}.${name}` : name;
      const defObj = {
        qualified,
        leaf: name,
        kind: "function",
        startLine: lineNum,
        endLine: lineNum,
        indent,
      };
      definitions.push(defObj);
      stack.push({ name, kind: "function", indent, startLine: lineNum, defObj });
      continue;
    }

    // Track module-level or class-body assignments (matching Python ast.Assign / ast.AnnAssign)
    const inFunction = stack.some((s) => s.kind === "function");
    if (!inFunction) {
      const assignMatch = trimmed.match(
        /^([A-Za-z_][A-Za-z0-9_]*)(?:\s*:\s*[^=]+)?\s*=(?!=)/
      );
      if (assignMatch) {
        const name = assignMatch[1];
        const containerNames = stack.filter((s) => s.kind === "class").map((s) => s.name);
        const qualified = containerNames.length ? `${containerNames.join(".")}.${name}` : name;
        assignments.push({ qualified, name, line: lineNum });
      }
    }
  }

  // Close remaining open blocks at last non-empty line
  let lastCodeLine = cleanLines.length;
  while (lastCodeLine > 1 && !cleanLines[lastCodeLine - 1].trim()) {
    lastCodeLine--;
  }
  closeBlocksAbove(-1, lastCodeLine);

  return { definitions, assignments };
}

const REGEX_PREV_CHARS = new Set([
  "",
  "(",
  "[",
  "{",
  ",",
  ";",
  ":",
  "=",
  "!",
  "&",
  "|",
  "?",
  "~",
  "+",
  "-",
  "*",
  "%",
  "^",
  "<",
  ">",
  "\n",
]);

const REGEX_PREV_WORDS = new Set([
  "return",
  "case",
  "throw",
  "typeof",
  "void",
  "delete",
  "in",
  "of",
  "instanceof",
  "new",
  "yield",
  "await",
]);

/**
 * Strip comments, string literals, and regex literals from brace-delimited languages
 * (JS/TS/Go/Rust/Java/C/C++/C#) while preserving line numbers and braces/identifiers
 * outside comments, strings, and regexes.
 */
function sanitizeBraceSource(content) {
  let out = "";
  let i = 0;
  const n = content.length;
  let state = "code"; // code | line_comment | block_comment | sq | dq | bq | regex
  let regexInClass = false;
  let lastSigChar = "";
  let lastWord = "";

  const canStartRegex = () =>
    REGEX_PREV_WORDS.has(lastWord) || REGEX_PREV_CHARS.has(lastSigChar);

  while (i < n) {
    const ch = content[i];
    const next = i + 1 < n ? content[i + 1] : "";

    if (state === "line_comment") {
      if (ch === "\n") {
        out += "\n";
        state = "code";
      } else {
        out += " ";
      }
      i++;
      continue;
    }

    if (state === "block_comment") {
      if (ch === "*" && next === "/") {
        out += "  ";
        i += 2;
        state = "code";
      } else {
        out += ch === "\n" ? "\n" : " ";
        i++;
      }
      continue;
    }

    if (state === "regex") {
      if (ch === "\\") {
        out += " ";
        if (next) {
          out += next === "\n" ? "\n" : " ";
          i += 2;
        } else {
          i++;
        }
        continue;
      }
      if (ch === "\n") {
        out += "\n";
        state = "code";
        regexInClass = false;
        i++;
        continue;
      }
      if (ch === "[") {
        regexInClass = true;
      } else if (ch === "]") {
        regexInClass = false;
      } else if (ch === "/" && !regexInClass) {
        out += " ";
        i++;
        while (i < n && /[A-Za-z]/.test(content[i])) {
          out += " ";
          i++;
        }
        state = "code";
        lastSigChar = "/";
        lastWord = "";
        continue;
      }
      out += " ";
      i++;
      continue;
    }

    if (state === "sq" || state === "dq" || state === "bq") {
      const quote = state === "sq" ? "'" : state === "dq" ? '"' : "`";
      if (ch === "\\") {
        out += ch === "\n" ? "\n" : " ";
        if (next) {
          out += next === "\n" ? "\n" : " ";
          i += 2;
        } else {
          i++;
        }
        continue;
      }
      if (ch === quote) {
        out += " ";
        state = "code";
        lastSigChar = quote;
        lastWord = "";
      } else {
        out += ch === "\n" ? "\n" : " ";
      }
      i++;
      continue;
    }

    // state === "code"
    if (ch === "/" && next === "/") {
      out += "  ";
      i += 2;
      state = "line_comment";
      continue;
    }
    if (ch === "/" && next === "*") {
      out += "  ";
      i += 2;
      state = "block_comment";
      continue;
    }
    if (ch === "/" && canStartRegex()) {
      out += " ";
      i++;
      state = "regex";
      regexInClass = false;
      continue;
    }
    if (ch === "'") {
      out += " ";
      state = "sq";
      i++;
      continue;
    }
    if (ch === '"') {
      out += " ";
      state = "dq";
      i++;
      continue;
    }
    if (ch === "`") {
      out += " ";
      state = "bq";
      i++;
      continue;
    }
    out += ch;
    if (/[A-Za-z0-9_$]/.test(ch)) {
      if (!/[A-Za-z0-9_$]/.test(lastSigChar)) {
        lastWord = ch;
      } else {
        lastWord += ch;
      }
      lastSigChar = ch;
    } else if (!/\s/.test(ch)) {
      lastSigChar = ch;
      lastWord = "";
    }
    i++;
  }
  return out;
}

const BRACE_IGNORED_METHOD_NAMES = new Set([
  "if",
  "for",
  "while",
  "switch",
  "catch",
  "else",
  "return",
  "throw",
  "new",
  "typeof",
  "await",
  "yield",
  "super",
  "this",
]);

/**
 * Parse brace-delimited language definitions with 1-indexed [startLine, endLine] intervals.
 */
export function parseBraceStructure(content, lang) {
  if (!content) return [];
  const clean = sanitizeBraceSource(content);
  const lines = clean.split(/\r?\n/);

  const definitions = [];
  const containerStack = []; // { name, type, braceDepth, defObj }
  let braceDepth = 0;
  let parenDepth = 0;
  let pendingDef = null; // { type, name, defObj }

  for (let idx = 0; idx < lines.length; idx++) {
    const lineNum = idx + 1;
    const raw = lines[idx];
    const trimmed = raw.trim();

    if (trimmed && !pendingDef && parenDepth === 0) {
      let matched = null;
      let isSingleExprArrow = false;

      // 1. Class / struct / interface / enum / trait / impl
      let mContainer = null;
      if (lang === "go") {
        mContainer = trimmed.match(/^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b/);
      } else if (lang === "rust") {
        mContainer =
          trimmed.match(/^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b/) ||
          trimmed.match(/^impl(?:<[^>]+>)?\s+(?:[A-Za-z0-9_:<>]+\s+for\s+)?([A-Za-z_][A-Za-z0-9_]*)\b/);
      } else {
        mContainer = trimmed.match(
          /^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?(?:class|interface|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b/
        );
      }

      if (mContainer) {
        const cname = mContainer[1];
        const prefix = containerStack
          .filter((c) => c.type === "container")
          .map((c) => c.name);
        const qualified = prefix.length ? `${prefix.join(".")}.${cname}` : cname;
        const defObj = {
          qualified,
          leaf: cname,
          kind: "container",
          startLine: lineNum,
          endLine: lineNum,
          openDepth: braceDepth + 1,
        };
        definitions.push(defObj);
        matched = { type: "container", name: cname, defObj };
      }

      // 2. Functions / arrow functions / Go funcs / Rust fns / class methods
      if (!matched) {
        let fname = null;
        if (lang === "go") {
          const mGo = trimmed.match(/^func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(/);
          if (mGo) fname = mGo[1];
        } else if (lang === "rust") {
          const mRs = trimmed.match(
            /^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]/
          );
          if (mRs) fname = mRs[1];
        } else if (lang === "c" || lang === "cpp") {
          const mC = trimmed.match(
            /^(?:static\s+|inline\s+|virtual\s+|explicit\s+|constexpr\s+)*(?:[A-Za-z_][A-Za-z0-9_:<>*&\s]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*$/
          );
          if (mC && !BRACE_IGNORED_METHOD_NAMES.has(mC[1])) fname = mC[1];
        } else {
          const mArrow = trimmed.match(
            /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>/
          );
          const mFn =
            trimmed.match(
              /^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*[<(]/
            ) ||
            mArrow ||
            trimmed.match(
              /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?function\b/
            );
          if (mFn) {
            fname = mFn[1];
            if (mArrow && /=>\s*(?!\{)\S/.test(trimmed)) {
              isSingleExprArrow = true;
            }
          } else if (
            containerStack.length > 0 &&
            containerStack[containerStack.length - 1].type === "container" &&
            braceDepth === containerStack[containerStack.length - 1].braceDepth
          ) {
            const mMethod = trimmed.match(
              /^(?:public|private|protected|static|async|override|readonly|abstract|\s)*?(?:get\s+|set\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]*>)?\s*\(/
            );
            if (
              mMethod &&
              !BRACE_IGNORED_METHOD_NAMES.has(mMethod[1]) &&
              !trimmed.startsWith("return ")
            ) {
              fname = mMethod[1];
            }
          }
        }

        if (fname) {
          const topIsContainer =
            containerStack.length > 0 &&
            containerStack[containerStack.length - 1].type === "container" &&
            braceDepth === containerStack[containerStack.length - 1].braceDepth;
          const prefix = topIsContainer
            ? containerStack.filter((c) => c.type === "container").map((c) => c.name)
            : [];
          const qualified = prefix.length ? `${prefix.join(".")}.${fname}` : fname;
          const defObj = {
            qualified,
            leaf: fname,
            kind: "function",
            startLine: lineNum,
            endLine: lineNum,
            openDepth: braceDepth + 1,
          };
          definitions.push(defObj);
          if (!isSingleExprArrow) {
            matched = { type: "function", name: fname, defObj };
          }
        }
      }

      if (matched) {
        pendingDef = matched;
      }
    }

    for (let cIdx = 0; cIdx < raw.length; cIdx++) {
      const ch = raw[cIdx];
      if (ch === "(") {
        parenDepth++;
      } else if (ch === ")") {
        parenDepth = Math.max(0, parenDepth - 1);
      } else if (ch === "{") {
        braceDepth++;
        if (pendingDef && parenDepth === 0) {
          containerStack.push({
            name: pendingDef.name,
            type: pendingDef.type,
            braceDepth,
            defObj: pendingDef.defObj,
          });
          pendingDef = null;
        }
      } else if (ch === "}") {
        braceDepth = Math.max(0, braceDepth - 1);
        while (
          containerStack.length > 0 &&
          braceDepth < containerStack[containerStack.length - 1].braceDepth
        ) {
          const top = containerStack.pop();
          top.defObj.endLine = lineNum;
        }
      } else if (ch === ";" && parenDepth === 0 && pendingDef) {
        pendingDef.defObj.endLine = lineNum;
        pendingDef = null;
      }
    }
  }

  while (containerStack.length > 0) {
    const top = containerStack.pop();
    top.defObj.endLine = Math.max(top.defObj.startLine, lines.length);
  }

  return definitions;
}

export function extractTsSymbolsFromText(text, filePath, stopwords = null) {
  const lang = detectLanguage(filePath);
  if (!lang || !text) return [];
  const stop = stopwords || new Set();
  const defs =
    lang === "python" ? parsePythonStructure(text).definitions : parseBraceStructure(text, lang);
  const found = new Set();
  for (const d of defs) {
    if (d.leaf && !stop.has(d.leaf.toLowerCase()) && d.leaf.length >= 2) {
      found.add(d.leaf);
    }
  }
  return Array.from(found).sort();
}

export function extractTsSymbolsForLineRanges(content, filePath, lineRanges) {
  if (!content || !lineRanges || lineRanges.length === 0) return [];
  const lang = detectLanguage(filePath);
  if (!lang || lang === "python") return [];

  const defs = parseBraceStructure(content, lang);
  const matched = [];
  const containersWithMatchedChildren = new Set();
  const allHits = [];

  for (const d of defs) {
    if (rangesOverlap(d.startLine, d.endLine, lineRanges)) {
      const isNested = d.qualified.includes(".");
      if (isNested) {
        const lastDot = d.qualified.lastIndexOf(".");
        containersWithMatchedChildren.add(d.qualified.slice(0, lastDot));
      }
      allHits.push([d.qualified, isNested]);
    }
  }

  for (const [qualified, isNested] of allHits) {
    if (!isNested && containersWithMatchedChildren.has(qualified)) continue;
    if (!matched.includes(qualified)) matched.push(qualified);
  }
  return matched.sort();
}

export function checkTsSymbolPresent(content, filePath, expectedSymbol) {
  const lang = detectLanguage(filePath);
  if (!lang || !content) return null;
  const target = (expectedSymbol || "").trim();
  if (!target) return false;

  if (lang === "python") {
    const { definitions, assignments } = parsePythonStructure(content);
    for (const d of definitions) {
      if (d.qualified === target) return true;
      if (!target.includes(".") && d.leaf === target) return true;
    }
    for (const a of assignments) {
      if (a.qualified === target) return true;
      if (!target.includes(".") && a.name === target) return true;
    }
    return false;
  }

  const defs = parseBraceStructure(content, lang);
  return defs.some((d) => d.qualified === target || (!target.includes(".") && d.leaf === target));
}

function splitTopLevelCommas(paramStr) {
  const parts = [];
  let buf = "";
  let depthParen = 0;
  let depthBracket = 0;
  let depthBrace = 0;
  let depthAngle = 0;

  for (let i = 0; i < paramStr.length; i++) {
    const ch = paramStr[i];
    if (ch === "(") depthParen++;
    else if (ch === ")") depthParen = Math.max(0, depthParen - 1);
    else if (ch === "[") depthBracket++;
    else if (ch === "]") depthBracket = Math.max(0, depthBracket - 1);
    else if (ch === "{") depthBrace++;
    else if (ch === "}") depthBrace = Math.max(0, depthBrace - 1);
    else if (ch === "<") depthAngle++;
    else if (ch === ">") depthAngle = Math.max(0, depthAngle - 1);

    if (
      ch === "," &&
      depthParen === 0 &&
      depthBracket === 0 &&
      depthBrace === 0 &&
      depthAngle === 0
    ) {
      if (buf.trim()) parts.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts;
}

function stripTopLevelSuffix(param, sepChar) {
  let depthParen = 0;
  let depthBracket = 0;
  let depthBrace = 0;
  let depthAngle = 0;
  for (let i = 0; i < param.length; i++) {
    const ch = param[i];
    if (ch === "(") depthParen++;
    else if (ch === ")") depthParen = Math.max(0, depthParen - 1);
    else if (ch === "[") depthBracket++;
    else if (ch === "]") depthBracket = Math.max(0, depthBracket - 1);
    else if (ch === "{") depthBrace++;
    else if (ch === "}") depthBrace = Math.max(0, depthBrace - 1);
    else if (ch === "<") depthAngle++;
    else if (ch === ">") depthAngle = Math.max(0, depthAngle - 1);
    else if (
      ch === sepChar &&
      depthParen === 0 &&
      depthBracket === 0 &&
      depthBrace === 0 &&
      depthAngle === 0
    ) {
      if (sepChar === ":" && param[i + 1] === ":") {
        i++;
        continue;
      }
      return param.slice(0, i).trim();
    }
  }
  return param.trim();
}

function extractParenBody(text, startIdx = 0) {
  const openIdx = text.indexOf("(", startIdx);
  if (openIdx < 0) return null;
  let depth = 0;
  for (let i = openIdx; i < text.length; i++) {
    if (text[i] === "(") depth++;
    else if (text[i] === ")") {
      depth--;
      if (depth === 0) {
        return text.slice(openIdx + 1, i);
      }
    }
  }
  return null;
}

function normalizePythonParamList(rawInside) {
  const rawParams = splitTopLevelCommas(rawInside);
  const out = [];
  for (let idx = 0; idx < rawParams.length; idx++) {
    let p = rawParams[idx].trim();
    if (!p || p === "/" || p === "*") continue;
    p = stripTopLevelSuffix(p, "=");
    p = stripTopLevelSuffix(p, ":");
    p = p.replace(/\s+/g, "");
    if (idx === 0 && (p === "self" || p === "cls")) continue;
    if (p) out.push(p);
  }
  return out;
}

function normalizeBraceParamList(rawInside, lang) {
  const rawParams = splitTopLevelCommas(rawInside);
  const out = [];
  for (let idx = 0; idx < rawParams.length; idx++) {
    let p = rawParams[idx].trim();
    if (!p) continue;
    if (lang === "rust" && idx === 0 && /^&?(?:mut\s+)?self$/.test(p)) continue;
    p = stripTopLevelSuffix(p, "=");
    p = stripTopLevelSuffix(p, ":");
    p = p
      .replace(/^(?:public|private|protected|readonly|mut|ref|in|out|final)\s+/g, "")
      .trim();
    if (p.startsWith("{")) {
      out.push("{}");
      continue;
    }
    if (p.startsWith("[")) {
      out.push("[]");
      continue;
    }
    const isRest = p.startsWith("...");
    if (lang === "go" || lang === "c" || lang === "cpp" || lang === "java" || lang === "c_sharp") {
      const toks = p.split(/\s+/).filter(Boolean);
      const ident = (lang === "go" ? toks[0] : toks[toks.length - 1] || "")
        .replace(/^[&*]+|[?]+$/g, "");
      if (ident) out.push(isRest ? `...${ident}` : ident);
      continue;
    }
    const cleanIdent = p.replace(/\?$/, "").replace(/\s+/g, "");
    if (cleanIdent) out.push(cleanIdent);
  }
  return out;
}

/**
 * Extract canonical normalized parameter signature `fnName(p1,p2,...)` for a function/method symbol.
 * Returns null if the symbol is not found or is not a callable function/method.
 */
export function extractSymbolSignature(content, filePath, symbolName) {
  const lang = detectLanguage(filePath);
  if (!lang || !content || !symbolName) return null;
  const target = String(symbolName).trim();
  if (!target) return null;

  if (lang === "python") {
    const cleanLines = sanitizePythonLines(content);
    const { definitions } = parsePythonStructure(content);
    const def = definitions.find(
      (d) =>
        d.kind === "function" &&
        (d.qualified === target || (!target.includes(".") && d.leaf === target))
    );
    if (!def) return null;
    const windowText = cleanLines
      .slice(def.startLine - 1, Math.min(cleanLines.length, def.startLine + 8))
      .join(" ");
    const inside = extractParenBody(windowText, 0);
    if (inside === null) return null;
    const params = normalizePythonParamList(inside);
    return `${def.leaf}(${params.join(",")})`;
  }

  const clean = sanitizeBraceSource(content);
  const lines = clean.split(/\r?\n/);
  const defs = parseBraceStructure(content, lang);
  const def = defs.find(
    (d) =>
      d.kind === "function" &&
      (d.qualified === target || (!target.includes(".") && d.leaf === target))
  );
  if (!def) return null;

  const windowText = lines
    .slice(def.startLine - 1, Math.min(lines.length, def.startLine + 8))
    .join(" ");
  let searchFrom = 0;
  const leafIdx = windowText.indexOf(def.leaf);
  if (leafIdx >= 0) {
    searchFrom = leafIdx + def.leaf.length;
  }
  // Skip generic `<...>` between function name and `(`
  while (searchFrom < windowText.length && /\s/.test(windowText[searchFrom])) searchFrom++;
  if (windowText[searchFrom] === "<") {
    let angleDepth = 0;
    while (searchFrom < windowText.length) {
      if (windowText[searchFrom] === "<") angleDepth++;
      else if (windowText[searchFrom] === ">") {
        angleDepth--;
        if (angleDepth === 0) {
          searchFrom++;
          break;
        }
      }
      searchFrom++;
    }
  }
  // Handle single-identifier arrow function `const fn = x => ...`
  const afterEq = windowText.slice(searchFrom).trimStart();
  if (afterEq.startsWith("=")) {
    const mSingleArrow = afterEq.match(/^=\s*(?:async\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=>/);
    if (mSingleArrow) {
      return `${def.leaf}(${mSingleArrow[1]})`;
    }
  }
  const inside = extractParenBody(windowText, searchFrom);
  if (inside === null) return null;
  const params = normalizeBraceParamList(inside, lang);
  return `${def.leaf}(${params.join(",")})`;
}

/**
 * Verify whether a function/method in `content` still matches `expectedSpec`
 * (formatted either as `"qualifiedSymbol:leaf(p1,p2)"` or `"leaf(p1,p2)"`).
 */
export function checkSymbolSignatureMatch(content, filePath, expectedSpec) {
  if (!content || !filePath || !expectedSpec) return false;
  const raw = String(expectedSpec).trim();
  let symbolName = raw;
  let expectedSig = raw;
  const colonIdx = raw.indexOf(":");
  if (colonIdx > 0 && raw.indexOf("(") > colonIdx) {
    symbolName = raw.slice(0, colonIdx).trim();
    expectedSig = raw.slice(colonIdx + 1).trim();
  } else {
    const parenIdx = raw.indexOf("(");
    if (parenIdx > 0) {
      symbolName = raw.slice(0, parenIdx).trim();
    }
  }
  const actualSig = extractSymbolSignature(content, filePath, symbolName);
  if (!actualSig) return false;
  return actualSig === expectedSig;
}

export function extractTsLocalImports(content, filePath, candidateFiles) {
  const lang = detectLanguage(filePath);
  if (!lang || !content) return null;

  const rawSpecs = new Set();
  if (lang === "javascript" || lang === "typescript" || lang === "tsx") {
    const importRe =
      /(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]|(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
    let m;
    while ((m = importRe.exec(content)) !== null) {
      const spec = (m[1] || m[2] || "").trim();
      if (spec) rawSpecs.add(spec);
    }
  } else if (lang === "go") {
    const goRe = /import\s+(?:\(\s*([\s\S]*?)\s*\)|"([^"]+)")/g;
    let m;
    while ((m = goRe.exec(content)) !== null) {
      if (m[2]) rawSpecs.add(m[2].trim());
      if (m[1]) {
        for (const qm of m[1].matchAll(/"([^"]+)"/g)) {
          rawSpecs.add(qm[1].trim());
        }
      }
    }
  } else if (lang === "rust") {
    const rsRe = /^\s*(?:pub\s+)?use\s+([^;]+);/gm;
    let m;
    while ((m = rsRe.exec(content)) !== null) {
      for (const part of m[1].matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) {
        const tok = part[0];
        if (!["use", "crate", "self", "super", "as", "pub"].includes(tok)) {
          rawSpecs.add(tok);
        }
      }
    }
  } else {
    return new Set();
  }

  if (rawSpecs.size === 0) return new Set();

  const matched = new Set();
  const curDir = path.posix.dirname(filePath.replace(/\\/g, "/"));

  if (lang === "javascript" || lang === "typescript" || lang === "tsx") {
    for (const spec of rawSpecs) {
      if (!spec.startsWith(".")) continue;
      const resolvedBase = path.posix.normalize(path.posix.join(curDir, spec));
      const candidatesToTry = [resolvedBase];
      for (const ext of JS_TS_RESOLVE_EXTS) {
        candidatesToTry.push(`${resolvedBase}${ext}`);
        candidatesToTry.push(path.posix.join(resolvedBase, `index${ext}`));
      }
      const cleanCands = new Set(candidatesToTry.map((c) => c.replace(/^\.\//, "")));
      for (const cand of candidateFiles) {
        if (cleanCands.has(cand.replace(/^\.\//, ""))) {
          matched.add(cand);
        }
      }
    }
  } else {
    for (const cand of candidateFiles) {
      const parsed = path.posix.parse(cand.replace(/\\/g, "/"));
      const stem = parsed.name;
      const withoutExt = parsed.dir ? `${parsed.dir}/${parsed.name}` : parsed.name;
      for (const spec of rawSpecs) {
        if (spec === stem || withoutExt.endsWith(spec)) {
          matched.add(cand);
          break;
        }
      }
    }
  }

  return matched;
}
