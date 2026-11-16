/**
 * Statistical Co-Change Topology Analyzer (L1).
 *
 * Computes recurring file co-change pairs from Git commit history and multi-trace
 * records, filtering out bulk sweeps (>10 files) and metadata/noise files so that
 * only genuine, recurring co-edit contracts surface.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { logger } from "./index.mjs";
import { isCodeFile, isDocFile } from "./adapters/base.mjs";

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
  constructor({ file_a, file_b, count, confidence }) {
    this.file_a = file_a;
    this.file_b = file_b;
    this.count = count;
    this.confidence = confidence;
  }

  formatLabel() {
    const pct = Math.round(this.confidence * 100);
    return `${this.file_a} <-> ${this.file_b} (co-changed ${this.count}x, ${pct}% conf)`;
  }
}

function isTrackableCochangeFile(pathStr) {
  const p = String(pathStr || "").trim();
  if (!p || IGNORED_PREFIXES.some((pfx) => p.startsWith(pfx))) return false;
  if (IGNORED_FILENAMES.has(path.basename(p)) || isDocFile(p)) return false;
  return true;
}

function loadGitTransactions(workspaceRoot, maxCommits = 100, maxFilesPerTx = 10) {
  const ws = path.resolve(workspaceRoot);
  if (!existsSync(path.join(ws, ".git"))) {
    return { transactions: [], seenHashes: new Set() };
  }
  let stdout = "";
  try {
    stdout = execFileSync(
      "git",
      ["log", `-n${maxCommits}`, "--name-only", "--pretty=format:---COMMIT:%h"],
      {
        cwd: ws,
        encoding: "utf-8",
        timeout: 3000,
        stdio: ["ignore", "pipe", "ignore"],
      }
    );
  } catch (err) {
    logger.debug("git log co-change scan skipped:", err?.message || err);
    return { transactions: [], seenHashes: new Set() };
  }

  const transactions = [];
  const seenHashes = new Set();
  let currentFiles = [];

  for (const rawLine of stdout.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith("---COMMIT:")) {
      if (currentFiles.length >= 2 && currentFiles.length <= maxFilesPerTx) {
        transactions.push(Array.from(new Set(currentFiles)).sort());
      }
      currentFiles = [];
      const h = line.slice("---COMMIT:".length).trim();
      if (h) seenHashes.add(h);
    } else if (
      line &&
      isTrackableCochangeFile(line) &&
      existsSync(path.join(ws, line))
    ) {
      currentFiles.push(line);
    }
  }

  if (currentFiles.length >= 2 && currentFiles.length <= maxFilesPerTx) {
    transactions.push(Array.from(new Set(currentFiles)).sort());
  }

  return { transactions, seenHashes };
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
  const { transactions, seenHashes } = loadGitTransactions(
    workspaceRoot,
    maxCommits,
    maxFilesPerTx
  );

  if (playbooks) {
    for (const pb of playbooks) {
      if (pb.is_trivial || pb.status === "invalidated") continue;
      if (pb.git_commit && seenHashes.has(pb.git_commit)) continue;
      const files = Array.from(
        new Set((pb.touched_files || []).filter(isTrackableCochangeFile))
      ).sort();
      if (files.length >= 2 && files.length <= maxFilesPerTx) {
        transactions.push(files);
      }
    }
  }

  if (transactions.length === 0) return [];

  const fileFreq = new Map();
  const pairFreq = new Map();

  for (const tx of transactions) {
    for (const f of tx) {
      fileFreq.set(f, (fileFreq.get(f) || 0) + 1);
    }
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
    const conf = Math.max(count / fileFreq.get(a), count / fileFreq.get(b));
    if (conf >= minConfidence) {
      pairs.push(
        new CoChangePair({
          file_a: a,
          file_b: b,
          count,
          confidence: Math.round(conf * 1000) / 1000,
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

export function findCochangePartnersForFiles(
  workspaceRoot,
  editedFiles,
  playbooks = null,
  limit = 3
) {
  const editedSet = new Set(Array.from(editedFiles || []).filter(Boolean));
  if (editedSet.size === 0) return [];
  const ws = path.resolve(workspaceRoot);
  const pairs = computeCochangeStats(workspaceRoot, playbooks);
  const out = [];
  const seenPartners = new Set();

  for (const p of pairs) {
    let partner;
    let src;
    if (editedSet.has(p.file_a) && !editedSet.has(p.file_b)) {
      partner = p.file_b;
      src = p.file_a;
    } else if (editedSet.has(p.file_b) && !editedSet.has(p.file_a)) {
      partner = p.file_a;
      src = p.file_b;
    } else {
      continue;
    }
    if (seenPartners.has(partner)) continue;
    if (!existsSync(path.join(ws, partner))) continue;
    seenPartners.add(partner);
    out.push([src, partner, p.count, p.confidence]);
    if (out.length >= limit) break;
  }
  return out;
}
