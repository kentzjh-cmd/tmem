/**
 * Statistical Co-Change Topology Analyzer (L1).
 *
 * Computes recurring file co-change pairs from Git commit history and multi-trace
 * records, filtering out bulk sweeps (>10 files) and metadata/noise files so that
 * only genuine, recurring co-edit contracts surface.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { logger } from "./index.mjs";
import { isCodeFile, isDocFile } from "./adapters/base.mjs";
import { loadTelemetryEvents } from "./telemetry.mjs";

const _gitTxCache = new Map();

function getGitHeadSignature(ws) {
  try {
    const headPath = path.join(ws, ".git", "HEAD");
    if (!existsSync(headPath)) return "";
    const headContent = readFileSync(headPath, "utf-8").trim();
    if (headContent.startsWith("ref: ")) {
      const refRel = headContent.slice(5).trim();
      const refPath = path.join(ws, ".git", refRel);
      if (existsSync(refPath)) {
        return readFileSync(refPath, "utf-8").trim();
      }
      const logsHead = path.join(ws, ".git", "logs", "HEAD");
      if (existsSync(logsHead)) {
        return `${headContent}:${statSync(logsHead).mtimeMs}`;
      }
    }
    return headContent;
  } catch (_err) {
    return "";
  }
}

const IGNORED_PREFIXES = [".tmem/", ".agents/", ".dsh/", ".git/"];
const IGNORED_FILENAMES = new Set([
  "MEMORIES.md",
  "MEM.md",
  "package-lock.json",
  "pnpm-lock.yaml",
  "yarn.lock",
  "Cargo.lock",
  "poetry.lock",
  "uv.lock",
]);

export class CoChangePair {
  constructor({
    file_a,
    file_b,
    count,
    confidence,
    conf_a_to_b = confidence,
    conf_b_to_a = confidence,
  }) {
    this.file_a = file_a;
    this.file_b = file_b;
    this.count = count;
    this.confidence = confidence;
    this.conf_a_to_b = conf_a_to_b;
    this.conf_b_to_a = conf_b_to_a;
  }

  formatLabel() {
    const pct = Math.round(this.confidence * 100);
    return `${this.file_a} <-> ${this.file_b} (co-changed ${this.count}x, ${pct}% conf)`;
  }
}

function isTrackableCochangeFile(pathStr) {
  const p = String(pathStr || "").trim();
  if (!p || IGNORED_PREFIXES.some((pfx) => p.startsWith(pfx))) return false;
  if (IGNORED_FILENAMES.has(path.basename(p))) return false;
  return true;
}

function resolveHistoricalPath(ws, rawPath, renameMap) {
  let cur = rawPath;
  const seen = new Set();
  while (renameMap.has(cur) && !seen.has(cur)) {
    seen.add(cur);
    cur = renameMap.get(cur);
  }
  if (existsSync(path.join(ws, cur))) return cur;
  if (cur.endsWith(".py")) {
    const mjsPath = `${cur.slice(0, -3)}.mjs`;
    if (existsSync(path.join(ws, mjsPath))) return mjsPath;
  }
  return null;
}

function loadGitTransactions(workspaceRoot, maxCommits = 100, maxFilesPerTx = 10) {
  const ws = path.resolve(workspaceRoot);
  if (!existsSync(path.join(ws, ".git"))) {
    return { transactions: [], allFileFreq: new Map(), seenHashes: new Set() };
  }
  const headSig = getGitHeadSignature(ws);
  const cacheKey = `${ws}\0${maxCommits}\0${maxFilesPerTx}`;
  const cached = _gitTxCache.get(cacheKey);
  if (cached && headSig && cached.headSig === headSig) {
    return cached.value;
  }

  let stdout = "";
  try {
    stdout = execFileSync(
      "git",
      ["log", `-n${maxCommits}`, "-M", "--name-status", "--pretty=format:---COMMIT:%h"],
      {
        cwd: ws,
        encoding: "utf-8",
        timeout: 3000,
        stdio: ["ignore", "pipe", "ignore"],
      }
    );
  } catch (err) {
    logger.debug("git log co-change scan skipped:", err?.message || err);
    return { transactions: [], allFileFreq: new Map(), seenHashes: new Set() };
  }

  const rawCommits = [];
  const seenHashes = new Set();
  const renameMap = new Map();
  let currentFiles = [];

  for (const rawLine of stdout.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith("---COMMIT:")) {
      if (currentFiles.length > 0) {
        rawCommits.push(currentFiles);
      }
      currentFiles = [];
      const h = line.slice("---COMMIT:".length).trim();
      if (h) seenHashes.add(h);
    } else if (line) {
      const parts = line.split("\t");
      const status = parts[0] || "";
      if (status.startsWith("R") && parts.length >= 3) {
        renameMap.set(parts[1].trim(), parts[2].trim());
        currentFiles.push(parts[2].trim());
      } else if (!status.startsWith("D")) {
        const filePath = (parts[parts.length - 1] || "").trim();
        if (filePath) currentFiles.push(filePath);
      }
    }
  }
  if (currentFiles.length > 0) {
    rawCommits.push(currentFiles);
  }

  const transactions = [];
  const allFileFreq = new Map();

  for (const commitFiles of rawCommits) {
    const mapped = commitFiles
      .map((f) => resolveHistoricalPath(ws, f, renameMap))
      .filter((f) => f && isTrackableCochangeFile(f));
    const uniq = Array.from(new Set(mapped)).sort();
    if (uniq.length >= 1 && uniq.length <= maxFilesPerTx) {
      for (const f of uniq) {
        allFileFreq.set(f, (allFileFreq.get(f) || 0) + 1);
      }
    }
    if (uniq.length >= 2 && uniq.length <= maxFilesPerTx) {
      transactions.push(uniq);
    }
  }

  const result = { transactions, allFileFreq, seenHashes };
  if (headSig) {
    if (_gitTxCache.size > 32) _gitTxCache.clear();
    _gitTxCache.set(cacheKey, { headSig, value: result });
  }
  return result;
}

export function computeCochangeStats(
  workspaceRoot,
  playbooks = null,
  {
    maxCommits = 100,
    minCount = 2,
    minConfidence = 0.5,
    maxFilesPerTx = 10,
  } = {}
) {
  const { transactions, allFileFreq, seenHashes } = loadGitTransactions(
    workspaceRoot,
    maxCommits,
    maxFilesPerTx
  );

  const fileFreq = new Map(allFileFreq || []);
  const extraTransactions = [];
  if (playbooks) {
    for (const pb of playbooks) {
      if (pb.is_trivial || pb.status === "invalidated") continue;
      if (pb.git_commit && seenHashes.has(pb.git_commit)) continue;
      const files = Array.from(
        new Set((pb.touched_files || []).filter(isTrackableCochangeFile))
      ).sort();
      if (files.length >= 1 && files.length <= maxFilesPerTx) {
        for (const f of files) {
          fileFreq.set(f, (fileFreq.get(f) || 0) + 1);
        }
      }
      if (files.length >= 2 && files.length <= maxFilesPerTx) {
        extraTransactions.push(files);
      }
    }
  }

  const allMultiTransactions = [...transactions, ...extraTransactions];
  if (allMultiTransactions.length === 0) return [];

  const pairFreq = new Map();

  for (const tx of allMultiTransactions) {
    for (let i = 0; i < tx.length; i++) {
      for (let j = i + 1; j < tx.length; j++) {
        const a = tx[i];
        const b = tx[j];
        if (isDocFile(a) && isDocFile(b)) continue;
        if (!(isCodeFile(a) || isCodeFile(b))) continue;
        const key = `${a}\0${b}`;
        pairFreq.set(key, (pairFreq.get(key) || 0) + 1);
      }
    }
  }

  const pairs = [];
  for (const [key, count] of pairFreq.entries()) {
    if (count < minCount) continue;
    const [a, b] = key.split("\0");
    const confAtoB = count / fileFreq.get(a);
    const confBtoA = count / fileFreq.get(b);
    const conf = Math.max(confAtoB, confBtoA);
    if (conf >= minConfidence) {
      pairs.push(
        new CoChangePair({
          file_a: a,
          file_b: b,
          count,
          confidence: Math.round(conf * 1000) / 1000,
          conf_a_to_b: Math.round(confAtoB * 1000) / 1000,
          conf_b_to_a: Math.round(confBtoA * 1000) / 1000,
        })
      );
    }
  }

  pairs.sort((p1, p2) => {
    if (p2.count !== p1.count) return p2.count - p1.count;
    if (p2.confidence !== p1.confidence) return p2.confidence - p1.confidence;
    if (p1.file_a !== p2.file_a) return p1.file_a < p2.file_a ? -1 : 1;
    if (p1.file_b !== p2.file_b) return p1.file_b < p2.file_b ? -1 : 1;
    return 0;
  });
  return pairs;
}

export function findCochangeForFiles(
  workspaceRoot,
  targetFiles,
  playbooks = null,
  limit = 5
) {
  if (!targetFiles || targetFiles.size === 0) return [];
  const pairs = computeCochangeStats(workspaceRoot, playbooks);
  let matched = pairs
    .filter((p) => targetFiles.has(p.file_a) && targetFiles.has(p.file_b))
    .map((p) => p.formatLabel());
  if (matched.length === 0) {
    matched = pairs
      .filter((p) => targetFiles.has(p.file_a) || targetFiles.has(p.file_b))
      .map((p) => p.formatLabel());
  }
  return matched.slice(0, limit);
}

function getSuppressedPartners(workspaceRoot) {
  const events = loadTelemetryEvents(workspaceRoot, 500);
  if (!events || events.length === 0) return new Set();
  const stats = new Map();
  const pendingTargetsBySrc = new Map();
  for (const ev of events) {
    if (ev.event === "l1_cochange_hinted") {
      const src = ev.edited_file || "";
      const targets = ev.suggested_files || [];
      pendingTargetsBySrc.set(src, targets);
      for (const tgt of targets) {
        const k = `${src}->${tgt}`;
        const cur = stats.get(k) || { hinted: 0, adopted: 0, ignored: 0 };
        cur.hinted += 1;
        stats.set(k, cur);
      }
    } else if (ev.event === "l1_outcome") {
      const src = ev.edited_file || "";
      if (ev.adopted) {
        for (const tgt of ev.adopted_files || []) {
          const k = `${src}->${tgt}`;
          const cur = stats.get(k) || { hinted: 0, adopted: 0, ignored: 0 };
          cur.adopted += 1;
          stats.set(k, cur);
        }
      } else {
        for (const tgt of pendingTargetsBySrc.get(src) || []) {
          const k = `${src}->${tgt}`;
          const cur = stats.get(k) || { hinted: 0, adopted: 0, ignored: 0 };
          cur.ignored += 1;
          stats.set(k, cur);
        }
      }
    }
  }
  const suppressed = new Set();
  for (const [k, cur] of stats.entries()) {
    if (cur.ignored >= 3 && cur.adopted === 0) {
      suppressed.add(k);
    }
  }
  return suppressed;
}

export function findCochangePartnersForFiles(
  workspaceRoot,
  editedFiles,
  playbooks = null,
  limit = 3,
  minDirectionalConf = 0.5
) {
  const editedSet = new Set(Array.from(editedFiles || []).filter(Boolean));
  if (editedSet.size === 0) return [];
  const ws = path.resolve(workspaceRoot);
  const pairs = computeCochangeStats(workspaceRoot, playbooks);
  const suppressed = getSuppressedPartners(ws);
  const out = [];
  const seenPartners = new Set();

  for (const p of pairs) {
    let partner;
    let src;
    let dirConf;
    if (editedSet.has(p.file_a) && !editedSet.has(p.file_b)) {
      partner = p.file_b;
      src = p.file_a;
      dirConf = p.conf_a_to_b ?? p.confidence;
    } else if (editedSet.has(p.file_b) && !editedSet.has(p.file_a)) {
      partner = p.file_a;
      src = p.file_b;
      dirConf = p.conf_b_to_a ?? p.confidence;
    } else {
      continue;
    }
    if (dirConf < minDirectionalConf) continue;
    if (suppressed.has(`${src}->${partner}`)) continue;
    if (seenPartners.has(partner)) continue;
    if (!existsSync(path.join(ws, partner))) continue;
    seenPartners.add(partner);
    out.push([src, partner, p.count, dirConf]);
    if (out.length >= limit) break;
  }
  return out;
}
