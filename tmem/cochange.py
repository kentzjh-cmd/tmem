"""
Statistical Co-Change Topology Analyzer (L1).

Computes recurring file co-change pairs from Git commit history and multi-trace
records, filtering out bulk sweeps (>10 files) and metadata/noise files so that
only genuine, recurring co-edit contracts surface.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from tmem import logger
from tmem.adapters.base import is_code_file, is_doc_file
from tmem.models import TracePlaybook

_IGNORED_PREFIXES = (".tmem/", ".agents/", ".dsh/", ".git/")
_IGNORED_FILENAMES = {
    "MEMORIES.md",
    "MEM.md",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
}


@dataclass(frozen=True)
class CoChangePair:
    file_a: str
    file_b: str
    count: int
    confidence: float  # max(P(B|A), P(A|B))

    def format_label(self) -> str:
        pct = int(round(self.confidence * 100))
        return f"{self.file_a} <-> {self.file_b} (co-changed {self.count}x, {pct}% conf)"


def _is_trackable_cochange_file(path_str: str) -> bool:
    p = path_str.strip()
    if not p or p.startswith(_IGNORED_PREFIXES):
        return False
    if Path(p).name in _IGNORED_FILENAMES or is_doc_file(p):
        return False
    return True


def _load_git_transactions(
    workspace_root: str, max_commits: int = 100, max_files_per_tx: int = 10
) -> Tuple[List[List[str]], Set[str]]:
    """
    Return (transactions, seen_short_hashes) from `git log`.
    Bulk commits touching > max_files_per_tx files are excluded to avoid spurious coupling.
    """
    ws = Path(workspace_root)
    if not (ws / ".git").exists():
        return [], set()
    try:
        res = subprocess.run(
            ["git", "log", f"-n{max_commits}", "--name-only", "--pretty=format:---COMMIT:%h"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode != 0:
            return [], set()
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("git log co-change scan skipped: %s", e)
        return [], set()

    transactions: List[List[str]] = []
    seen_hashes: Set[str] = set()
    current_files: List[str] = []

    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("---COMMIT:"):
            if 2 <= len(current_files) <= max_files_per_tx:
                transactions.append(sorted(set(current_files)))
            current_files = []
            h = line.split(":", 1)[1].strip()
            if h:
                seen_hashes.add(h)
        elif line and _is_trackable_cochange_file(line):
            current_files.append(line)

    if 2 <= len(current_files) <= max_files_per_tx:
        transactions.append(sorted(set(current_files)))

    return transactions, seen_hashes


def compute_cochange_stats(
    workspace_root: str,
    playbooks: Optional[Iterable[TracePlaybook]] = None,
    *,
    max_commits: int = 100,
    min_count: int = 2,
    min_confidence: float = 0.5,
    max_files_per_tx: int = 10,
) -> List[CoChangePair]:
    """
    Compute statistically recurring co-change pairs from Git history and non-trivial playbooks.
    Deduplicates playbooks whose `git_commit` was already observed in `git log`.
    """
    transactions, seen_hashes = _load_git_transactions(
        workspace_root, max_commits=max_commits, max_files_per_tx=max_files_per_tx
    )

    if playbooks:
        for pb in playbooks:
            if getattr(pb, "is_trivial", False) or pb.status == "invalidated":
                continue
            if pb.git_commit and pb.git_commit in seen_hashes:
                continue
            files = sorted({f for f in pb.touched_files if _is_trackable_cochange_file(f)})
            if 2 <= len(files) <= max_files_per_tx:
                transactions.append(files)

    if not transactions:
        return []

    file_freq: Counter[str] = Counter()
    pair_freq: Counter[Tuple[str, str]] = Counter()

    for tx in transactions:
        for f in tx:
            file_freq[f] += 1
        for i in range(len(tx)):
            for j in range(i + 1, len(tx)):
                a, b = tx[i], tx[j]
                # Skip pairs consisting solely of documentation files unless one is code
                if is_doc_file(a) and is_doc_file(b):
                    continue
                if not (is_code_file(a) or is_code_file(b)):
                    continue
                pair_freq[(a, b)] += 1

    pairs: List[CoChangePair] = []
    for (a, b), count in pair_freq.items():
        if count < min_count:
            continue
        conf = max(count / file_freq[a], count / file_freq[b])
        if conf >= min_confidence:
            pairs.append(CoChangePair(file_a=a, file_b=b, count=count, confidence=round(conf, 3)))

    pairs.sort(key=lambda p: (-p.count, -p.confidence, p.file_a, p.file_b))
    return pairs


def find_cochange_for_files(
    workspace_root: str,
    target_files: Set[str],
    playbooks: Optional[Iterable[TracePlaybook]] = None,
    limit: int = 5,
) -> List[str]:
    """Return formatted statistically verified co-change labels relevant to `target_files`."""
    if not target_files:
        return []
    pairs = compute_cochange_stats(workspace_root, playbooks=playbooks)
    matched = [
        p.format_label()
        for p in pairs
        if p.file_a in target_files and p.file_b in target_files
    ]
    if not matched:
        matched = [
            p.format_label()
            for p in pairs
            if p.file_a in target_files or p.file_b in target_files
        ]
    return matched[:limit]


def find_cochange_partners_for_files(
    workspace_root: str,
    edited_files: Iterable[str],
    playbooks: Optional[Iterable[TracePlaybook]] = None,
    limit: int = 3,
) -> List[Tuple[str, str, int, float]]:
    """
    Return existing co-change partner files for `edited_files` as
    (edited_file, partner_file, cochange_count, confidence).
    """
    edited_set = {f for f in edited_files if f}
    if not edited_set:
        return []
    ws = Path(workspace_root)
    pairs = compute_cochange_stats(workspace_root, playbooks=playbooks)
    out: List[Tuple[str, str, int, float]] = []
    seen_partners: Set[str] = set()
    for p in pairs:
        if p.file_a in edited_set and p.file_b not in edited_set:
            partner, src = p.file_b, p.file_a
        elif p.file_b in edited_set and p.file_a not in edited_set:
            partner, src = p.file_a, p.file_b
        else:
            continue
        if partner in seen_partners:
            continue
        if not (ws / partner).exists():
            continue
        seen_partners.add(partner)
        out.append((src, partner, p.count, p.confidence))
        if len(out) >= limit:
            break
    return out
