/**
 * Repository-Scoped Storage & Retrieval Engine (.tmem/) using Node >= 22.5 `node:sqlite` FTS5.
 */

import { createHash } from "node:crypto";
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
import { SessionEpisode } from "./episodes.mjs";
import { evaluateGuards } from "./guards.mjs";
import { logger } from "./index.mjs";
import {
  COMMON_QUERY_STOPWORDS,
  deleteDocumentPostings,
  deleteEpisodeFromDb,
  ensureInvertedSchema,
  extractPostingsForPlaybook,
  queryInvertedIndex,
  upsertDocumentPostings,
  upsertEpisodeToDb,
} from "./inverted_index.mjs";
import { syncMemoriesFile } from "./mem_doc.mjs";
import { RawExecutionTrace, TracePlaybook } from "./models.mjs";
import { RawTraceStore } from "./raw_storage.mjs";
import { assessPlaybookTriviality } from "./slicer.mjs";
import { withDatabase } from "./sqlite_compat.mjs";

export { COMMON_QUERY_STOPWORDS };

const ERROR_ARCHETYPE_RE =
  /^(?:[a-z0-9_]+\.)*([a-z0-9_]*(?:error|exception|fault|panic)|error\[e\d+\]|cannot find module)\b/i;

export const GENERIC_ERROR_WORDS = new Set([
  "error", "exception", "fault", "panic", "invalid", "bad", "missing", "unexpected",
  "failed", "named", "module", "object", "attribute", "has", "no", "cannot", "can",
  "only", "literal", "argument", "keyword", "value", "type", "key", "index", "name",
  "line", "file", "most", "recent", "call", "last",
]);

function extractErrorArchetype(sig) {
  const m = String(sig || "").trim().toLowerCase().match(ERROR_ARCHETYPE_RE);
  return m ? m[1].toLowerCase() : "";
}

function extractErrorDetailTokens(sig, archetype = "") {
  let low = String(sig || "").trim().toLowerCase();
  if (archetype && low.startsWith(archetype)) {
    low = low.slice(archetype.length).replace(/^[:\s]+/, "");
  }
  const tokens = new Set();
  for (const m of low.matchAll(/[a-z0-9_]+/g)) {
    const t = m[0];
    if (t.length >= 2 && !COMMON_QUERY_STOPWORDS.has(t) && t !== archetype) {
      tokens.add(t);
    }
  }
  return tokens;
}

export function scoreL2ErrorMatch(querySig, storedSig) {
  const q = String(querySig || "").trim().toLowerCase();
  const s = String(storedSig || "").trim().toLowerCase();
  if (q.length < 6 || s.length < 6) return 0.0;
  if (q === s) return 15.0;

  const archQ = extractErrorArchetype(q);
  const archS = extractErrorArchetype(s);
  if (archQ && archS && archQ !== archS) return 0.0;

  const toksQ = extractErrorDetailTokens(q, archQ);
  const toksS = extractErrorDetailTokens(s, archS);
  if (toksQ.size === 0 || toksS.size === 0) return 0.0;

  const shared = new Set([...toksQ].filter((x) => toksS.has(x)));
  if (shared.size === 0) return 0.0;

  const union = new Set([...toksQ, ...toksS]);
  const minLen = Math.min(toksQ.size, toksS.size);
  const jaccard = union.size > 0 ? shared.size / union.size : 0.0;
  const containment = minLen > 0 ? shared.size / minLen : 0.0;

  const specificQ = new Set([...toksQ].filter((x) => !GENERIC_ERROR_WORDS.has(x)));
  const specificS = new Set([...toksS].filter((x) => !GENERIC_ERROR_WORDS.has(x)));
  const specificShared = [...specificQ].some((x) => specificS.has(x));
  if ((specificQ.size > 0 || specificS.size > 0) && !specificShared) {
    return 0.0;
  }

  if (jaccard >= 0.5 || containment >= 0.75) {
    return Math.round((8.0 + 4.0 * jaccard) * 100) / 100;
  }
  return 0.0;
}

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function listTraceJsonFiles(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => path.join(dir, f));
}

const _initializedDbPaths = new Set();

function ensureProjectSchema(db, dbPath = null) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS playbooks (
      trace_id TEXT PRIMARY KEY,
      scope TEXT,
      status TEXT,
      confidence REAL,
      hit_count INTEGER,
      created_at TEXT,
      payload_json TEXT
    );
    CREATE TABLE IF NOT EXISTS exact_triggers (
      trace_id TEXT,
      trigger_type TEXT,
      trigger_key TEXT,
      PRIMARY KEY (trace_id, trigger_type, trigger_key)
    );
    CREATE INDEX IF NOT EXISTS idx_trigger_lookup
      ON exact_triggers(trigger_type, trigger_key);
    CREATE TABLE IF NOT EXISTS trace_links (
      symbol TEXT,
      trace_id TEXT,
      PRIMARY KEY (symbol, trace_id)
    );
    CREATE INDEX IF NOT EXISTS idx_trace_links_sym
      ON trace_links(symbol);
    CREATE VIRTUAL TABLE IF NOT EXISTS playbooks_fts USING fts5(
      trace_id UNINDEXED,
      problem_intent,
      error_signatures,
      touched_files,
      touched_symbols,
      repairs_text
    );
  `);
  ensureInvertedSchema(db);
  if (dbPath) {
    _initializedDbPaths.add(path.resolve(dbPath));
  }
}

export class ProjectMemoryStore {
  constructor(workspaceRoot) {
    this.workspace_root = path.resolve(workspaceRoot);
    this.project_dir = path.join(this.workspace_root, ".tmem");
    this.project_traces_dir = path.join(this.project_dir, "traces");
    this.project_raw_dir = path.join(this.project_dir, "raw");
    this.project_episodes_dir = path.join(this.project_dir, "episodes");
    this.project_cache_dir = path.join(this.project_dir, ".cache");
    this.project_db_path = path.join(this.project_cache_dir, "index.db");
    this.raw_store = new RawTraceStore(this.workspace_root);
  }

  ensureInitialized(initProject = true) {
    if (!initProject) return;
    mkdirSync(this.project_traces_dir, { recursive: true });
    mkdirSync(this.project_raw_dir, { recursive: true });
    mkdirSync(this.project_episodes_dir, { recursive: true });
    mkdirSync(this.project_cache_dir, { recursive: true });

    const gitignore = path.join(this.project_dir, ".gitignore");
    const desiredLines = [
      ".cache/",
      "runtime_state.json",
      "telemetry.jsonl",
      "episodes/",
    ];
    if (!existsSync(gitignore)) {
      writeFileSync(gitignore, desiredLines.join("\n") + "\n", "utf-8");
    } else {
      const curGi = readFileSync(gitignore, "utf-8");
      const missing = desiredLines.filter((ln) => !curGi.includes(ln));
      if (missing.length > 0) {
        writeFileSync(
          gitignore,
          curGi.trimEnd() + "\n" + missing.join("\n") + "\n",
          "utf-8"
        );
      }
    }
    this._initSqlite(this.project_db_path);
  }

  _initSqlite(dbPath) {
    const resolved = path.resolve(dbPath);
    if (existsSync(resolved) && _initializedDbPaths.has(resolved)) {
      return;
    }
    mkdirSync(path.dirname(dbPath), { recursive: true });
    withDatabase(dbPath, (db) => {
      ensureProjectSchema(db, resolved);
    });
  }

  _withSyncedProjectDb(fn, { createIfMissing = false } = {}) {
    const hasTraces = existsSync(this.project_traces_dir);
    const hasEpisodes = existsSync(this.project_episodes_dir);
    if (!hasTraces && !hasEpisodes) {
      if (!createIfMissing && !existsSync(this.project_db_path)) {
        return null;
      }
    }
    mkdirSync(this.project_cache_dir, { recursive: true });
    const resolved = path.resolve(this.project_db_path);
    const needsSchema = !existsSync(resolved) || !_initializedDbPaths.has(resolved);

    const jsonFiles = hasTraces ? listTraceJsonFiles(this.project_traces_dir) : [];
    const diskIds = new Set(jsonFiles.map((jf) => path.basename(jf, ".json")));
    const epFiles = hasEpisodes ? listTraceJsonFiles(this.project_episodes_dir) : [];
    const epDiskIds = new Set(epFiles.map((ef) => path.basename(ef, ".json")));

    return withDatabase(this.project_db_path, (db) => {
      if (needsSchema) {
        ensureProjectSchema(db, resolved);
      }
      const rows = db.prepare("SELECT trace_id FROM playbooks").all();
      const dbIds = new Set(rows.map((r) => r.trace_id));

      for (const oid of dbIds) {
        if (!diskIds.has(oid)) {
          db.prepare("DELETE FROM playbooks WHERE trace_id = ?").run(oid);
          db.prepare("DELETE FROM exact_triggers WHERE trace_id = ?").run(oid);
          db.prepare("DELETE FROM playbooks_fts WHERE trace_id = ?").run(oid);
          deleteDocumentPostings(db, "playbook", oid);
          try {
            db.prepare("DELETE FROM trace_links WHERE trace_id = ?").run(oid);
          } catch (_err) {
            // ignore
          }
        }
      }

      const invPbRow = db
        .prepare("SELECT COUNT(DISTINCT doc_id) AS cnt FROM inverted_postings WHERE doc_type = 'playbook'")
        .get();
      const missingInvertedPb = diskIds.size > 0 && Number(invPbRow?.cnt || 0) === 0;
      const setsDiffer =
        missingInvertedPb ||
        dbIds.size !== diskIds.size ||
        Array.from(diskIds).some((id) => !dbIds.has(id));
      if (setsDiffer) {
        for (const jf of jsonFiles) {
          try {
            const pb = TracePlaybook.fromDict(JSON.parse(readFileSync(jf, "utf-8")));
            this._upsertToDb(db, pb);
          } catch (_err) {
            // ignore malformed
          }
        }
      }

      // Sync episodes into episodes table + inverted_postings
      const epRows = db.prepare("SELECT episode_id FROM episodes").all();
      const epDbIds = new Set(epRows.map((r) => r.episode_id));
      for (const oep of epDbIds) {
        if (!epDiskIds.has(oep)) {
          deleteEpisodeFromDb(db, oep);
        }
      }
      const epSetsDiffer =
        epDbIds.size !== epDiskIds.size ||
        Array.from(epDiskIds).some((id) => !epDbIds.has(id));
      if (epSetsDiffer) {
        for (const ef of epFiles) {
          try {
            const rawStr = readFileSync(ef, "utf-8");
            const ep = SessionEpisode.fromDict(JSON.parse(rawStr));
            upsertEpisodeToDb(db, ep, Buffer.byteLength(rawStr, "utf-8"));
          } catch (_err) {
            // ignore malformed
          }
        }
      }

      return fn(db);
    });
  }

  syncProjectIndexIfNeeded() {
    this._withSyncedProjectDb(() => {});
  }

  static _tokenJaccard(textA, textB) {
    const extract = (txt) => {
      const s = new Set();
      for (const m of (txt || "").matchAll(/[A-Za-z0-9_]+|[\u4e00-\u9fff]+/g)) {
        const t = m[0].toLowerCase();
        if (t.length >= 2 && !COMMON_QUERY_STOPWORDS.has(t)) s.add(t);
      }
      return s;
    };
    const toksA = extract(textA);
    const toksB = extract(textB);
    if (toksA.size === 0 || toksB.size === 0) return 0.0;
    const inter = [...toksA].filter((x) => toksB.has(x)).length;
    const union = new Set([...toksA, ...toksB]).size;
    return union > 0 ? inter / union : 0.0;
  }

  static _setJaccard(a, b) {
    const sa = new Set(
      (a || []).map((x) => String(x || "").trim().toLowerCase()).filter(Boolean)
    );
    const sb = new Set(
      (b || []).map((x) => String(x || "").trim().toLowerCase()).filter(Boolean)
    );
    if (sa.size === 0 || sb.size === 0) return 0.0;
    const inter = [...sa].filter((x) => sb.has(x)).length;
    const union = new Set([...sa, ...sb]).size;
    return union > 0 ? inter / union : 0.0;
  }

  _findSimilarPlaybook(incoming) {
    const inErrs = new Set(
      (incoming.error_signatures || [])
        .map((e) => String(e || "").trim().toLowerCase())
        .filter(Boolean)
    );
    const inRepairs = new Set(
      (incoming.l2_causal_repairs || []).map(
        (r) =>
          `${String(r.error_signature || "").trim().toLowerCase()}\0${String(
            r.repair_action || ""
          )
            .trim()
            .toLowerCase()}`
      )
    );

    for (const existing of this.getAllProjectPlaybooks()) {
      if (existing.trace_id === incoming.trace_id || existing.status === "invalidated") {
        continue;
      }
      const fileSim = ProjectMemoryStore._setJaccard(
        existing.touched_files,
        incoming.touched_files
      );
      const exErrs = new Set(
        (existing.error_signatures || [])
          .map((e) => String(e || "").trim().toLowerCase())
          .filter(Boolean)
      );
      const exRepairs = new Set(
        (existing.l2_causal_repairs || []).map(
          (r) =>
            `${String(r.error_signature || "").trim().toLowerCase()}\0${String(
              r.repair_action || ""
            )
              .trim()
              .toLowerCase()}`
        )
      );

      if (inRepairs.size > 0 && exRepairs.size > 0) {
        const repOverlap = [...inRepairs].some((k) => exRepairs.has(k));
        if (
          repOverlap &&
          (fileSim >= 0.5 ||
            (incoming.l2_causal_repairs || []).some((r) => r.is_env_or_toolchain))
        ) {
          return existing;
        }
      }
      if (inErrs.size > 0 && exErrs.size > 0) {
        const errOverlap = [...inErrs].some((e) => exErrs.has(e));
        if (errOverlap && fileSim >= 0.6) return existing;
      }

      if (
        incoming.touched_files.length > 0 &&
        existing.touched_files.length > 0 &&
        fileSim >= 0.8 &&
        ProjectMemoryStore._tokenJaccard(
          existing.problem_intent,
          incoming.problem_intent
        ) >= 0.45
      ) {
        return existing;
      }
    }
    return null;
  }

  static _mergePlaybooks(existing, incoming) {
    const union = (a, b) =>
      Array.from(new Set([...(a || []), ...(b || [])].filter(Boolean)));

    existing.created_at = incoming.created_at || existing.created_at;
    existing.git_commit = incoming.git_commit || existing.git_commit;
    existing.status = "active";
    existing.guard_fail_streak = 0;
    existing.merge_count = Number(existing.merge_count || 0) + 1;
    existing.confidence = Math.min(
      1.0,
      Math.round((Math.max(existing.confidence, incoming.confidence) + 0.08) * 1000) /
        1000
    );
    existing.is_trivial = Boolean(existing.is_trivial && incoming.is_trivial);

    existing.error_signatures = union(
      existing.error_signatures,
      incoming.error_signatures
    );
    existing.source_session_ids = union(
      existing.source_session_ids,
      incoming.source_session_ids
    );
    existing.touched_files = union(existing.touched_files, incoming.touched_files);
    existing.touched_symbols = union(
      existing.touched_symbols,
      incoming.touched_symbols
    );
    existing.l1_cochange_topology = union(
      existing.l1_cochange_topology,
      incoming.l1_cochange_topology
    );
    existing.l1_verification_cmds = union(
      existing.l1_verification_cmds,
      incoming.l1_verification_cmds
    );
    existing.pitfalls = union(existing.pitfalls, incoming.pitfalls);

    const seenRep = new Set(
      (existing.l2_causal_repairs || []).map(
        (r) =>
          `${String(r.error_signature || "").trim().toLowerCase()}\0${String(
            r.repair_action || ""
          )
            .trim()
            .toLowerCase()}`
      )
    );
    for (const r of incoming.l2_causal_repairs || []) {
      const key = `${String(r.error_signature || "").trim().toLowerCase()}\0${String(
        r.repair_action || ""
      )
        .trim()
        .toLowerCase()}`;
      if (!seenRep.has(key)) {
        seenRep.add(key);
        existing.l2_causal_repairs.push(r);
      }
    }

    if (
      incoming.guards &&
      incoming.guards.length > 0 &&
      (incoming.l0_critical_path || []).length >=
        (existing.l0_critical_path || []).length
    ) {
      existing.guards = incoming.guards;
      existing.l0_critical_path = incoming.l0_critical_path;
      existing.raw_steps_count = incoming.raw_steps_count || existing.raw_steps_count;
      existing.sliced_steps_count =
        incoming.sliced_steps_count || existing.sliced_steps_count;
    }

    if (
      incoming.root_cause &&
      (!existing.root_cause || incoming.root_cause.length > existing.root_cause.length)
    ) {
      existing.root_cause = incoming.root_cause;
    }
    if (incoming.semantic_summary && !existing.semantic_summary) {
      existing.semantic_summary = incoming.semantic_summary;
    }
    return existing;
  }

  savePlaybook(
    playbook,
    { targetScope = "project", mergeSimilar = true, rawTrace = null } = {}
  ) {
    this.ensureInitialized(true);
    const origTraceId = playbook.trace_id;
    const incomingL0Len = (playbook.l0_critical_path || []).length;
    const incomingHasGuards = Boolean(playbook.guards && playbook.guards.length > 0);

    playbook.scope = "project";
    let merged = false;
    let existingL0Len = 0;

    if (mergeSimilar) {
      const similar = this._findSimilarPlaybook(playbook);
      if (similar !== null) {
        existingL0Len = (similar.l0_critical_path || []).length;
        playbook = ProjectMemoryStore._mergePlaybooks(similar, playbook);
        merged = true;
      }
    }

    const rawPath = path.join(this.project_raw_dir, `${playbook.trace_id}.raw.json`);
    const l0Refreshed = incomingHasGuards && incomingL0Len >= existingL0Len;

    if (rawTrace !== null) {
      if (!merged || l0Refreshed || !existsSync(rawPath)) {
        rawTrace.trace_id = playbook.trace_id;
        this.raw_store.saveRawTrace(rawTrace);
      }
    } else if (merged && origTraceId !== playbook.trace_id) {
      const origRawPath = path.join(this.project_raw_dir, `${origTraceId}.raw.json`);
      if (existsSync(origRawPath)) {
        if (l0Refreshed || !existsSync(rawPath)) {
          try {
            const rawObj = RawExecutionTrace.fromDict(
              JSON.parse(readFileSync(origRawPath, "utf-8"))
            );
            rawObj.trace_id = playbook.trace_id;
            this.raw_store.saveRawTrace(rawObj);
          } catch (_err) {
            // ignore
          }
        }
        if (!existsSync(path.join(this.project_traces_dir, `${origTraceId}.json`))) {
          try {
            unlinkSync(origRawPath);
          } catch (_err) {
            // ignore
          }
        }
      }
    }

    if (existsSync(rawPath)) {
      playbook.raw_trace_path = `raw/${playbook.trace_id}.raw.json`;
      playbook.raw_trace_hash = createHash("sha256")
        .update(readFileSync(rawPath))
        .digest("hex")
        .slice(0, 16);
    }

    const projFile = path.join(this.project_traces_dir, `${playbook.trace_id}.json`);
    writeFileSync(projFile, JSON.stringify(playbook.toDict(), null, 2), "utf-8");

    withDatabase(this.project_db_path, (db) => {
      this._upsertToDb(db, playbook);
    });

    const projId = playbook.trace_id;
    try {
      this.syncMemoriesMarkdown();
    } catch (_err) {
      // ignore
    }
    return [projId, null];
  }

  _upsertToDb(db, pb) {
    db.prepare(
      `INSERT OR REPLACE INTO playbooks
       (trace_id, scope, status, confidence, hit_count, created_at, payload_json)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(
      pb.trace_id,
      pb.scope,
      pb.status,
      pb.confidence,
      pb.hit_count,
      pb.created_at,
      JSON.stringify(pb.toDict())
    );

    db.prepare("DELETE FROM exact_triggers WHERE trace_id = ?").run(pb.trace_id);
    db.prepare("DELETE FROM playbooks_fts WHERE trace_id = ?").run(pb.trace_id);
    try {
      db.prepare("DELETE FROM trace_links WHERE trace_id = ?").run(pb.trace_id);
    } catch (_err) {
      // ignore
    }

    const allErrSigs = new Set();
    for (const e of pb.error_signatures || []) {
      if (e && e.trim()) allErrSigs.add(e.trim().toLowerCase());
    }
    for (const r of pb.l2_causal_repairs || []) {
      if (r.error_signature && r.error_signature.trim()) {
        allErrSigs.add(r.error_signature.trim().toLowerCase());
      }
    }

    const insertTrigger = db.prepare(
      "INSERT OR IGNORE INTO exact_triggers VALUES (?, ?, ?)"
    );
    for (const errLow of Array.from(allErrSigs).sort()) {
      insertTrigger.run(pb.trace_id, "error", errLow);
    }
    for (const f of pb.touched_files || []) {
      insertTrigger.run(pb.trace_id, "file", f.toLowerCase());
    }
    const insertLink = db.prepare("INSERT OR IGNORE INTO trace_links VALUES (?, ?)");
    for (const sym of pb.touched_symbols || []) {
      insertTrigger.run(pb.trace_id, "symbol", sym.toLowerCase());
      try {
        insertLink.run(sym.toLowerCase(), pb.trace_id);
      } catch (_err) {
        // ignore
      }
    }

    const repairsText = (pb.l2_causal_repairs || [])
      .map((r) => `${r.error_signature} ${r.repair_action}`)
      .join(" ");
    const stepRats = (pb.l0_critical_path || [])
      .map((s) => String(s.rationale || ""))
      .join(" ");
    const intentAndSummary = `${pb.problem_intent} ${pb.semantic_summary} ${
      pb.root_cause || ""
    } ${stepRats}`.trim();

    db.prepare(
      `INSERT INTO playbooks_fts
       (trace_id, problem_intent, error_signatures, touched_files, touched_symbols, repairs_text)
       VALUES (?, ?, ?, ?, ?, ?)`
    ).run(
      pb.trace_id,
      intentAndSummary,
      (pb.error_signatures || []).join(" "),
      (pb.touched_files || []).join(" "),
      (pb.touched_symbols || []).join(" "),
      repairsText
    );

    ensureInvertedSchema(db);
    upsertDocumentPostings(db, "playbook", pb.trace_id, extractPostingsForPlaybook(pb));
  }

  cascadingLookup({
    queryText = "",
    filePaths = null,
    errorSignatures = null,
    limit = 3,
  } = {}) {
    this.syncProjectIndexIfNeeded();
    if (!existsSync(this.project_db_path)) return [];

    const candidates = new Map();
    for (const [pb, score] of this._querySingleDb(
      this.project_db_path,
      queryText,
      filePaths || [],
      errorSignatures || []
    )) {
      if (!candidates.has(pb.trace_id) && pb.status !== "invalidated") {
        candidates.set(pb.trace_id, [pb, score]);
      }
    }

    const evaluatedPairs = [];
    for (const [pb, score] of candidates.values()) {
      const ev = evaluateGuards(pb, this.workspace_root);
      evaluatedPairs.push([ev, score]);
    }

    evaluatedPairs.sort((a, b) => {
      const [evA, scA] = a;
      const [evB, scB] = b;
      const rA = Math.round(scA * 100);
      const rB = Math.round(scB * 100);
      if (rB !== rA) return rB - rA;
      const l0A = evA.l0_valid ? 1 : 0;
      const l0B = evB.l0_valid ? 1 : 0;
      if (l0B !== l0A) return l0B - l0A;
      return evB.playbook.confidence - evA.playbook.confidence;
    });

    return evaluatedPairs.slice(0, limit).map(([ev]) => ev);
  }

  _querySingleDb(dbPath, queryText, filePaths, errorSignatures) {
    const scores = new Map();

    const results = withDatabase(dbPath, (db) => {
      const errStmt = db.prepare(
        "SELECT trace_id, trigger_key FROM exact_triggers WHERE trigger_type = 'error' AND (? LIKE '%' || trigger_key || '%' OR trigger_key LIKE '%' || ? || '%')"
      );
      for (const err of errorSignatures) {
        const low = err.toLowerCase();
        for (const row of errStmt.all(low, low)) {
          const boost = row.trigger_key === low ? 10.0 : 5.0;
          scores.set(row.trace_id, (scores.get(row.trace_id) || 0) + boost);
        }
      }

      const fileStmt = db.prepare(
        "SELECT trace_id FROM exact_triggers WHERE trigger_type = 'file' AND trigger_key = ?"
      );
      for (const fp of filePaths) {
        for (const row of fileStmt.all(fp.toLowerCase())) {
          scores.set(row.trace_id, (scores.get(row.trace_id) || 0) + 12.0);
        }
      }

      if (queryText) {
        const qLow = queryText.toLowerCase();
        const mentionStmt = db.prepare(
          "SELECT DISTINCT trace_id, trigger_key FROM exact_triggers WHERE LENGTH(trigger_key) >= 4 AND ? LIKE '%' || trigger_key || '%'"
        );
        for (const row of mentionStmt.all(qLow)) {
          const re = new RegExp(
            `(?<![A-Za-z0-9_])${escapeRegExp(row.trigger_key)}(?![A-Za-z0-9_])`
          );
          if (re.test(qLow)) {
            scores.set(row.trace_id, (scores.get(row.trace_id) || 0) + 6.0);
          }
        }

        const tokens = [];
        for (const m of queryText.matchAll(/[A-Za-z0-9_]+|[\u4e00-\u9fff]+/g)) {
          const t = m[0].toLowerCase();
          if (t.length >= 3 && !COMMON_QUERY_STOPWORDS.has(t)) tokens.push(t);
        }
        if (tokens.length > 0) {
          const candTokens = tokens.slice(0, 15);
          const tokenHits = new Map();
          const ftsStmt = db.prepare(
            "SELECT trace_id FROM playbooks_fts WHERE playbooks_fts MATCH ?"
          );
          for (const t of candTokens) {
            try {
              for (const row of ftsStmt.all(`"${t}"`)) {
                tokenHits.set(row.trace_id, (tokenHits.get(row.trace_id) || 0) + 1);
              }
            } catch (_err) {
              // ignore FTS syntax errors
            }
          }
          const minHits = candTokens.length === 1 && candTokens[0].length >= 6 ? 1 : 2;
          for (const [tid, hits] of tokenHits.entries()) {
            const coverage = hits / candTokens.length;
            if (hits >= minHits && coverage >= 0.25) {
              scores.set(tid, (scores.get(tid) || 0) + hits * 2.0);
            }
          }
        }
      }

      const out = [];
      const pbStmt = db.prepare("SELECT payload_json FROM playbooks WHERE trace_id = ?");
      for (const [tid, score] of scores.entries()) {
        const row = pbStmt.get(tid);
        if (row && row.payload_json) {
          try {
            out.push([TracePlaybook.fromDict(JSON.parse(row.payload_json)), score]);
          } catch (_err) {
            // ignore
          }
        }
      }
      return out;
    });

    results.sort((a, b) => b[1] - a[1]);
    return results;
  }

  lookupL2Repair({ errorSignatures, filePaths = null, limit = 2 } = {}) {
    const sigs = (errorSignatures || [])
      .map((s) => String(s || "").trim())
      .filter((s) => s.length >= 6);
    if (sigs.length === 0) return [];
    this.syncProjectIndexIfNeeded();
    if (!existsSync(this.project_db_path)) return [];

    const results = withDatabase(this.project_db_path, (db) => {
      const scores = new Map();
      const triggerRows = db
        .prepare("SELECT trace_id, trigger_key FROM exact_triggers WHERE trigger_type = 'error'")
        .all();

      for (const err of sigs) {
        for (const row of triggerRows) {
          const matchScore = scoreL2ErrorMatch(err, row.trigger_key || "");
          if (matchScore > 0.0) {
            scores.set(row.trace_id, Math.max(scores.get(row.trace_id) || 0.0, matchScore));
          }
        }
      }

      if (scores.size === 0) return [];

      const fileSet = new Set(
        (filePaths || []).filter(Boolean).map((f) => f.toLowerCase())
      );
      const out = [];
      const pbStmt = db.prepare("SELECT payload_json FROM playbooks WHERE trace_id = ?");

      for (const [tid, baseSc] of scores.entries()) {
        const row = pbStmt.get(tid);
        if (!row || !row.payload_json) continue;
        let pb;
        try {
          pb = TracePlaybook.fromDict(JSON.parse(row.payload_json));
        } catch (_err) {
          continue;
        }
        if (
          pb.status === "invalidated" ||
          !pb.l2_causal_repairs ||
          pb.l2_causal_repairs.length === 0
        ) {
          continue;
        }

        let bestRepScore = 0.0;
        for (const err of sigs) {
          for (const rep of pb.l2_causal_repairs) {
            const rs = scoreL2ErrorMatch(err, rep.error_signature);
            if (rs > bestRepScore) bestRepScore = rs;
          }
        }
        if (bestRepScore <= 0.0) continue;

        let sc = Math.max(baseSc, bestRepScore);
        if (
          fileSet.size > 0 &&
          pb.touched_files.some((f) => fileSet.has(f.toLowerCase()))
        ) {
          sc += 3.0;
        }
        const ev = evaluateGuards(pb, this.workspace_root);
        out.push([ev, sc]);
      }
      return out;
    });

    results.sort((a, b) => {
      const [evA, scA] = a;
      const [evB, scB] = b;
      const rA = Math.round(scA * 100);
      const rB = Math.round(scB * 100);
      if (rB !== rA) return rB - rA;
      const l0A = evA.l0_valid ? 1 : 0;
      const l0B = evB.l0_valid ? 1 : 0;
      if (l0B !== l0A) return l0B - l0A;
      return evB.playbook.confidence - evA.playbook.confidence;
    });
    return results.slice(0, limit).map(([ev]) => ev);
  }

  runMetabolismGc(pruneStale = false) {
    this.syncProjectIndexIfNeeded();
    const stats = { active_l0: 0, degraded_l1_l2: 0, stale: 0, pruned: 0 };
    if (!existsSync(this.project_traces_dir)) return stats;

    withDatabase(this.project_db_path, (db) => {
      for (const jf of listTraceJsonFiles(this.project_traces_dir)) {
        try {
          const pb = TracePlaybook.fromDict(JSON.parse(readFileSync(jf, "utf-8")));
          const ev = evaluateGuards(pb, this.workspace_root);
          const [isTriv] = assessPlaybookTriviality(pb);
          pb.is_trivial = isTriv;

          if (ev.l0_valid) {
            pb.status = "active";
            pb.guard_fail_streak = 0;
            stats.active_l0 += 1;
          } else {
            pb.guard_fail_streak = Number(pb.guard_fail_streak || 0) + 1;
            if (
              (pb.l2_causal_repairs.length > 0 || pb.l1_cochange_topology.length > 0) &&
              pb.guard_fail_streak < 3
            ) {
              pb.status = "active";
              pb.confidence = Math.max(
                0.2,
                Math.round((pb.confidence - 0.05) * 1000) / 1000
              );
              stats.degraded_l1_l2 += 1;
            } else {
              pb.status = "stale";
              pb.confidence = Math.max(
                0.05,
                Math.round((pb.confidence - 0.3) * 1000) / 1000
              );
              stats.stale += 1;
              if (
                pruneStale &&
                pb.l2_causal_repairs.length === 0 &&
                (pb.guard_fail_streak >= 3 || pb.confidence <= 0.1)
              ) {
                try {
                  unlinkSync(jf);
                } catch (_err) {
                  // ignore
                }
                this.raw_store.deleteRawTrace(pb.trace_id, true);
                db.prepare("DELETE FROM playbooks WHERE trace_id = ?").run(pb.trace_id);
                db.prepare("DELETE FROM exact_triggers WHERE trace_id = ?").run(pb.trace_id);
                db.prepare("DELETE FROM playbooks_fts WHERE trace_id = ?").run(pb.trace_id);
                db.prepare("DELETE FROM trace_links WHERE trace_id = ?").run(pb.trace_id);
                try {
                  deleteDocumentPostings(db, "playbook", pb.trace_id);
                } catch (_err) {
                  // ignore
                }
                stats.pruned += 1;
                continue;
              }
            }
          }
          writeFileSync(jf, JSON.stringify(pb.toDict(), null, 2), "utf-8");
          this._upsertToDb(db, pb);
        } catch (_err) {
          // ignore
        }
      }
    });

    try {
      this.syncMemoriesMarkdown();
    } catch (_err) {
      // ignore
    }
    return stats;
  }

  listPlaybooks(_scope = "project") {
    const items = [];
    if (!existsSync(this.project_traces_dir)) return items;

    for (const jf of listTraceJsonFiles(this.project_traces_dir)) {
      try {
        const pb = TracePlaybook.fromDict(JSON.parse(readFileSync(jf, "utf-8")));
        const ev = evaluateGuards(pb, this.workspace_root);
        items.push({
          trace_id: pb.trace_id,
          source_session_ids: pb.source_session_ids,
          scope: "project",
          effective_tier: ev.effective_tier,
          problem_intent: pb.problem_intent,
          semantic_summary: pb.semantic_summary,
          touched_files: pb.touched_files,
          l2_repairs_count: pb.l2_causal_repairs.length,
          status: pb.status,
          created_at: pb.created_at,
        });
      } catch (_err) {
        // ignore
      }
    }
    return items;
  }

  deletePlaybook(traceId) {
    const removedFrom = [];
    const tid = String(traceId || "").trim();
    if (!tid) return { deleted: false, removed_from: [] };

    if (existsSync(this.project_traces_dir)) {
      for (const jf of listTraceJsonFiles(this.project_traces_dir)) {
        const stem = path.basename(jf, ".json");
        if (stem === tid || stem.startsWith(tid)) {
          try {
            unlinkSync(jf);
            removedFrom.push(`project:${stem}`);
          } catch (_err) {
            // ignore
          }
          if (existsSync(this.project_db_path)) {
            try {
              withDatabase(this.project_db_path, (db) => {
                db.prepare("DELETE FROM playbooks WHERE trace_id = ?").run(stem);
                db.prepare("DELETE FROM exact_triggers WHERE trace_id = ?").run(stem);
                db.prepare("DELETE FROM playbooks_fts WHERE trace_id = ?").run(stem);
                try {
                  deleteDocumentPostings(db, "playbook", stem);
                } catch (_err) {
                  // ignore
                }
                try {
                  db.prepare("DELETE FROM trace_links WHERE trace_id = ?").run(stem);
                } catch (_err) {
                  // ignore
                }
              });
            } catch (_err) {
              // ignore
            }
          }
        }
      }
    }

    try {
      this.raw_store.deleteRawTrace(tid);
    } catch (_err) {
      // ignore
    }
    try {
      this.syncMemoriesMarkdown();
    } catch (_err) {
      // ignore
    }
    return { deleted: removedFrom.length > 0, removed_from: removedFrom };
  }

  getRawTrace(traceId) {
    return this.raw_store.loadRawTrace(traceId);
  }

  getAllProjectPlaybooks() {
    if (!existsSync(this.project_traces_dir)) return [];
    const playbooks = [];
    for (const jf of listTraceJsonFiles(this.project_traces_dir)) {
      try {
        playbooks.push(TracePlaybook.fromDict(JSON.parse(readFileSync(jf, "utf-8"))));
      } catch (_err) {
        // ignore
      }
    }
    return playbooks;
  }

  getLinkedTraces(symbol, excludeTraceId = "") {
    if (!symbol || !existsSync(this.project_db_path)) return [];
    try {
      return withDatabase(this.project_db_path, (db) => {
        const rows = db
          .prepare(
            "SELECT trace_id FROM trace_links WHERE symbol = ? AND trace_id != ? LIMIT 3"
          )
          .all(symbol.toLowerCase(), excludeTraceId);
        return rows.map((r) => r.trace_id);
      });
    } catch (_err) {
      return [];
    }
  }

  syncMemoriesMarkdown() {
    const projPbs = this.getAllProjectPlaybooks();
    return syncMemoriesFile(this.workspace_root, projPbs);
  }

  recordTraceOutcome(traceIds, outcome) {
    if (!traceIds || traceIds.length === 0 || !["success", "contradict"].includes(outcome)) {
      return {};
    }
    if (!existsSync(this.project_traces_dir)) return {};

    this.syncProjectIndexIfNeeded();
    const updated = {};
    try {
      withDatabase(this.project_db_path, (db) => {
        for (const tid of Array.from(new Set(traceIds))) {
          const jf = path.join(this.project_traces_dir, `${tid}.json`);
          if (!existsSync(jf)) continue;
          let pb;
          try {
            pb = TracePlaybook.fromDict(JSON.parse(readFileSync(jf, "utf-8")));
          } catch (_err) {
            continue;
          }

          pb.hit_count += 1;
          if (outcome === "success") {
            pb.success_count += 1;
            pb.confidence = Math.min(
              1.0,
              Math.round((pb.confidence + 0.1) * 1000) / 1000
            );
            if (pb.status === "stale" && pb.confidence >= 0.4) {
              pb.status = "active";
            }
          } else {
            pb.contradict_count += 1;
            pb.confidence = Math.max(
              0.0,
              Math.round((pb.confidence - 0.2) * 1000) / 1000
            );
            if (pb.confidence < 0.3 || pb.contradict_count >= 2) {
              pb.status = "stale";
            }
          }

          writeFileSync(jf, JSON.stringify(pb.toDict(), null, 2), "utf-8");
          this._upsertToDb(db, pb);
          updated[tid] = pb.confidence;
        }
      });
    } catch (err) {
      logger.debug("Failed to record trace outcome:", err?.message || err);
    }
    return updated;
  }

  /**
   * Persist a compacted or captured SessionEpisode to `.tmem/episodes/`
   * and index its Turn-level multi-role postings into `index.db`.
   */
  saveEpisode(episode, { autoGc = true } = {}) {
    if (!episode || !episode.episode_id) return null;
    this.ensureInitialized(true);
    const ep = episode instanceof SessionEpisode ? episode : SessionEpisode.fromDict(episode);

    // Automatically chain `prev_episode_id` and `phase_index` within the same session
    try {
      withDatabase(this.project_db_path, (db) => {
        ensureInvertedSchema(db);
        const priors = db
          .prepare(
            `SELECT episode_id, seq_from, seq_to, payload_json
             FROM episodes
             WHERE session_id = ? AND episode_id != ?
             ORDER BY seq_from ASC, seq_to ASC, created_at ASC`
          )
          .all(ep.session_id || "default", ep.episode_id);
        if (priors.length > 0) {
          const lastPrior = priors[priors.length - 1];
          if (!ep.prev_episode_id) {
            ep.prev_episode_id = lastPrior.episode_id;
          }
          let prevPhase = priors.length;
          try {
            const pObj = lastPrior.payload_json ? JSON.parse(lastPrior.payload_json) : null;
            if (pObj && Number.isFinite(Number(pObj.phase_index))) {
              prevPhase = Math.max(prevPhase, Number(pObj.phase_index));
            }
          } catch (_e) {
            // ignore
          }
          ep.phase_index = prevPhase + 1;
        }
      });
    } catch (_err) {
      // ignore chaining error
    }

    const jsonStr = JSON.stringify(ep.toDict(), null, 2);
    const epFile = path.join(this.project_episodes_dir, `${ep.episode_id}.json`);
    const tmpFile = path.join(
      this.project_episodes_dir,
      `.${ep.episode_id}.${process.pid}.tmp`
    );
    writeFileSync(tmpFile, jsonStr, "utf-8");
    renameSync(tmpFile, epFile);

    const byteSize = Buffer.byteLength(jsonStr, "utf-8");
    withDatabase(this.project_db_path, (db) => {
      upsertEpisodeToDb(db, ep, byteSize);
    });

    if (autoGc) {
      try {
        this.runEpisodeGc({ maxMb: 200, maxAgeDays: 30 });
      } catch (_err) {
        // ignore GC failure during hot write
      }
    }
    return ep.episode_id;
  }

  /**
   * Load a SessionEpisode by exact or prefix `episode_id`, optionally refreshing LRU timestamp.
   */
  loadEpisode(episodeId, { touchLru = false } = {}) {
    const eid = String(episodeId || "").trim();
    if (!eid || !existsSync(this.project_episodes_dir)) return null;

    let targetFile = path.join(this.project_episodes_dir, `${eid}.json`);
    if (!existsSync(targetFile)) {
      const matches = readdirSync(this.project_episodes_dir)
        .filter((f) => f.startsWith(eid) && f.endsWith(".json"))
        .sort();
      if (matches.length === 0) return null;
      targetFile = path.join(this.project_episodes_dir, matches[0]);
    }

    try {
      const ep = SessionEpisode.fromDict(JSON.parse(readFileSync(targetFile, "utf-8")));
      if (touchLru) {
        const lastMs = Date.parse(ep.last_accessed_at || "");
        const nowMs = Date.now();
        if (!Number.isFinite(lastMs) || nowMs - lastMs >= 60_000) {
          ep.last_accessed_at = new Date(nowMs).toISOString();
          try {
            writeFileSync(targetFile, JSON.stringify(ep.toDict(), null, 2), "utf-8");
            if (existsSync(this.project_db_path)) {
              const resolvedDb = path.resolve(this.project_db_path);
              withDatabase(this.project_db_path, (db) => {
                if (!_initializedDbPaths.has(resolvedDb)) {
                  ensureInvertedSchema(db);
                }
                db.prepare(
                  "UPDATE episodes SET last_accessed_at = ? WHERE episode_id = ?"
                ).run(ep.last_accessed_at, ep.episode_id);
              });
            }
          } catch (_err) {
            // ignore LRU write failure
          }
        }
      }
      return ep;
    } catch (_err) {
      return null;
    }
  }

  findEpisodesByCompactionId(compactionId, sessionId = null) {
    const cid = String(compactionId || "").trim();
    if (!cid) return [];
    try {
      const epIds = this._withSyncedProjectDb((db) => {
        const rows = sessionId
          ? db
              .prepare(
                "SELECT episode_id FROM episodes WHERE compaction_id = ? AND session_id = ? ORDER BY seq_from ASC"
              )
              .all(cid, sessionId)
          : db
              .prepare(
                "SELECT episode_id FROM episodes WHERE compaction_id = ? ORDER BY seq_from ASC"
              )
              .all(cid);
        return rows.map((r) => r.episode_id).filter(Boolean);
      });
      if (!epIds) return [];
      return epIds.map((id) => this.loadEpisode(id)).filter(Boolean);
    } catch (_err) {
      return [];
    }
  }

  findEpisodeByCompactionId(compactionId, sessionId = null) {
    const eps = this.findEpisodesByCompactionId(compactionId, sessionId);
    return eps.length > 0 ? eps[eps.length - 1] : null;
  }

  getSessionTimeline(sessionId, { limit = 20 } = {}) {
    const sid = String(sessionId || "").trim();
    if (!sid) return [];
    try {
      const res = this._withSyncedProjectDb((db) => {
        const rows = db
          .prepare(
            `SELECT episode_id, session_id, compaction_id, trigger, seq_from, seq_to,
                    created_at, phase_intent, touched_files_json, payload_json
             FROM episodes
             WHERE session_id = ?
             ORDER BY seq_from ASC, seq_to ASC, created_at ASC
             LIMIT ?`
          )
          .all(sid, limit);
        return rows.map((r, idx) => {
          let pObj = null;
          try {
            pObj = r.payload_json ? JSON.parse(r.payload_json) : null;
          } catch (_e) {
            pObj = null;
          }
          return {
            episode_id: r.episode_id,
            session_id: r.session_id,
            compaction_id: r.compaction_id || "",
            trigger: r.trigger || "compaction",
            phase_index: Number(pObj?.phase_index ?? idx + 1),
            prev_episode_id: pObj?.prev_episode_id || (idx > 0 ? rows[idx - 1].episode_id : null),
            seq_from: Number(r.seq_from ?? 0),
            seq_to: Number(r.seq_to ?? 0),
            created_at: r.created_at || "",
            phase_intent: r.phase_intent || "",
            touched_files: JSON.parse(r.touched_files_json || "[]"),
          };
        });
      });
      return res || [];
    } catch (_err) {
      return [];
    }
  }

  listEpisodes({ sessionId = null, limit = 50 } = {}) {
    const res = this._withSyncedProjectDb((db) => {
      const rows = sessionId
        ? db
            .prepare(
              `SELECT episode_id, session_id, compaction_id, trigger, seq_from, seq_to,
                      created_at, last_accessed_at, phase_intent, touched_files_json, byte_size
               FROM episodes WHERE session_id = ?
               ORDER BY created_at DESC, seq_to DESC LIMIT ?`
            )
            .all(sessionId, limit)
        : db
            .prepare(
              `SELECT episode_id, session_id, compaction_id, trigger, seq_from, seq_to,
                      created_at, last_accessed_at, phase_intent, touched_files_json, byte_size
               FROM episodes
               ORDER BY created_at DESC, seq_to DESC LIMIT ?`
            )
            .all(limit);
      return rows.map((r) => ({
        episode_id: r.episode_id,
        session_id: r.session_id,
        compaction_id: r.compaction_id || "",
        trigger: r.trigger || "compaction",
        seq_from: Number(r.seq_from ?? 0),
        seq_to: Number(r.seq_to ?? 0),
        created_at: r.created_at || "",
        last_accessed_at: r.last_accessed_at || "",
        phase_intent: r.phase_intent || "",
        touched_files: JSON.parse(r.touched_files_json || "[]"),
        byte_size: Number(r.byte_size || 0),
      }));
    });
    return res || [];
  }

  /**
   * Unified multi-dimensional inverted-index search across compacted Session Episodes
   * and verified Project Playbooks.
   */
  searchMemory({
    query = "",
    files = [],
    symbols = [],
    errors = [],
    roles = [],
    scope = "all",
    sessionId = null,
    limit = 5,
  } = {}) {
    const res = this._withSyncedProjectDb((db) =>
      queryInvertedIndex(db, {
        query,
        files,
        symbols,
        errors,
        roles,
        scope,
        sessionId,
        limit,
      })
    );
    return res || [];
  }

  /**
   * Enforce TTL and disk-quota LRU eviction on `.tmem/episodes/` without touching `.tmem/traces/`.
   */
  runEpisodeGc({ maxMb = 200, maxAgeDays = 30 } = {}) {
    const stats = { kept: 0, expired_pruned: 0, quota_pruned: 0, total_bytes: 0 };
    if (!existsSync(this.project_episodes_dir)) return stats;
    this.syncProjectIndexIfNeeded();
    if (!existsSync(this.project_db_path)) return stats;

    const nowMs = Date.now();
    const maxAgeMs = Math.max(1, Number(maxAgeDays || 30)) * 86400 * 1000;
    const maxBytes = Math.max(1, Number(maxMb || 200)) * 1024 * 1024;

    withDatabase(this.project_db_path, (db) => {
      ensureInvertedSchema(db);
      const rows = db
        .prepare(
          "SELECT episode_id, created_at, last_accessed_at, byte_size FROM episodes ORDER BY COALESCE(last_accessed_at, created_at) ASC"
        )
        .all();

      const survivors = [];
      let totalBytes = 0;

      for (const r of rows) {
        const epPath = path.join(this.project_episodes_dir, `${r.episode_id}.json`);
        let size = Number(r.byte_size || 0);
        if (existsSync(epPath) && size <= 0) {
          try {
            size = statSync(epPath).size;
          } catch (_err) {
            size = 0;
          }
        }
        const tsMs = Date.parse(r.last_accessed_at || r.created_at || "");
        if (Number.isFinite(tsMs) && nowMs - tsMs > maxAgeMs) {
          try {
            if (existsSync(epPath)) unlinkSync(epPath);
          } catch (_err) {
            // ignore
          }
          deleteEpisodeFromDb(db, r.episode_id);
          stats.expired_pruned += 1;
          continue;
        }
        totalBytes += size;
        survivors.push({ episode_id: r.episode_id, size, epPath });
      }

      // Evict oldest-LRU episodes if over byte budget
      let idx = 0;
      while (totalBytes > maxBytes && idx < survivors.length) {
        const victim = survivors[idx++];
        try {
          if (existsSync(victim.epPath)) unlinkSync(victim.epPath);
        } catch (_err) {
          // ignore
        }
        deleteEpisodeFromDb(db, victim.episode_id);
        totalBytes -= victim.size;
        stats.quota_pruned += 1;
      }

      stats.kept = survivors.length - idx;
      stats.total_bytes = Math.max(0, totalBytes);
    });

    return stats;
  }
}
