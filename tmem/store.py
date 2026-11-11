"""
Dual-Tier Storage & Cascading Retrieval Engine (.tmem/ Project Tier + ~/.tmem/ Global Tier).

Architectural inspirations absorbed:
- Beads (bd): Git-tracked per-trace files (.tmem/traces/<id>.json) for zero merge conflicts,
  paired with a gitignored local SQLite FTS5 index (.tmem/.cache/index.db).
- Zep / Graphiti: Bi-temporal lifecycle & deterministic invalidation when Git files drift.
- Voyager / Reflexion: Skill library with confidence decay & automatic global promotion of L2 invariants.
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


class DualTierMemoryStore:
    """Manages both Project-level (.tmem/) and Global-level (~/.tmem/) trace stores."""

    def __init__(self, workspace_root: str, global_root: Optional[str] = None) -> None:
        self.workspace_root = str(Path(workspace_root).resolve())
        self.project_dir = Path(self.workspace_root) / ".tmem"
        self.project_traces_dir = self.project_dir / "traces"
        self.project_raw_dir = self.project_dir / "raw"
        self.project_cache_dir = self.project_dir / ".cache"
        self.project_db_path = self.project_cache_dir / "index.db"
        self.raw_store = RawTraceStore(self.workspace_root)

        self.global_dir = (
            Path(global_root).resolve()
            if global_root
            else Path(os.environ.get("TMEM_GLOBAL_DIR", str(Path.home() / ".tmem"))).resolve()
        )
        self.global_traces_dir = self.global_dir / "playbooks"
        self.global_db_path = self.global_dir / "global_index.db"

    def ensure_initialized(self, init_project: bool = True) -> None:
        """Create directory hierarchy and gitignore for local cache."""
        self.global_traces_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite(self.global_db_path)

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
        with sqlite3.connect(str(self.project_db_path)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM playbooks")
            count = cur.fetchone()[0]
            if count != len(json_files):
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
        auto_promote_global: bool = True,
        target_scope: str = "auto",
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Save playbook according to target_scope:
        - "project": Save only to Project Tier (.tmem/traces/<id>.json)
        - "global": Save only to Global Tier (~/.tmem/playbooks/<id>.json)
        - "both": Save to Project Tier AND force-distill to Global Tier
        - "auto": Save to Project Tier, and auto-promote to Global Tier if L2 causal repairs exist
        Returns (project_trace_id, global_trace_id).
        """
        proj_id: Optional[str] = None
        promoted_id: Optional[str] = None

        if target_scope in ("project", "both", "auto"):
            self.ensure_initialized(init_project=True)
            raw_path = self.project_raw_dir / f"{playbook.trace_id}.raw.json"
            if raw_path.exists():
                if not playbook.raw_trace_path:
                    playbook.raw_trace_path = f"raw/{playbook.trace_id}.raw.json"
                if not playbook.raw_trace_hash:
                    playbook.raw_trace_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()[:16]
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

        should_save_global = (
            target_scope in ("global", "both")
            or (target_scope == "auto" and auto_promote_global and bool(playbook.l2_causal_repairs))
        )
        if should_save_global:
            global_pb = self._distill_to_global(
                playbook, force=(target_scope in ("global", "both"))
            )
            if global_pb:
                self.save_global_playbook(global_pb)
                promoted_id = global_pb.trace_id

        return proj_id, promoted_id

    def save_global_playbook(self, global_pb: TracePlaybook) -> None:
        self.ensure_initialized(init_project=False)
        glob_file = self.global_traces_dir / f"{global_pb.trace_id}.json"
        glob_file.write_text(
            json.dumps(global_pb.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with sqlite3.connect(str(self.global_db_path)) as conn:
            self._UpsertToDb(conn, global_pb)

    def _distill_to_global(
        self, pb: TracePlaybook, force: bool = False
    ) -> Optional[TracePlaybook]:
        """Strip project-specific file paths to create a portable Global L2 Playbook."""
        if not pb.l2_causal_repairs and not force:
            return None

        portable_repairs: List[CausalRepairPair] = []
        for r in pb.l2_causal_repairs:
            portable_repairs.append(
                CausalRepairPair(
                    error_signature=r.error_signature,
                    failed_command=r.failed_command,
                    repair_action=r.repair_action,
                    verification_cmd=r.verification_cmd,
                    is_env_or_toolchain=r.is_env_or_toolchain,
                )
            )

        # When user explicitly runs `/tmem global` on a task without an error stack,
        # synthesize a portable workflow recipe from verification/execution steps
        if not portable_repairs and force:
            steps_summary = " -> ".join(
                f"{s['action']}({s.get('command') or Path(str(s.get('target', ''))).name})"
                for s in pb.l0_critical_path[:6]
            )
            verify_cmd = pb.l1_verification_cmds[-1] if pb.l1_verification_cmds else ""
            portable_repairs.append(
                CausalRepairPair(
                    error_signature=pb.problem_intent[:120],
                    failed_command="",
                    repair_action=steps_summary or "Execute portable workflow",
                    verification_cmd=verify_cmd,
                    is_env_or_toolchain=True,
                )
            )

        sig_seed = "|".join(r.error_signature for r in portable_repairs) or pb.problem_intent
        global_id = "gtr_" + hashlib.sha256(sig_seed.encode("utf-8")).hexdigest()[:10]

        return TracePlaybook(
            trace_id=global_id,
            agent_source=pb.agent_source,
            scope="global",
            created_at=pb.created_at,
            git_commit=None,
            problem_intent=pb.problem_intent,
            error_signatures=pb.error_signatures,
            touched_files=[],  # Stripped for global portability
            touched_symbols=pb.touched_symbols,
            guards=[],         # L2 invariants rely on error signature trigger
            l0_critical_path=[],
            l1_cochange_topology=[],
            l1_verification_cmds=pb.l1_verification_cmds,
            l2_causal_repairs=portable_repairs,
            confidence=pb.confidence,
            raw_steps_count=pb.raw_steps_count,
            sliced_steps_count=pb.sliced_steps_count,
        )

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
        3-Stage Deterministic Funnel across Project Tier (.tmem/) -> Global Tier (~/.tmem/):
        Stage 1: Exact Error Signature / File Path / Symbol Match
        Stage 2: SQLite FTS5 BM25 Match
        Stage 3: Pre-flight Guard Evaluation & L0->L1->L2 Progressive Degradation
        """
        self.sync_project_index_if_needed()
        candidates: Dict[str, Tuple[TracePlaybook, float]] = {}

        # Search Project DB first (higher priority), then Global DB
        # Deduplicate global clones that were distilled from the current project's own traces
        seen_signatures: Set[str] = set()
        db_list = []
        if self.project_db_path.exists():
            db_list.append((self.project_db_path, "project"))
        if self.global_db_path.exists():
            db_list.append((self.global_db_path, "global"))

        for db_path, tier_name in db_list:
            for pb, score in self._query_single_db(
                db_path, query_text, file_paths or [], error_signatures or []
            ):
                dedup_sig = f"{pb.problem_intent}|{'|'.join(pb.error_signatures)}"
                if pb.trace_id not in candidates and dedup_sig not in seen_signatures and pb.status != "invalidated":
                    candidates[pb.trace_id] = (pb, score)
                    seen_signatures.add(dedup_sig)

        # Evaluate pre-flight guards on every matched candidate
        evaluated_pairs: List[Tuple[EvaluatedPlaybook, float]] = []
        for pb, score in candidates.values():
            ev = evaluate_guards(pb, self.workspace_root)
            evaluated_pairs.append((ev, score))

        # Rank: Project vs Global > Relevance Score (exact match + BM25) > L0 Guard Validity > Confidence
        def _rank_key(item: Tuple[EvaluatedPlaybook, float]) -> Tuple[int, float, int, float]:
            ev, rel_score = item
            is_proj = 1 if ev.playbook.scope == "project" else 0
            l0_ok = 1 if ev.l0_valid else 0
            return (is_proj, round(rel_score, 2), l0_ok, ev.playbook.confidence)

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

                # 4. FTS5 BM25 keyword fallback
                tokens = [
                    t for t in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", query_text)
                    if len(t) >= 2
                ]
                if tokens:
                    fts_query = " OR ".join(f'"{t}"' for t in tokens[:12])
                    try:
                        cur = conn.execute(
                            "SELECT trace_id, rank FROM playbooks_fts WHERE playbooks_fts MATCH ? ORDER BY rank LIMIT 10",
                            (fts_query,),
                        )
                        for (tid, rank_val) in cur.fetchall():
                            bm25_score = max(0.1, -float(rank_val))
                            scores[tid] = scores.get(tid, 0.0) + bm25_score
                    except sqlite3.OperationalError:
                        pass

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

        for jf in self.project_traces_dir.glob("*.json"):
            try:
                pb = TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                ev = evaluate_guards(pb, self.workspace_root)
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
            except Exception:
                continue
        try:
            self.sync_memories_markdown()
        except Exception:
            pass
        return stats

    def list_playbooks(self, scope: str = "both") -> List[Dict[str, object]]:
        """List summary entries of stored traces across project and/or global tiers."""
        items: List[Dict[str, object]] = []
        dirs: List[Tuple[str, Path]] = []
        if scope in ("project", "both"):
            dirs.append(("project", self.project_traces_dir))
        if scope in ("global", "both"):
            dirs.append(("global", self.global_traces_dir))

        for tier_name, t_dir in dirs:
            if not t_dir.exists():
                continue
            for jf in sorted(t_dir.glob("*.json")):
                try:
                    pb = TracePlaybook.from_dict(json.loads(jf.read_text(encoding="utf-8")))
                    ev = evaluate_guards(pb, self.workspace_root)
                    items.append(
                        {
                            "trace_id": pb.trace_id,
                            "scope": tier_name,
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
        """Remove a trace by ID (or prefix) from project and/or global stores."""
        removed_from: List[str] = []
        tid = trace_id.strip()
        if not tid:
            return {"deleted": False, "removed_from": []}

        for tier_name, t_dir, db_path in [
            ("project", self.project_traces_dir, self.project_db_path),
            ("global", self.global_traces_dir, self.global_db_path),
        ]:
            if not t_dir.exists():
                continue
            for jf in list(t_dir.glob("*.json")):
                if jf.stem == tid or jf.stem.startswith(tid):
                    matched_id = jf.stem
                    try:
                        jf.unlink()
                        removed_from.append(f"{tier_name}:{matched_id}")
                    except Exception:
                        pass
                    if db_path.exists():
                        try:
                            with sqlite3.connect(str(db_path)) as conn:
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

    def get_all_global_playbooks(self) -> List[TracePlaybook]:
        """Load all active global playbooks."""
        if not self.global_traces_dir.exists():
            return []
        playbooks: List[TracePlaybook] = []
        for jf in sorted(self.global_traces_dir.glob("*.json")):
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
        glob_pbs = self.get_all_global_playbooks()
        return sync_memories_file(self.workspace_root, proj_pbs, global_playbooks=glob_pbs)
