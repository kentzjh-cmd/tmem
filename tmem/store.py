"""
Repository-Scoped Storage & Retrieval Engine (.tmem/).

Architectural inspirations absorbed:
- Beads (bd): Git-tracked per-trace files (.tmem/traces/<id>.json) for zero merge conflicts,
  paired with a gitignored local SQLite FTS5 index (.tmem/.cache/index.db).
- Zep / Graphiti: Bi-temporal lifecycle & deterministic invalidation when Git files drift.
- Strict Repository Boundary: Execution traces, AST symbols, and verification contracts are strictly
  repository-scoped (.tmem/) to eliminate cross-project negative transfer and hallucinated file targets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tmem.guards import EvaluatedPlaybook, evaluate_guards
from tmem.mem_doc import sync_memories_file
from tmem.models import CausalRepairPair, RawExecutionTrace, TracePlaybook
from tmem.raw_storage import RawTraceStore


class ProjectMemoryStore:
    """Manages Project-level (.tmem/) execution trace store and local SQLite FTS5 index."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = str(Path(workspace_root).resolve())
        self.project_dir = Path(self.workspace_root) / ".tmem"
        self.project_traces_dir = self.project_dir / "traces"
        self.project_raw_dir = self.project_dir / "raw"
        self.project_cache_dir = self.project_dir / ".cache"
        self.project_db_path = self.project_cache_dir / "index.db"
        self.raw_store = RawTraceStore(self.workspace_root)

    def ensure_initialized(self, init_project: bool = True) -> None:
        """Create directory hierarchy and gitignore for local cache."""
        if init_project:
            self.project_traces_dir.mkdir(parents=True, exist_ok=True)
            self.project_raw_dir.mkdir(parents=True, exist_ok=True)
            self.project_cache_dir.mkdir(parents=True, exist_ok=True)
            gitignore = self.project_dir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(".cache/\nruntime_state.json\n", encoding="utf-8")
            self._init_sqlite(self.project_db_path)

    def _init_sqlite(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(
                """
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
                    trigger_type TEXT, -- 'error' | 'file' | 'symbol'
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
                """
            )

    def sync_project_index_if_needed(self) -> None:
        """Rebuild local SQLite cache if git-tracked .tmem/traces/*.json changed."""
        if not self.project_traces_dir.exists():
            return
        self.project_cache_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite(self.project_db_path)

        json_files = list(self.project_traces_dir.glob("*.json"))
        disk_ids = {jf.stem for jf in json_files}
        with sqlite3.connect(str(self.project_db_path)) as conn:
            cur = conn.execute("SELECT trace_id FROM playbooks")
            db_ids = {row[0] for row in cur.fetchall()}

            # Remove orphaned traces that were deleted from .tmem/traces/
            orphaned_ids = db_ids - disk_ids
            for oid in orphaned_ids:
                conn.execute("DELETE FROM playbooks WHERE trace_id = ?", (oid,))
                conn.execute("DELETE FROM exact_triggers WHERE trace_id = ?", (oid,))
                conn.execute("DELETE FROM playbooks_fts WHERE trace_id = ?", (oid,))
                try:
                    conn.execute("DELETE FROM trace_links WHERE trace_id = ?", (oid,))
                except Exception:
                    pass

            # Upsert missing or out-of-sync JSON trace files
            if db_ids != disk_ids:
                for jf in json_files:
                    try:
                        pb = TracePlaybook.from_dict(
                            json.loads(jf.read_text(encoding="utf-8"))
                        )
                        self._UpsertToDb(conn, pb)
                    except Exception:
                        continue

    def save_playbook(
        self,
        playbook: TracePlaybook,
        target_scope: str = "project",
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Save playbook to Project Tier (.tmem/traces/<id>.json) and index into SQLite.
        Returns (project_trace_id, None).
        """
        self.ensure_initialized(init_project=True)
        raw_path = self.project_raw_dir / f"{playbook.trace_id}.raw.json"
        if raw_path.exists():
            if not playbook.raw_trace_path:
                playbook.raw_trace_path = f"raw/{playbook.trace_id}.raw.json"
            if not playbook.raw_trace_hash:
                playbook.raw_trace_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()[:16]

        playbook.scope = "project"
        proj_file = self.project_traces_dir / f"{playbook.trace_id}.json"
        proj_file.write_text(
            json.dumps(playbook.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with sqlite3.connect(str(self.project_db_path)) as conn:
            self._UpsertToDb(conn, playbook)
        proj_id = playbook.trace_id
        try:
            self.sync_memories_markdown()
        except Exception:
            pass

        return proj_id, None

    def _UpsertToDb(self, conn: sqlite3.Connection, pb: TracePlaybook) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO playbooks
            (trace_id, scope, status, confidence, hit_count, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pb.trace_id,
                pb.scope,
                pb.status,
                pb.confidence,
                pb.hit_count,
                pb.created_at,
                json.dumps(pb.to_dict(), ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM exact_triggers WHERE trace_id = ?", (pb.trace_id,))
        conn.execute("DELETE FROM playbooks_fts WHERE trace_id = ?", (pb.trace_id,))
        try:
            conn.execute("DELETE FROM trace_links WHERE trace_id = ?", (pb.trace_id,))
        except Exception:
            pass

        for err in pb.error_signatures:
            conn.execute(
                "INSERT OR IGNORE INTO exact_triggers VALUES (?, 'error', ?)",
                (pb.trace_id, err.lower()),
            )
        for f in pb.touched_files:
            conn.execute(
                "INSERT OR IGNORE INTO exact_triggers VALUES (?, 'file', ?)",
                (pb.trace_id, f.lower()),
            )
        for sym in pb.touched_symbols:
            conn.execute(
                "INSERT OR IGNORE INTO exact_triggers VALUES (?, 'symbol', ?)",
                (pb.trace_id, sym.lower()),
            )
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO trace_links VALUES (?, ?)",
                    (sym.lower(), pb.trace_id),
                )
            except Exception:
                pass

        repairs_text = " ".join(
            f"{r.error_signature} {r.repair_action}" for r in pb.l2_causal_repairs
        )
        reqs_text = " ".join(r.text for r in pb.requirements)
        edges_text = " ".join(e.why for e in pb.intent_edges if e.why)
        step_rats = " ".join(str(s.get("rationale") or "") for s in pb.l0_critical_path)
        intent_and_summary = (
            f"{pb.problem_intent} {pb.semantic_summary} {pb.root_cause or ''} {reqs_text} {edges_text} {step_rats}"
        ).strip()
        conn.execute(
            """
            INSERT INTO playbooks_fts
            (trace_id, problem_intent, error_signatures, touched_files, touched_symbols, repairs_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                pb.trace_id,
                intent_and_summary,
                " ".join(pb.error_signatures),
                " ".join(pb.touched_files),
                " ".join(pb.touched_symbols),
                repairs_text,
            ),
        )

    def cascading_lookup(
        self,
        query_text: str = "",
        file_paths: Optional[List[str]] = None,
        error_signatures: Optional[List[str]] = None,
        limit: int = 3,
    ) -> List[EvaluatedPlaybook]:
        """
        Deterministic Retrieval Funnel across Project Repository (.tmem/):
        Stage 1: Exact Error Signature / File Path / Symbol Match
        Stage 2: SQLite FTS5 BM25 Match
        Stage 3: Pre-flight Guard Evaluation & L0->L1->L2 Progressive Degradation
        """
        self.sync_project_index_if_needed()
        if not self.project_db_path.exists():
            return []

        candidates: Dict[str, Tuple[TracePlaybook, float]] = {}
        for pb, score in self._query_single_db(
            self.project_db_path, query_text, file_paths or [], error_signatures or []
        ):
            if pb.trace_id not in candidates and pb.status != "invalidated":
                candidates[pb.trace_id] = (pb, score)

        # Evaluate pre-flight guards on every matched candidate
        evaluated_pairs: List[Tuple[EvaluatedPlaybook, float]] = []
        for pb, score in candidates.values():
            ev = evaluate_guards(pb, self.workspace_root)
            evaluated_pairs.append((ev, score))

        # Rank: Relevance Score (exact match + BM25) > L0 Guard Validity > Confidence
        def _rank_key(item: Tuple[EvaluatedPlaybook, float]) -> Tuple[float, int, float]:
            ev, rel_score = item
            l0_ok = 1 if ev.l0_valid else 0
            return (round(rel_score, 2), l0_ok, ev.playbook.confidence)

        evaluated_pairs.sort(key=_rank_key, reverse=True)
        return [ev for ev, _ in evaluated_pairs[:limit]]

    def _query_single_db(
        self,
        db_path: Path,
        query_text: str,
        file_paths: List[str],
        error_signatures: List[str],
    ) -> List[Tuple[TracePlaybook, float]]:
        scores: Dict[str, float] = {}

        with sqlite3.connect(str(db_path)) as conn:
            # 1. Exact Error triggers
            for err in error_signatures:
                cur = conn.execute(
                    "SELECT trace_id, trigger_key FROM exact_triggers WHERE trigger_type = 'error' AND (? LIKE '%' || trigger_key || '%' OR trigger_key LIKE '%' || ? || '%')",
                    (err.lower(), err.lower()),
                )
                for (tid, tkey) in cur.fetchall():
                    boost = 10.0 if tkey == err.lower() else 5.0
                    scores[tid] = scores.get(tid, 0.0) + boost

            # 2. File triggers
            for fp in file_paths:
                cur = conn.execute(
                    "SELECT trace_id FROM exact_triggers WHERE trigger_type = 'file' AND trigger_key = ?",
                    (fp.lower(),),
                )
                for (tid,) in cur.fetchall():
                    scores[tid] = scores.get(tid, 0.0) + 12.0

            # 3. Direct mention in query_text
            if query_text:
                q_low = query_text.lower()
                cur = conn.execute(
                    "SELECT DISTINCT trace_id FROM exact_triggers WHERE ? LIKE '%' || trigger_key || '%'",
                    (q_low,),
                )
                for (tid,) in cur.fetchall():
                    scores[tid] = scores.get(tid, 0.0) + 6.0

                # 4. FTS5 Multi-Token Keyword Match
                tokens = [
                    t.lower()
                    for t in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", query_text)
                    if len(t) >= 3 and t.lower() not in COMMON_QUERY_STOPWORDS
                ]
                if tokens:
                    cand_tokens = tokens[:15]
                    token_hits: Dict[str, int] = {}
                    for t in cand_tokens:
                        try:
                            cur = conn.execute(
                                "SELECT trace_id FROM playbooks_fts WHERE playbooks_fts MATCH ?",
                                (f'"{t}"',),
                            )
                            for (tid,) in cur.fetchall():
                                token_hits[tid] = token_hits.get(tid, 0) + 1
                        except sqlite3.OperationalError:
                            pass

                    min_hits = 2 if len(cand_tokens) >= 3 else 1
                    for tid, hits in token_hits.items():
                        coverage = hits / len(cand_tokens)
                        if hits >= min_hits and coverage >= 0.20:
                            scores[tid] = scores.get(tid, 0.0) + (hits * 2.0)

            results: List[Tuple[TracePlaybook, float]] = []
            for tid, score in scores.items():
                cur = conn.execute(
                    "SELECT payload_json FROM playbooks WHERE trace_id = ?", (tid,)
                )
                row = cur.fetchone()
                if row:
                    try:
                        results.append((TracePlaybook.from_dict(json.loads(row[0])), score))
                    except Exception:
                        pass

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def run_metabolism_gc(self) -> Dict[str, int]:
        """
        Scan project traces against current workspace state:
        - Marks traces whose L0 guards fail and have no L1/L2 value as 'stale'.
        - Keeps traces with L1/L2 invariants active (auto-degraded at runtime).
        """
        self.sync_project_index_if_needed()
        stats = {"active_l0": 0, "degraded_l1_l2": 0, "stale": 0}
        if not self.project_traces_dir.exists():
            return stats

        with sqlite3.connect(str(self.project_db_path)) as conn:
            for jf in self.project_traces_dir.glob("*.json"):
                try:
                    pb = TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                    ev = evaluate_guards(pb, self.workspace_root)
                    from tmem.slicer import assess_playbook_triviality
                    is_triv, reason = assess_playbook_triviality(pb)
                    pb.is_trivial = is_triv
                    if ev.l0_valid:
                        pb.status = "active"
                        stats["active_l0"] += 1
                    elif pb.l2_causal_repairs or len(pb.l1_cochange_topology) > 0:
                        pb.status = "active"
                        stats["degraded_l1_l2"] += 1
                    else:
                        pb.status = "stale"
                        pb.confidence = max(0.1, pb.confidence - 0.3)
                        stats["stale"] += 1
                    jf.write_text(
                        json.dumps(pb.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    self._UpsertToDb(conn, pb)
                except Exception:
                    continue
        try:
            self.sync_memories_markdown()
        except Exception:
            pass
        return stats

    def list_playbooks(self, scope: str = "project") -> List[Dict[str, object]]:
        """List summary entries of stored project traces."""
        items: List[Dict[str, object]] = []
        if not self.project_traces_dir.exists():
            return items

        for jf in sorted(self.project_traces_dir.glob("*.json")):
            try:
                pb = TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                ev = evaluate_guards(pb, self.workspace_root)
                items.append(
                    {
                        "trace_id": pb.trace_id,
                        "scope": "project",
                        "effective_tier": ev.effective_tier,
                        "problem_intent": pb.problem_intent,
                        "semantic_summary": pb.semantic_summary,
                        "touched_files": pb.touched_files,
                        "l2_repairs_count": len(pb.l2_causal_repairs),
                        "status": pb.status,
                        "created_at": pb.created_at,
                    }
                )
            except Exception:
                continue
        return items

    def delete_playbook(self, trace_id: str) -> Dict[str, object]:
        """Remove a trace by ID (or prefix) from project store."""
        removed_from: List[str] = []
        tid = trace_id.strip()
        if not tid:
            return {"deleted": False, "removed_from": []}

        if self.project_traces_dir.exists():
            for jf in list(self.project_traces_dir.glob("*.json")):
                if jf.stem == tid or jf.stem.startswith(tid):
                    matched_id = jf.stem
                    try:
                        jf.unlink()
                        removed_from.append(f"project:{matched_id}")
                    except Exception:
                        pass
                    if self.project_db_path.exists():
                        try:
                            with sqlite3.connect(str(self.project_db_path)) as conn:
                                conn.execute("DELETE FROM playbooks WHERE trace_id = ?", (matched_id,))
                                conn.execute("DELETE FROM exact_triggers WHERE trace_id = ?", (matched_id,))
                                conn.execute("DELETE FROM playbooks_fts WHERE trace_id = ?", (matched_id,))
                                try:
                                    conn.execute("DELETE FROM trace_links WHERE trace_id = ?", (matched_id,))
                                except Exception:
                                    pass
                        except Exception:
                            pass
        try:
            self.raw_store.delete_raw_trace(tid)
        except Exception:
            pass
        try:
            self.sync_memories_markdown()
        except Exception:
            pass
        return {"deleted": bool(removed_from), "removed_from": removed_from}

    def get_raw_trace(self, trace_id: str) -> Optional[RawExecutionTrace]:
        """Retrieve full-fidelity raw execution trace if stored."""
        return self.raw_store.load_raw_trace(trace_id)

    def get_all_project_playbooks(self) -> List[TracePlaybook]:
        """Load all active project playbooks."""
        if not self.project_traces_dir.exists():
            return []
        playbooks: List[TracePlaybook] = []
        for jf in sorted(self.project_traces_dir.glob("*.json")):
            try:
                playbooks.append(
                    TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                )
            except Exception:
                continue
        return playbooks


    def get_linked_traces(self, symbol: str, exclude_trace_id: str = "") -> List[str]:
        """Query other trace IDs linked by the same symbol."""
        if not symbol or not self.project_db_path.exists():
            return []
        try:
            with sqlite3.connect(str(self.project_db_path)) as conn:
                cur = conn.execute(
                    "SELECT trace_id FROM trace_links WHERE symbol = ? AND trace_id != ? LIMIT 3",
                    (symbol.lower(), exclude_trace_id),
                )
                return [row[0] for row in cur.fetchall()]
        except Exception:
            return []

    def sync_memories_markdown(self) -> Path:
        """Regenerate MEMORIES.md and maintain MEM.md living index in workspace root."""
        proj_pbs = self.get_all_project_playbooks()
        return sync_memories_file(self.workspace_root, proj_pbs)


COMMON_QUERY_STOPWORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "as", "is", "was", "are", "were", "it", "this", "that", "when", "if", "be", "not", "have",
    "has", "had", "do", "does", "did", "so", "can", "we", "you", "my", "your", "i", "me", "our",
    "all", "any", "some", "what", "which", "who", "whom", "how", "there", "their", "then", "into",
}

DualTierMemoryStore = ProjectMemoryStore  # Backward compatibility alias
