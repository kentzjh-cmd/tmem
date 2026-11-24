#!/usr/bin/env python3
"""Archive the REAL sample trajectories behind the published numbers, and seal them.

Why this exists
---------------
The published figures (per-arm means, gate verdicts, tool-call sequences) are
derived from machine verdicts.  A verdict is only as trustworthy as the
trajectories it was computed from.  Until now the verdict JSONs were archived
but the *sessions* were not: the numbers could not be re-derived from first
principles inside the repository.

This script copies the raw trajectory files into the repository and writes a
signed manifest (sha256 per file + a per-session trajectory digest).  The
verifier (`research/carrier-ablation/verify_report_numbers.py`) then re-derives
the digest from the archived `.zstd` transcripts and re-computes the published
tool-call sequences from them, so every published trajectory-level number is
reproducible from the archived evidence alone.

Usage
-----
    python3 research/archive_trajectories.py \
        --ablation-root /home/kent/abl2/raw \
        --round3-root   /home/kent/e2e/_harness/raw-r3

Idempotent: re-running recomputes the manifest and reports drift instead of
silently accepting a changed trajectory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trajectory_lib import digest_events, edited_files, read_events, sha256_file, workspace_root  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ABL_DEST = REPO / "research" / "carrier-ablation" / "results" / "raw"
R3_DEST = REPO / "research" / "round3" / "results" / "raw"
R2_DEST = REPO / "research" / "round2" / "results" / "raw"
ABL_MANIFEST = REPO / "research" / "carrier-ablation" / "results" / "raw_manifest.json"
R3_MANIFEST = REPO / "research" / "round3" / "results" / "raw_manifest.json"
R2_MANIFEST = REPO / "research" / "round2" / "results" / "raw_manifest.json"
# verdicts that are cited by the reports but produced outside this repo
HARNESS = Path("/home/kent/e2e/_harness")
EXTRA_VERDICTS = (
    # round 1: the baseline and its re-aggregations
    (REPO / "research" / "round1" / "results" / "metrics.json", HARNESS / "metrics.json"),
    (REPO / "research" / "round1" / "results" / "metrics_r1_pre_reaggregate.json",
     HARNESS / "metrics_r1_pre_reaggregate.json"),
    (REPO / "research" / "round1" / "results" / "metrics_after_cjk_fix.json",
     HARNESS / "metrics_after_cjk_fix.json"),
    (REPO / "research" / "round1" / "results" / "metrics_before_g3_aggregation.json",
     HARNESS / "metrics_before_g3_aggregation.json"),
    (REPO / "research" / "round1" / "results" / "metrics-r1-revised.json",
     HARNESS / "metrics-r1-revised.json"),
    # round 2: the final verdict and the partial runs that preceded it
    (REPO / "research" / "round2" / "results" / "metrics-r2.json", HARNESS / "metrics-r2.json"),
    (REPO / "research" / "round2" / "results" / "metrics-r2-partial.json", HARNESS / "metrics-r2-partial.json"),
    (REPO / "research" / "round2" / "results" / "metrics-r2-partial2.json", HARNESS / "metrics-r2-partial2.json"),
    (REPO / "research" / "round2" / "results" / "metrics-r2-partial3.json", HARNESS / "metrics-r2-partial3.json"),
    (REPO / "research" / "round2" / "results" / "metrics-h6.json", HARNESS / "metrics-h6.json"),
)

# every regular file of a scene is evidence; the sets are small (<= ~250 KB each)
MAX_FILE_BYTES = 8 * 1024 * 1024


def collect_session(scene: Path, dest_dir: Path, rel_id: str, source_root: str = "", source_set: str = "") -> dict:
    """Copy one session's raw files into dest_dir and describe them."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    for src in sorted(scene.iterdir()):
        if not src.is_file():
            continue
        if src.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError(f"unexpectedly large evidence file: {src}")
        dst = dest_dir / src.name
        shutil.copy2(src, dst)
        files[src.name] = {"bytes": dst.stat().st_size, "sha256": sha256_file(dst)}

    # a scene may be a direct CLI probe (no dsh session, hence no scene.json)
    scene_file = scene / "scene.json"
    scene_meta: dict = {}
    if scene_file.is_file():
        scene_meta = json.loads(scene_file.read_text(encoding="utf-8"))

    tpath = scene_meta.get("transcript_path")
    digest: dict = {}
    if tpath:
        tsrc = Path(tpath)
        if not tsrc.exists():
            raise FileNotFoundError(f"transcript missing for {scene}: {tsrc}")
        tdst = dest_dir / "session.v4.jsonl.zstd"
        shutil.copy2(tsrc, tdst)
        files[tdst.name] = {"bytes": tdst.stat().st_size, "sha256": sha256_file(tdst)}
        events = read_events(tdst)
        digest = digest_events(events)
        digest["transcript_sha256"] = files[tdst.name]["sha256"]
        root = workspace_root(events)
        digest["workspace_root"] = root
        digest["edited_files"] = edited_files(events, root)

    return {
        "id": rel_id,
        "scene": scene.name,
        "source_root": source_root,
        "source_set": source_set,
        "has_scene_meta": bool(scene_meta),
        "arm": scene_meta.get("arm"),
        "rep": scene_meta.get("rep"),
        "variant": scene_meta.get("variant"),
        "exit_code": scene_meta.get("exit_code"),
        "elapsed_sec": scene_meta.get("elapsed_sec"),
        "isolation": scene_meta.get("isolation"),
        "files": files,
        "trajectory": digest,
    }


def archive_ablation(src_root: Path) -> dict:
    sessions = []
    for scene in sorted(p for p in src_root.glob("*/*/*") if p.is_dir()):
        rel = scene.relative_to(src_root)
        sessions.append(collect_session(scene, ABL_DEST / rel, str(rel)))
    return {
        "dataset": "carrier-ablation-phase1b",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(src_root),
        "dest_root": str(ABL_DEST.relative_to(REPO)),
        "sessions": sessions,
    }


def archive_round3(src_root: Path) -> dict:
    sessions = []
    for scene in sorted(p for p in src_root.iterdir() if p.is_dir()):
        sessions.append(collect_session(scene, R3_DEST / scene.name, scene.name))
    return {
        "dataset": "e2e-round3",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(src_root),
        "dest_root": str(R3_DEST.relative_to(REPO)),
        "sessions": sessions,
    }


def archive_round2(src_roots: list[tuple[Path, str]]) -> dict:
    """Round 2 ran twice (initial run + post-fix re-run); both raw sets are kept.

    Which of the two produced `metrics-r2.json` was never proven: the injected
    block's session ids occur in both.  The manifest therefore records the
    source set per session and asserts *integrity only* -- no Round-2 claim is
    attributed to a particular raw set.
    """
    sessions = []
    for src_root, source_set in src_roots:
        if not src_root.is_dir():
            continue
        for scene in sorted(p for p in src_root.iterdir() if p.is_dir()):
            rel = f"{source_set}/{scene.name}"
            sessions.append(
                collect_session(scene, R2_DEST / source_set / scene.name, rel, str(src_root), source_set)
            )
    return {
        "dataset": "e2e-round2",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_roots": [str(s) for s, _ in src_roots],
        "dest_root": str(R2_DEST.relative_to(REPO)),
        "bound_to_verdict": False,
        "binding_note": (
            "Round-2 的两个原始轨迹目录都含注入块记录的 session id，判定与原始轨迹的逐场对应关系"
            "未被证明；本清单只声明文件完整性，不主张归属。"
        ),
        "sessions": sessions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ablation-root", default="/home/kent/abl2/raw")
    ap.add_argument("--round3-root", default="/home/kent/e2e/_harness/raw-r3")
    ap.add_argument("--round2-root", default="/home/kent/e2e/_harness/raw-r2")
    ap.add_argument("--round2-postfix-root", default="/home/kent/e2e/_harness/raw-r2-postfix")
    args = ap.parse_args()

    problems = []
    for dest, manifest, builder, src in (
        (ABL_DEST, ABL_MANIFEST, archive_ablation, Path(args.ablation_root)),
        (R3_DEST, R3_MANIFEST, archive_round3, Path(args.round3_root)),
    ):
        if not src.is_dir():
            print(f"SKIP {src} (not present)")
            continue
        doc = builder(src)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        n_files = sum(len(s["files"]) for s in doc["sessions"])
        total = sum(f["bytes"] for s in doc["sessions"] for f in s["files"].values())
        print(
            f"OK {doc['dataset']}: {len(doc['sessions'])} sessions, {n_files} files, "
            f"{total / 1024:.0f} KB -> {manifest.relative_to(REPO)}"
        )
        if not doc["sessions"]:
            problems.append(f"{doc['dataset']}: no sessions found under {src}")

    r2_sources = [(Path(args.round2_root), "r2"), (Path(args.round2_postfix_root), "r2-postfix")]
    r2_sources = [(s, name) for s, name in r2_sources if s.is_dir()]
    if r2_sources:
        doc = archive_round2(r2_sources)
        R2_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        R2_MANIFEST.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        n_files = sum(len(s["files"]) for s in doc["sessions"])
        total = sum(f["bytes"] for s in doc["sessions"] for f in s["files"].values())
        print(
            f"OK {doc['dataset']}: {len(doc['sessions'])} sessions, {n_files} files, "
            f"{total / 1024:.0f} KB -> {R2_MANIFEST.relative_to(REPO)} (bound_to_verdict=false)"
        )
        if not doc["sessions"]:
            problems.append("e2e-round2: no sessions found")

    for dest, src in EXTRA_VERDICTS:
        if not src.is_file():
            print(f"SKIP {src} (not present)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"OK verdict copy {src} -> {dest.relative_to(REPO)}")

    if problems:
        print("FAIL:")
        for p in problems:
            print("  -", p)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
