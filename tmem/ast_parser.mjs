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

/**
 * Strip comments and string literals from brace-delimited languages (JS/TS/Go/Rust/Java/C/C++/C#)
 * while preserving line numbers and braces/identifiers outside comments and strings.
 */
function sanitizeBraceSource(content) {
  let out = "";
  let i = 0;
  const n = content.length;
  let state = "code"; // code | line_comment | block_comment | sq | dq | bq

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

  // Compute brace depth at start of each line and matching closing lines for definitions
  const definitions = [];
  const containerStack = []; // { name, braceDepth, defObj }
  let braceDepth = 0;

  // Precompute line brace events
  for (let idx = 0; idx < lines.length; idx++) {
    const lineNum = idx + 1;
    const raw = lines[idx];
    const trimmed = raw.trim();

    // Pop containers whose closing brace occurred before this line
    while (
      containerStack.length > 0 &&
      braceDepth < containerStack[containerStack.length - 1].braceDepth
    ) {
      const top = containerStack.pop();
      top.defObj.endLine = Math.max(top.defObj.startLine, lineNum - 1);
    }

    if (trimmed) {
      let matched = null;

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
        const prefix = containerStack.map((c) => c.name);
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
          const mFn =
            trimmed.match(
              /^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*[<(]/
            ) ||
            trimmed.match(
              /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>/
            ) ||
            trimmed.match(
              /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?function\b/
            );
          if (mFn) {
            fname = mFn[1];
          } else if (containerStack.length > 0) {
            const mMethod = trimmed.match(
              /^(?:public|private|protected|static|async|override|readonly|abstract|\s)*?(?:get\s+|set\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{=]+)?\{?/
            );
            if (
              mMethod &&
              !BRACE_IGNORED_METHOD_NAMES.has(mMethod[1]) &&
              !trimmed.startsWith("return ") &&
              !trimmed.includes("=")
            ) {
              fname = mMethod[1];
            }
          }
        }

        if (fname) {
          const prefix = containerStack.map((c) => c.name);
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
          matched = { type: "function", name: fname, defObj };
        }
      }

      // Update braces for this line
      let opensOnLine = 0;
      let closesOnLine = 0;
      for (const ch of raw) {
        if (ch === "{") opensOnLine++;
        else if (ch === "}") closesOnLine++;
      }

      if (matched && opensOnLine > 0) {
        const targetDepth = braceDepth + 1;
        // Only push if it doesn't close on the same line
        if (braceDepth + opensOnLine - closesOnLine >= targetDepth) {
          containerStack.push({
            name: matched.name,
            braceDepth: targetDepth,
            defObj: matched.defObj,
          });
        } else {
          matched.defObj.endLine = lineNum;
        }
      }

      braceDepth = Math.max(0, braceDepth + opensOnLine - closesOnLine);
      while (
        containerStack.length > 0 &&
        braceDepth < containerStack[containerStack.length - 1].braceDepth
      ) {
        const top = containerStack.pop();
        top.defObj.endLine = lineNum;
      }
    } else {
      // Blank line still checks brace balance (none here)
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
  for (const d of defs) {
    if (d.qualified === target) return true;
    if (!target.includes(".") && d.leaf === target) return true;
  }
  return false;
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
