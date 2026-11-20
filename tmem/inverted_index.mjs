/**
 * Multi-Dimensional Deterministic Inverted Index & Ranked Retrieval Engine.
 *
 * Zero-LLM, zero-external-dependency inverted index stored in `.tmem/.cache/index.db`.
 * Indexes both compacted Session Episodes (`doc_type = 'episode'`) and verified
 * Project Playbooks (`doc_type = 'playbook'`) at Turn granularity across weighted
 * semantic roles (`intent`, `decision`, `error`, `symbol`, `file`, `command`, `diff`, `raw_output`).
 */

export const COMMON_QUERY_STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by", "from",
  "as", "is", "was", "are", "were", "it", "this", "that", "when", "if", "be", "not", "have",
  "has", "had", "do", "does", "did", "so", "can", "we", "you", "my", "your", "i", "me", "our",
  "all", "any", "some", "what", "which", "who", "whom", "how", "there", "their", "then", "into",
]);

export const CJK_FUNCTIONAL_STOP_NGRAMS = new Set([
  "我们", "你们", "他们", "这个", "那个", "这些", "那些", "一个", "一些", "一下",
  "帮我", "请问", "看看", "时候", "什么", "怎么", "为什么", "因为", "所以", "如果",
  "或者", "以及", "然后", "已经", "现在",
]);

const CJK_PARTICLE_SPLIT_RE = /[的了呢吧吗啊哦嘛]+/g;

export const ROLE_WEIGHTS = Object.freeze({
  intent: 3.5,
  error: 3.5,
  decision: 3.2,
  symbol: 3.0,
  file: 3.0,
  command: 2.2,
  diff: 2.0,
  raw_output: 1.0,
});

const ASCII_TOKEN_RE = /[A-Za-z0-9_./:-]{2,}/g;
const CJK_RUN_RE = /[\u4e00-\u9fff]+/g;
const MAX_POSTINGS_PER_ROLE_TURN = 256;

function addTermCount(map, term, inc = 1) {
  const t = String(term || "").trim().toLowerCase();
  if (!t || t.length < 2) return;
  if (COMMON_QUERY_STOPWORDS.has(t) || CJK_FUNCTIONAL_STOP_NGRAMS.has(t)) return;
  map.set(t, (map.get(t) || 0) + inc);
}

/**
 * Split identifiers and paths into both exact tokens and sub-tokens
 * (`parseDshEvents` -> `parsedsh...`, `parse`, `dsh`, `events`; `src/auth/jwt_guard.ts` -> components).
 */
function expandAsciiToken(rawToken, outMap) {
  const cleaned = rawToken.replace(/^[:./-]+|[:./-]+$/g, "");
  if (!cleaned || cleaned.length < 2) return;
  const low = cleaned.toLowerCase();
  addTermCount(outMap, low, 1);

  if (low.includes("/")) {
    const slashIdx = low.lastIndexOf("/");
    const base = low.slice(slashIdx + 1);
    if (base && base.length >= 2) {
      addTermCount(outMap, base, 1);
    }
  }

  // Split path / delimiter segments
  const segs = cleaned.split(/[./:-]+/).filter(Boolean);
  if (segs.length > 1) {
    for (const seg of segs) {
      if (seg.length >= 2) addTermCount(outMap, seg, 1);
      expandCamelAndSnake(seg, outMap);
    }
  } else {
    expandCamelAndSnake(cleaned, outMap);
  }
}

function expandCamelAndSnake(ident, outMap) {
  const snakeParts = ident.split(/_+/).filter(Boolean);
  for (const sp of snakeParts) {
    if (snakeParts.length > 1 && sp.length >= 2) {
      addTermCount(outMap, sp, 1);
    }
    // Split camelCase / PascalCase boundaries
    const camelParts = sp
      .replace(/([a-z0-9])([A-Z])/g, "$1\0$2")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1\0$2")
      .split("\0")
      .filter(Boolean);
    if (camelParts.length > 1) {
      for (const cp of camelParts) {
        if (cp.length >= 2) addTermCount(outMap, cp, 1);
      }
    }
  }
}

/**
 * Tokenize mixed Chinese/English engineering text into weighted term frequencies
 * for deterministic inverted indexing.
 * @param {string} text
 * @returns {Map<string, number>}
 */
export function tokenizeInvertedTerms(text) {
  const tfMap = new Map();
  if (!text) return tfMap;
  const str = String(text);

  for (const m of str.matchAll(ASCII_TOKEN_RE)) {
    expandAsciiToken(m[0], tfMap);
  }

  for (const m of str.matchAll(CJK_RUN_RE)) {
    const rawRun = m[0];
    if (rawRun.length === 1) continue;
    const subRuns = rawRun.split(CJK_PARTICLE_SPLIT_RE).filter((s) => s.length >= 2);
    for (const run of subRuns) {
      if (run.length <= 6) {
        addTermCount(tfMap, run, 1);
      }
      // Emit overlapping bigrams & trigrams so any Chinese phrase query matches reliably
      for (let i = 0; i <= run.length - 2; i++) {
        addTermCount(tfMap, run.slice(i, i + 2), 1);
        if (i <= run.length - 3) {
          addTermCount(tfMap, run.slice(i, i + 3), 1);
        }
      }
    }
  }

  return tfMap;
}

function addRoleTerms(postingsMap, text, { turnIdx, seqFrom, seqTo, role }) {
  if (!text) return;
  const tfMap = tokenizeInvertedTerms(text);
  if (tfMap.size === 0) return;

  const weight = ROLE_WEIGHTS[role] ?? 1.0;
  let entries = Array.from(tfMap.entries());
  if (entries.length > MAX_POSTINGS_PER_ROLE_TURN) {
    entries.sort((a, b) => b[1] - a[1]);
    entries = entries.slice(0, MAX_POSTINGS_PER_ROLE_TURN);
  }

  for (const [term, tf] of entries) {
    const key = `${term}\0${turnIdx}\0${role}`;
    const existing = postingsMap.get(key);
    if (existing) {
      existing.tf += tf;
      existing.seq_from = Math.min(existing.seq_from, seqFrom);
      existing.seq_to = Math.max(existing.seq_to, seqTo);
    } else {
      postingsMap.set(key, {
        term,
        turn_idx: turnIdx,
        seq_from: seqFrom,
        seq_to: seqTo,
        field_role: role,
        weight,
        tf,
      });
    }
  }
}

/**
 * Extract Turn-granular multi-role inverted postings from a SessionEpisode.
 * @param {object} episode
 * @returns {Array<object>}
 */
export function extractPostingsForEpisode(episode) {
  const postingsMap = new Map();
  const epSeqFrom = Number(episode.seq_from ?? 0);
  const epSeqTo = Number(episode.seq_to ?? epSeqFrom);

  // Episode-level summary metadata (turn_idx = -1)
  addRoleTerms(postingsMap, episode.phase_intent, {
    turnIdx: -1,
    seqFrom: epSeqFrom,
    seqTo: epSeqTo,
    role: "intent",
  });
  for (const dec of episode.decisions || []) {
    addRoleTerms(postingsMap, dec, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "decision",
    });
  }
  if (episode.dsh_summary) {
    addRoleTerms(postingsMap, episode.dsh_summary, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "decision",
    });
  }
  for (const fp of episode.touched_files || []) {
    addRoleTerms(postingsMap, fp, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "file",
    });
  }
  for (const sym of episode.touched_symbols || []) {
    addRoleTerms(postingsMap, sym, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "symbol",
    });
  }
  for (const err of episode.error_signatures || []) {
    addRoleTerms(postingsMap, err, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "error",
    });
  }
  for (const cmd of episode.verification_cmds || []) {
    addRoleTerms(postingsMap, cmd, {
      turnIdx: -1,
      seqFrom: epSeqFrom,
      seqTo: epSeqTo,
      role: "command",
    });
  }

  // Turn-level postings for pinpointing exact segments within the episode
  const turns = Array.isArray(episode.turns) ? episode.turns : [];
  for (const t of turns) {
    const tIdx = Number(t.turn_idx ?? 0);
    const sFrom = Number(t.seq_from ?? epSeqFrom);
    const sTo = Number(t.seq_to ?? sFrom);
    addRoleTerms(postingsMap, t.user_prompt, {
      turnIdx: tIdx,
      seqFrom: sFrom,
      seqTo: sTo,
      role: "intent",
    });
    addRoleTerms(postingsMap, t.assistant_summary, {
      turnIdx: tIdx,
      seqFrom: sFrom,
      seqTo: sTo,
      role: "decision",
    });
    for (const dec of t.decisions || []) {
      addRoleTerms(postingsMap, dec, {
        turnIdx: tIdx,
        seqFrom: sFrom,
        seqTo: sTo,
        role: "decision",
      });
    }
    for (const fp of t.files || []) {
      addRoleTerms(postingsMap, fp, {
        turnIdx: tIdx,
        seqFrom: sFrom,
        seqTo: sTo,
        role: "file",
      });
    }
    for (const sym of t.symbols || []) {
      addRoleTerms(postingsMap, sym, {
        turnIdx: tIdx,
        seqFrom: sFrom,
        seqTo: sTo,
        role: "symbol",
      });
    }
    for (const err of t.errors || []) {
      addRoleTerms(postingsMap, err, {
        turnIdx: tIdx,
        seqFrom: sFrom,
        seqTo: sTo,
        role: "error",
      });
    }
    for (const cmd of t.commands || []) {
      addRoleTerms(postingsMap, cmd, {
        turnIdx: tIdx,
        seqFrom: sFrom,
        seqTo: sTo,
        role: "command",
      });
    }
  }

  // Diff snippets
  for (const ds of episode.diff_snippets || []) {
    const tIdx = Number(ds.turn_idx ?? -1);
    const sFrom = Number(ds.seq ?? epSeqFrom);
    addRoleTerms(postingsMap, `${ds.file || ""} ${ds.snippet || ""}`, {
      turnIdx: tIdx,
      seqFrom: sFrom,
      seqTo: sFrom,
      role: "diff",
    });
  }

  // Step-level exact tool outputs & commands (recovers pruned middle sections!)
  for (const st of episode.steps || []) {
    const tIdx = Number(st.turn_idx ?? -1);
    const seq = Number(st.seq ?? st.step_idx ?? epSeqFrom);
    if (st.target_file) {
      addRoleTerms(postingsMap, st.target_file, {
        turnIdx: tIdx,
        seqFrom: seq,
        seqTo: seq,
        role: "file",
      });
    }
    for (const sym of st.symbols_touched || []) {
      addRoleTerms(postingsMap, sym, {
        turnIdx: tIdx,
        seqFrom: seq,
        seqTo: seq,
        role: "symbol",
      });
    }
    if (st.command) {
      addRoleTerms(postingsMap, st.command, {
        turnIdx: tIdx,
        seqFrom: seq,
        seqTo: seq,
        role: "command",
      });
    }
    if (st.error_signature) {
      addRoleTerms(postingsMap, st.error_signature, {
        turnIdx: tIdx,
        seqFrom: seq,
        seqTo: seq,
        role: "error",
      });
    }
    if (st.output_full || st.output_snippet) {
      addRoleTerms(postingsMap, st.output_full || st.output_snippet, {
        turnIdx: tIdx,
        seqFrom: seq,
        seqTo: seq,
        role: "raw_output",
      });
    }
  }

  return Array.from(postingsMap.values());
}

/**
 * Extract multi-role inverted postings from a compiled TracePlaybook.
 * @param {object} pb
 * @returns {Array<object>}
 */
export function extractPostingsForPlaybook(pb) {
  const postingsMap = new Map();
  const base = { turnIdx: 0, seqFrom: 0, seqTo: 0 };

  addRoleTerms(postingsMap, pb.problem_intent, { ...base, role: "intent" });
  addRoleTerms(postingsMap, `${pb.semantic_summary || ""} ${pb.root_cause || ""}`, {
    ...base,
    role: "decision",
  });
  for (const f of pb.touched_files || []) {
    addRoleTerms(postingsMap, f, { ...base, role: "file" });
  }
  for (const s of pb.touched_symbols || []) {
    addRoleTerms(postingsMap, s, { ...base, role: "symbol" });
  }
  for (const e of pb.error_signatures || []) {
    addRoleTerms(postingsMap, e, { ...base, role: "error" });
  }
  for (const r of pb.l2_causal_repairs || []) {
    addRoleTerms(postingsMap, r.error_signature, { ...base, role: "error" });
    addRoleTerms(postingsMap, `${r.repair_action || ""} ${r.verification_cmd || ""}`, {
      ...base,
      role: "command",
    });
  }
  for (const cmd of pb.l1_verification_cmds || []) {
    addRoleTerms(postingsMap, cmd, { ...base, role: "command" });
  }
  for (const step of pb.l0_critical_path || []) {
    if (step.diff_summary) {
      addRoleTerms(postingsMap, step.diff_summary, { ...base, role: "diff" });
    }
    if (step.rationale) {
      addRoleTerms(postingsMap, step.rationale, { ...base, role: "decision" });
    }
  }

  return Array.from(postingsMap.values());
}

/**
 * Ensure Episode & Inverted Index SQLite tables exist inside `index.db`.
 * @param {import("node:sqlite").DatabaseSync} db
 */
export function ensureInvertedSchema(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS episodes (
      episode_id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL,
      compaction_id TEXT,
      trigger TEXT,
      seq_from INTEGER,
      seq_to INTEGER,
      created_at TEXT,
      last_accessed_at TEXT,
      phase_intent TEXT,
      touched_files_json TEXT,
      touched_symbols_json TEXT,
      error_signatures_json TEXT,
      byte_size INTEGER DEFAULT 0,
      payload_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_episodes_session
      ON episodes(session_id, seq_from);
    CREATE INDEX IF NOT EXISTS idx_episodes_lru
      ON episodes(last_accessed_at, created_at);
    CREATE INDEX IF NOT EXISTS idx_episodes_compaction
      ON episodes(compaction_id);

    CREATE TABLE IF NOT EXISTS inverted_postings (
      term TEXT NOT NULL,
      doc_type TEXT NOT NULL,
      doc_id TEXT NOT NULL,
      turn_idx INTEGER NOT NULL,
      seq_from INTEGER NOT NULL,
      seq_to INTEGER NOT NULL,
      field_role TEXT NOT NULL,
      weight REAL NOT NULL,
      tf INTEGER NOT NULL,
      PRIMARY KEY (term, doc_type, doc_id, turn_idx, field_role)
    );
    CREATE INDEX IF NOT EXISTS idx_postings_term
      ON inverted_postings(term, doc_type);
    CREATE INDEX IF NOT EXISTS idx_postings_doc
      ON inverted_postings(doc_type, doc_id);
  `);
}

export function deleteDocumentPostings(db, docType, docId) {
  db.prepare("DELETE FROM inverted_postings WHERE doc_type = ? AND doc_id = ?").run(
    docType,
    docId
  );
}

export function upsertDocumentPostings(db, docType, docId, postings) {
  deleteDocumentPostings(db, docType, docId);
  if (!postings || postings.length === 0) return;
  const stmt = db.prepare(
    `INSERT OR REPLACE INTO inverted_postings
     (term, doc_type, doc_id, turn_idx, seq_from, seq_to, field_role, weight, tf)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  for (const p of postings) {
    stmt.run(
      p.term,
      docType,
      docId,
      p.turn_idx,
      p.seq_from,
      p.seq_to,
      p.field_role,
      p.weight,
      p.tf
    );
  }
}

export function upsertEpisodeToDb(db, episode, byteSize = 0) {
  ensureInvertedSchema(db);
  const dict = typeof episode.toDict === "function" ? episode.toDict() : episode;
  db.prepare(
    `INSERT OR REPLACE INTO episodes
     (episode_id, session_id, compaction_id, trigger, seq_from, seq_to,
      created_at, last_accessed_at, phase_intent, touched_files_json,
      touched_symbols_json, error_signatures_json, byte_size, payload_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    dict.episode_id,
    dict.session_id || "default",
    dict.compaction_id || "",
    dict.trigger || "compaction",
    Number(dict.seq_from ?? 0),
    Number(dict.seq_to ?? 0),
    dict.created_at || "",
    dict.last_accessed_at || dict.created_at || "",
    dict.phase_intent || "",
    JSON.stringify(dict.touched_files || []),
    JSON.stringify(dict.touched_symbols || []),
    JSON.stringify(dict.error_signatures || []),
    Number(byteSize || 0),
    JSON.stringify(dict)
  );
  const postings = extractPostingsForEpisode(dict);
  upsertDocumentPostings(db, "episode", dict.episode_id, postings);
}

export function deleteEpisodeFromDb(db, episodeId) {
  ensureInvertedSchema(db);
  db.prepare("DELETE FROM episodes WHERE episode_id = ?").run(episodeId);
  deleteDocumentPostings(db, "episode", episodeId);
}

/**
 * Query the unified inverted index across Episodes and/or Playbooks.
 * Returns ranked L0 hit records with matched terms, semantic roles, and Turn/Seq spans.
 */
export function queryInvertedIndex(
  db,
  {
    query = "",
    files = [],
    symbols = [],
    errors = [],
    roles = [],
    scope = "all",
    sessionId = null,
    limit = 5,
  } = {}
) {
  ensureInvertedSchema(db);

  const queryTerms = new Set();
  for (const t of tokenizeInvertedTerms(query).keys()) queryTerms.add(t);
  for (const f of files || []) {
    for (const t of tokenizeInvertedTerms(f).keys()) queryTerms.add(t);
  }
  for (const s of symbols || []) {
    for (const t of tokenizeInvertedTerms(s).keys()) queryTerms.add(t);
  }
  for (const e of errors || []) {
    for (const t of tokenizeInvertedTerms(e).keys()) queryTerms.add(t);
  }

  const normalizedFiles = (files || []).map((f) => String(f).trim().toLowerCase()).filter(Boolean);
  const normalizedSymbols = (symbols || []).map((s) => String(s).trim().toLowerCase()).filter(Boolean);
  const normalizedErrors = (errors || []).map((e) => String(e).trim().toLowerCase()).filter(Boolean);
  const roleFilter = new Set((roles || []).map((r) => String(r).trim().toLowerCase()).filter(Boolean));

  const docTypes =
    scope === "episodes"
      ? ["episode"]
      : scope === "playbooks"
        ? ["playbook"]
        : ["episode", "playbook"];

  // If no terms were given but sessionId/scope filter was provided, return recent records
  if (queryTerms.size === 0) {
    return listRecentDocs(db, { docTypes, sessionId, limit });
  }

  const epCntRow = db.prepare("SELECT COUNT(*) AS cnt FROM episodes").get();
  const pbCntRow = db.prepare("SELECT COUNT(*) AS cnt FROM playbooks").get();
  const totalDocs = Math.max(1, Number(epCntRow?.cnt || 0) + Number(pbCntRow?.cnt || 0));

  const termsList = Array.from(queryTerms).slice(0, 64);
  const termPlaceholders = termsList.map(() => "?").join(", ");
  const typePlaceholders = docTypes.map(() => "?").join(", ");

  const rows = db
    .prepare(
      `SELECT term, doc_type, doc_id, turn_idx, seq_from, seq_to, field_role, weight, tf
       FROM inverted_postings
       WHERE term IN (${termPlaceholders}) AND doc_type IN (${typePlaceholders})`
    )
    .all(...termsList, ...docTypes);

  if (rows.length === 0) return [];

  // Precompute document frequency per term
  const dfByTerm = new Map();
  for (const r of rows) {
    let set = dfByTerm.get(r.term);
    if (!set) {
      set = new Set();
      dfByTerm.set(r.term, set);
    }
    set.add(`${r.doc_type}:${r.doc_id}`);
  }

  // Aggregate per doc and per turn
  const docAgg = new Map();
  for (const r of rows) {
    if (roleFilter.size > 0 && !roleFilter.has(r.field_role)) continue;

    const docKey = `${r.doc_type}:${r.doc_id}`;
    let agg = docAgg.get(docKey);
    if (!agg) {
      agg = {
        doc_type: r.doc_type,
        doc_id: r.doc_id,
        matchedTerms: new Set(),
        matchedRoles: new Set(),
        termMaxScore: new Map(),
        turns: new Map(),
      };
      docAgg.set(docKey, agg);
    }

    const df = dfByTerm.get(r.term)?.size || 1;
    const idf = Math.log(1 + (totalDocs + 1) / (df + 0.5));
    const tfNorm = 1 + Math.log(Math.max(1, Number(r.tf || 1)));
    const postingScore = idf * Number(r.weight || 1.0) * tfNorm;

    agg.matchedTerms.add(r.term);
    agg.matchedRoles.add(r.field_role);
    const prevTermScore = agg.termMaxScore.get(r.term) || 0;
    if (postingScore > prevTermScore) {
      agg.termMaxScore.set(r.term, postingScore);
    }

    if (r.turn_idx >= 0) {
      let tAgg = agg.turns.get(r.turn_idx);
      if (!tAgg) {
        tAgg = {
          turn_idx: r.turn_idx,
          seq_from: r.seq_from,
          seq_to: r.seq_to,
          terms: new Set(),
          roles: new Set(),
          score: 0,
        };
        agg.turns.set(r.turn_idx, tAgg);
      }
      tAgg.seq_from = Math.min(tAgg.seq_from, r.seq_from);
      tAgg.seq_to = Math.max(tAgg.seq_to, r.seq_to);
      tAgg.terms.add(r.term);
      tAgg.roles.add(r.field_role);
      tAgg.score += postingScore;
    }
  }

  const hits = [];
  const epStmt = db.prepare("SELECT * FROM episodes WHERE episode_id = ?");
  const pbStmt = db.prepare("SELECT payload_json FROM playbooks WHERE trace_id = ?");
  const activeQueryTermCount = Math.max(1, dfByTerm.size);

  for (const agg of docAgg.values()) {
    let baseScore = 0;
    for (const sc of agg.termMaxScore.values()) baseScore += sc;
    const denom = termsList.length > 4 ? activeQueryTermCount : termsList.length;
    const coverage = Math.min(1.0, agg.matchedTerms.size / Math.max(1, denom));
    baseScore *= 0.6 + 0.8 * coverage;

    const sortedTurns = Array.from(agg.turns.values()).sort((a, b) => {
      if (b.terms.size !== a.terms.size) return b.terms.size - a.terms.size;
      return b.score - a.score;
    });
    // Locality bonus if multiple query terms hit inside the same turn
    if (sortedTurns.length > 0 && sortedTurns[0].terms.size > 1) {
      baseScore += sortedTurns[0].terms.size * 1.5;
    }

    if (agg.doc_type === "episode") {
      const row = epStmt.get(agg.doc_id);
      if (!row) continue;
      if (sessionId && row.session_id !== sessionId) continue;

      const epFiles = safeJsonArray(row.touched_files_json);
      const epSyms = safeJsonArray(row.touched_symbols_json);
      const epErrs = safeJsonArray(row.error_signatures_json);

      let epObj = null;
      try {
        epObj = row.payload_json ? JSON.parse(row.payload_json) : null;
      } catch (_err) {
        epObj = null;
      }
      const allKnownFiles = Array.from(
        new Set([...epFiles, ...(Array.isArray(epObj?.referenced_files) ? epObj.referenced_files : [])])
      );

      if (normalizedFiles.length > 0) {
        const hitF = allKnownFiles.some((f) =>
          normalizedFiles.some((nf) => f.toLowerCase().includes(nf) || nf.includes(f.toLowerCase()))
        );
        if (!hitF) continue;
        baseScore += 6.0;
      }
      if (normalizedSymbols.length > 0) {
        const hitS = epSyms.some((s) =>
          normalizedSymbols.some((ns) => s.toLowerCase() === ns || s.toLowerCase().includes(ns))
        );
        if (!hitS) continue;
        baseScore += 5.0;
      }
      if (normalizedErrors.length > 0) {
        const hitE = epErrs.some((e) =>
          normalizedErrors.some((ne) => e.toLowerCase().includes(ne) || ne.includes(e.toLowerCase()))
        );
        if (!hitE) continue;
        baseScore += 6.0;
      }
      const turnPromptMap = new Map();
      for (const t of epObj?.turns || []) {
        turnPromptMap.set(Number(t.turn_idx), String(t.user_prompt || "").slice(0, 100));
      }

      hits.push({
        doc_type: "episode",
        doc_id: row.episode_id,
        session_id: row.session_id,
        compaction_id: row.compaction_id || "",
        trigger: row.trigger || "compaction",
        phase_index: Number(epObj?.phase_index ?? 1),
        prev_episode_id: epObj?.prev_episode_id || null,
        seq_from: Number(row.seq_from ?? 0),
        seq_to: Number(row.seq_to ?? 0),
        created_at: row.created_at || "",
        intent: row.phase_intent || "",
        touched_files: epFiles,
        touched_symbols: epSyms,
        error_signatures: epErrs,
        matched_terms: Array.from(agg.matchedTerms).sort().slice(0, 10),
        matched_roles: Array.from(agg.matchedRoles).sort(),
        matched_turns: sortedTurns.slice(0, 3).map((t) => ({
          turn_idx: t.turn_idx,
          seq_from: t.seq_from,
          seq_to: t.seq_to,
          prompt_preview: turnPromptMap.get(t.turn_idx) || "",
          matched_terms: Array.from(t.terms).sort().slice(0, 6),
        })),
        score: Math.round(baseScore * 100) / 100,
      });
    } else {
      const row = pbStmt.get(agg.doc_id);
      if (!row || !row.payload_json) continue;
      let pb;
      try {
        pb = JSON.parse(row.payload_json);
      } catch (_err) {
        continue;
      }
      if (pb.status === "invalidated") continue;
      if (sessionId && !(pb.source_session_ids || []).includes(sessionId)) continue;

      const pbFiles = pb.touched_files || [];
      const pbSyms = pb.touched_symbols || [];
      const pbErrs = pb.error_signatures || [];

      if (normalizedFiles.length > 0) {
        const hitF = pbFiles.some((f) =>
          normalizedFiles.some((nf) => f.toLowerCase().includes(nf) || nf.includes(f.toLowerCase()))
        );
        if (!hitF) continue;
        baseScore += 6.0;
      }
      if (normalizedSymbols.length > 0) {
        const hitS = pbSyms.some((s) =>
          normalizedSymbols.some((ns) => s.toLowerCase() === ns || s.toLowerCase().includes(ns))
        );
        if (!hitS) continue;
        baseScore += 5.0;
      }
      if (normalizedErrors.length > 0) {
        const hitE = pbErrs.some((e) =>
          normalizedErrors.some((ne) => e.toLowerCase().includes(ne) || ne.includes(e.toLowerCase()))
        );
        if (!hitE) continue;
        baseScore += 6.0;
      }

      hits.push({
        doc_type: "playbook",
        doc_id: pb.trace_id,
        session_id: (pb.source_session_ids || []).slice(-1)[0] || "",
        compaction_id: "",
        trigger: "playbook",
        seq_from: 0,
        seq_to: 0,
        created_at: pb.created_at || "",
        intent: pb.problem_intent || "",
        touched_files: pbFiles,
        touched_symbols: pbSyms,
        error_signatures: pbErrs,
        matched_terms: Array.from(agg.matchedTerms).sort().slice(0, 10),
        matched_roles: Array.from(agg.matchedRoles).sort(),
        matched_turns: [],
        score: Math.round(baseScore * 100) / 100,
      });
    }
  }

  hits.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return b.created_at.localeCompare(a.created_at);
  });

  return hits.slice(0, limit);
}

function listRecentDocs(db, { docTypes, sessionId, limit }) {
  const hits = [];
  if (docTypes.includes("episode")) {
    const rows = sessionId
      ? db
          .prepare(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY created_at DESC, seq_to DESC LIMIT ?"
          )
          .all(sessionId, limit)
      : db
          .prepare("SELECT * FROM episodes ORDER BY created_at DESC, seq_to DESC LIMIT ?")
          .all(limit);
    for (const row of rows) {
      let epObj = null;
      try {
        epObj = row.payload_json ? JSON.parse(row.payload_json) : null;
      } catch (_err) {
        epObj = null;
      }
      hits.push({
        doc_type: "episode",
        doc_id: row.episode_id,
        session_id: row.session_id,
        compaction_id: row.compaction_id || "",
        trigger: row.trigger || "compaction",
        phase_index: Number(epObj?.phase_index ?? 1),
        prev_episode_id: epObj?.prev_episode_id || null,
        seq_from: Number(row.seq_from ?? 0),
        seq_to: Number(row.seq_to ?? 0),
        created_at: row.created_at || "",
        intent: row.phase_intent || "",
        touched_files: safeJsonArray(row.touched_files_json),
        touched_symbols: safeJsonArray(row.touched_symbols_json),
        error_signatures: safeJsonArray(row.error_signatures_json),
        matched_terms: [],
        matched_roles: [],
        matched_turns: [],
        score: 1.0,
      });
    }
  }
  if (docTypes.includes("playbook") && hits.length < limit) {
    const rows = db
      .prepare(
        "SELECT payload_json FROM playbooks WHERE status != 'invalidated' ORDER BY created_at DESC LIMIT ?"
      )
      .all(limit);
    for (const row of rows) {
      try {
        const pb = JSON.parse(row.payload_json);
        if (sessionId && !(pb.source_session_ids || []).includes(sessionId)) continue;
        hits.push({
          doc_type: "playbook",
          doc_id: pb.trace_id,
          session_id: (pb.source_session_ids || []).slice(-1)[0] || "",
          compaction_id: "",
          trigger: "playbook",
          seq_from: 0,
          seq_to: 0,
          created_at: pb.created_at || "",
          intent: pb.problem_intent || "",
          touched_files: pb.touched_files || [],
          touched_symbols: pb.touched_symbols || [],
          error_signatures: pb.error_signatures || [],
          matched_terms: [],
          matched_roles: [],
          matched_turns: [],
          score: 0.8,
        });
      } catch (_err) {
        // ignore
      }
    }
  }
  return hits.slice(0, limit);
}

function safeJsonArray(raw) {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_err) {
    return [];
  }
}
