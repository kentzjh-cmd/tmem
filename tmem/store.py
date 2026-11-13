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
            desired_lines = [".cache/", "runtime_state.json", "telemetry.jsonl"]
            if not gitignore.exists():
                gitignore.write_text("\n".join(desired_lines) + "\n", encoding="utf-8")
            else:
                cur_gi = gitignore.read_text(encoding="utf-8", errors="ignore")
                missing = [ln for ln in desired_lines if ln not in cur_gi]
                if missing:
                    gitignore.write_text(
                        cur_gi.rstrip() + "\n" + "\n".join(missing) + "\n",
                        encoding="utf-8",
                    )
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

    @staticmethod
    def _token_jaccard(text_a: str, text_b: str) -> float:
        toks_a = {
            t.lower()
            for t in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text_a or "")
            if len(t) >= 2 and t.lower() not in COMMON_QUERY_STOPWORDS
        }
        toks_b = {
            t.lower()
            for t in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text_b or "")
            if len(t) >= 2 and t.lower() not in COMMON_QUERY_STOPWORDS
        }
        if not toks_a or not toks_b:
            return 0.0
        return len(toks_a & toks_b) / len(toks_a | toks_b)

    @staticmethod
    def _set_jaccard(a: List[str], b: List[str]) -> float:
        sa = {x.strip().lower() for x in a if x and x.strip()}
        sb = {x.strip().lower() for x in b if x and x.strip()}
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def _find_similar_playbook(self, incoming: TracePlaybook) -> Optional[TracePlaybook]:
        """Find an existing non-invalidated playbook sharing the same L2 error repair or task file set."""
        in_errs = {e.strip().lower() for e in incoming.error_signatures if e and e.strip()}
        in_repairs = {
            (r.error_signature.strip().lower(), r.repair_action.strip().lower())
            for r in incoming.l2_causal_repairs
        }
        for existing in self.get_all_project_playbooks():
            if existing.trace_id == incoming.trace_id or existing.status == "invalidated":
                continue

            file_sim = self._set_jaccard(existing.touched_files, incoming.touched_files)

            # Rule A: Same L2 causal error repair signature + matching repair or file scope
            ex_errs = {e.strip().lower() for e in existing.error_signatures if e and e.strip()}
            ex_repairs = {
                (r.error_signature.strip().lower(), r.repair_action.strip().lower())
                for r in existing.l2_causal_repairs
            }
            if in_repairs and ex_repairs and (in_repairs & ex_repairs):
                if file_sim >= 0.5 or any(r.is_env_or_toolchain for r in incoming.l2_causal_repairs):
                    return existing
            if in_errs and ex_errs and (in_errs & ex_errs) and file_sim >= 0.6:
                return existing

            # Rule B: High file-set overlap (>=0.8 Jaccard) + matching intent semantics (>=0.45)
            if (
                incoming.touched_files
                and existing.touched_files
                and file_sim >= 0.8
                and self._token_jaccard(existing.problem_intent, incoming.problem_intent) >= 0.45
            ):
                return existing
        return None

    @staticmethod
    def _merge_playbooks(existing: TracePlaybook, incoming: TracePlaybook) -> TracePlaybook:
        """Merge an incoming session playbook in place into an existing matching playbook."""
        def _union(seq_a: List[str], seq_b: List[str]) -> List[str]:
            return list(dict.fromkeys([x for x in (seq_a + seq_b) if x]))

        existing.created_at = incoming.created_at or existing.created_at
        existing.git_commit = incoming.git_commit or existing.git_commit
        existing.status = "active"
        existing.guard_fail_streak = 0
        existing.merge_count = int(existing.merge_count) + 1
        existing.confidence = min(1.0, round(max(existing.confidence, incoming.confidence) + 0.08, 3))
        existing.is_trivial = existing.is_trivial and incoming.is_trivial

        existing.error_signatures = _union(existing.error_signatures, incoming.error_signatures)
        existing.source_session_ids = _union(existing.source_session_ids, incoming.source_session_ids)
        existing.touched_files = _union(existing.touched_files, incoming.touched_files)
        existing.touched_symbols = _union(existing.touched_symbols, incoming.touched_symbols)
        existing.l1_cochange_topology = _union(
            existing.l1_cochange_topology, incoming.l1_cochange_topology
        )
        existing.l1_verification_cmds = _union(
            existing.l1_verification_cmds, incoming.l1_verification_cmds
        )
        existing.pitfalls = _union(existing.pitfalls, incoming.pitfalls)

        seen_rep = {
            (r.error_signature.strip().lower(), r.repair_action.strip().lower())
            for r in existing.l2_causal_repairs
        }
        for r in incoming.l2_causal_repairs:
            key = (r.error_signature.strip().lower(), r.repair_action.strip().lower())
            if key not in seen_rep:
                seen_rep.add(key)
                existing.l2_causal_repairs.append(r)

        # Refresh L0 path/guards when incoming carries equal or richer verified detail
        if incoming.guards and len(incoming.l0_critical_path) >= len(existing.l0_critical_path):
            existing.guards = incoming.guards
            existing.l0_critical_path = incoming.l0_critical_path
            existing.raw_steps_count = incoming.raw_steps_count or existing.raw_steps_count
            existing.sliced_steps_count = incoming.sliced_steps_count or existing.sliced_steps_count
            if incoming.raw_trace_path:
                existing.raw_trace_path = incoming.raw_trace_path
                existing.raw_trace_hash = incoming.raw_trace_hash

        if incoming.root_cause and (not existing.root_cause or len(incoming.root_cause) > len(existing.root_cause)):
            existing.root_cause = incoming.root_cause
        if incoming.semantic_summary and not existing.semantic_summary:
            existing.semantic_summary = incoming.semantic_summary
        return existing

    def save_playbook(
        self,
        playbook: TracePlaybook,
        target_scope: str = "project",
        merge_similar: bool = True,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Save playbook to Project Tier (.tmem/traces/<id>.json) and index into SQLite.
        When `merge_similar=True`, merges into an existing playbook sharing the same L2 repair
        or high-overlap file set + intent instead of spawning duplicate files.
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
        if merge_similar:
            similar = self._find_similar_playbook(playbook)
            if similar is not None:
                playbook = self._merge_playbooks(similar, playbook)

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
        step_rats = " ".join(str(s.get("rationale") or "") for s in pb.l0_critical_path)
        intent_and_summary = (
            f"{pb.problem_intent} {pb.semantic_summary} {pb.root_cause or ''} {step_rats}"
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

    def lookup_l2_repair(
        self,
        error_signatures: List[str],
        file_paths: Optional[List[str]] = None,
        limit: int = 2,
    ) -> List[EvaluatedPlaybook]:
        """
        Strict error-signature lookup for DSH post_tool_use interception.
        Requires deterministic error match (exact_triggers type='error') and non-empty L2 repairs.
        """
        sigs = [s.strip() for s in (error_signatures or []) if s and len(s.strip()) >= 6]
        if not sigs:
            return []
        self.sync_project_index_if_needed()
        if not self.project_db_path.exists():
            return []

        scores: Dict[str, float] = {}
        with sqlite3.connect(str(self.project_db_path)) as conn:
            for err in sigs:
                err_low = err.lower()
                cur = conn.execute(
                    "SELECT trace_id, trigger_key FROM exact_triggers "
                    "WHERE trigger_type = 'error' AND (? LIKE '%' || trigger_key || '%' OR trigger_key LIKE '%' || ? || '%')",
                    (err_low, err_low),
                )
                for tid, tkey in cur.fetchall():
                    if len(tkey or "") < 6:
                        continue
                    boost = 15.0 if tkey == err_low else 8.0
                    scores[tid] = scores.get(tid, 0.0) + boost

            if not scores:
                return []

            file_set = {f.lower() for f in (file_paths or []) if f}
            results: List[Tuple[EvaluatedPlaybook, float]] = []
            for tid, sc in scores.items():
                row = conn.execute(
                    "SELECT payload_json FROM playbooks WHERE trace_id = ?", (tid,)
                ).fetchone()
                if not row:
                    continue
                try:
                    pb = TracePlaybook.from_dict(json.loads(row[0]))
                except Exception:
                    continue
                if pb.status == "invalidated" or not pb.l2_causal_repairs:
                    continue
                if file_set and any(f.lower() in file_set for f in pb.touched_files):
                    sc += 3.0
                ev = evaluate_guards(pb, self.workspace_root)
                results.append((ev, sc))

        results.sort(
            key=lambda item: (round(item[1], 2), 1 if item[0].l0_valid else 0, item[0].playbook.confidence),
            reverse=True,
        )
        return [ev for ev, _ in results[:limit]]

    def run_metabolism_gc(self, prune_stale: bool = False) -> Dict[str, int]:
        """
        Scan project traces against current workspace state:
        - Resets `guard_fail_streak` when L0 guards pass.
        - Increments `guard_fail_streak` on guard drift, auto-degrading L1/L2 traces or marking stale.
        - When `prune_stale=True`, removes expired stale traces without L2 invariants.
        """
        self.sync_project_index_if_needed()
        stats = {"active_l0": 0, "degraded_l1_l2": 0, "stale": 0, "pruned": 0}
        if not self.project_traces_dir.exists():
            return stats

        with sqlite3.connect(str(self.project_db_path)) as conn:
            for jf in list(self.project_traces_dir.glob("*.json")):
                try:
                    pb = TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                    ev = evaluate_guards(pb, self.workspace_root)
                    from tmem.slicer import assess_playbook_triviality
                    is_triv, _reason = assess_playbook_triviality(pb)
                    pb.is_trivial = is_triv
                    if ev.l0_valid:
                        pb.status = "active"
                        pb.guard_fail_streak = 0
                        stats["active_l0"] += 1
                    else:
                        pb.guard_fail_streak = int(pb.guard_fail_streak) + 1
                        if (pb.l2_causal_repairs or pb.l1_cochange_topology) and pb.guard_fail_streak < 3:
                            pb.status = "active"
                            pb.confidence = max(0.2, round(pb.confidence - 0.05, 3))
                            stats["degraded_l1_l2"] += 1
                        else:
                            pb.status = "stale"
                            pb.confidence = max(0.05, round(pb.confidence - 0.3, 3))
                            stats["stale"] += 1
                            if prune_stale and not pb.l2_causal_repairs and (
                                pb.guard_fail_streak >= 3 or pb.confidence <= 0.1
                            ):
                                jf.unlink(missing_ok=True)
                                conn.execute("DELETE FROM playbooks WHERE trace_id = ?", (pb.trace_id,))
                                conn.execute("DELETE FROM exact_triggers WHERE trace_id = ?", (pb.trace_id,))
                                conn.execute("DELETE FROM playbooks_fts WHERE trace_id = ?", (pb.trace_id,))
                                conn.execute("DELETE FROM trace_links WHERE trace_id = ?", (pb.trace_id,))
                                stats["pruned"] += 1
                                continue
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
                        "source_session_ids": pb.source_session_ids,
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

    def record_trace_outcome(self, trace_ids: List[str], outcome: str) -> Dict[str, float]:
        """
        Update playbook confidence, hit/success/contradict counters, and status
        based on deterministic runtime outcomes ('success' | 'contradict') without LLM calls.
        """
        if not trace_ids or outcome not in ("success", "contradict"):
            return {}
        if not self.project_traces_dir.exists():
            return {}

        self.sync_project_index_if_needed()
        updated: Dict[str, float] = {}
        try:
            with sqlite3.connect(str(self.project_db_path)) as conn:
                for tid in dict.fromkeys(trace_ids):
                    jf = self.project_traces_dir / f"{tid}.json"
                    if not jf.exists():
                        continue
                    try:
                        pb = TracePlaybook.from_dict(
                            json.loads(jf.read_text(encoding="utf-8"))
                        )
                    except Exception:
                        continue

                    pb.hit_count += 1
                    if outcome == "success":
                        pb.success_count += 1
                        pb.confidence = min(1.0, round(pb.confidence + 0.1, 3))
                        if pb.status == "stale" and pb.confidence >= 0.4:
                            pb.status = "active"
                    else:
                        pb.contradict_count += 1
                        pb.confidence = max(0.0, round(pb.confidence - 0.2, 3))
                        if pb.confidence < 0.3 or pb.contradict_count >= 2:
                            pb.status = "stale"

                    jf.write_text(
                        json.dumps(pb.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    self._UpsertToDb(conn, pb)
                    updated[tid] = pb.confidence
        except Exception as e:
            logger.debug("Failed to record trace outcome: %s", e)

        return updated


COMMON_QUERY_STOPWORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "as", "is", "was", "are", "were", "it", "this", "that", "when", "if", "be", "not", "have",
    "has", "had", "do", "does", "did", "so", "can", "we", "you", "my", "your", "i", "me", "our",
    "all", "any", "some", "what", "which", "who", "whom", "how", "there", "their", "then", "into",
}

DualTierMemoryStore = ProjectMemoryStore  # Backward compatibility alias
