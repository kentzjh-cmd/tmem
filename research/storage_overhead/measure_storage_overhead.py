#!/usr/bin/env python3
"""Measure tmem's storage overhead from the archived experiment roots.

The numbers quoted in `docs/dsh-tmem-storage-overhead.md` come from this
script, so they can be re-derived instead of taken on faith.

    python3 research/storage_overhead/measure_storage_overhead.py

Sources (both are real experiment roots, not fixtures):
  ABL_ARMS   the carrier-ablation arms  (/home/kent/abl2/arms)
  ABL_SEED   the seed those arms were cloned from (/home/kent/abl2/seed)
  R3_FIXTURE the E2E round-3 fixture, after 18 real sessions
             (/home/kent/e2e/r3/ledger-svc)

Every path is optional: a missing root is reported as missing rather than
crashing, so the script still runs on a machine that only has some of them.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from pathlib import Path

ABL_ARMS = Path("/home/kent/abl2/arms")
ABL_SEED = Path("/home/kent/abl2/seed")
R3_FIXTURE = Path("/home/kent/e2e/r3/ledger-svc")
REPO = Path(__file__).resolve().parents[2]

SUBS = (".cache", "raw", "episodes", "traces")


def breakdown(root: Path) -> dict:
    """Byte/count breakdown of one .tmem directory."""
    if not root.is_dir():
        return {}
    out: dict = {}
    for sub in SUBS:
        d = root / sub
        files = [p.stat().st_size for p in d.iterdir() if p.is_file()] if d.is_dir() else []
        out[sub] = {"n": len(files), "bytes": sum(files)}
    root_files = [p.stat().st_size for p in root.iterdir() if p.is_file()]
    out["root_files"] = {"n": len(root_files), "bytes": sum(root_files)}
    out["total"] = {"n": sum(out[k]["n"] for k in (*SUBS, "root_files")),
                    "bytes": sum(out[k]["bytes"] for k in (*SUBS, "root_files"))}
    return out


def kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def per_trace(root: Path) -> dict | None:
    """Marginal cost of one captured trace, from the round-3 fixture."""
    store = root / ".tmem"
    if not store.is_dir():
        return None
    bd = breakdown(store)
    n_traces = bd["raw"]["n"]
    if n_traces == 0:
        return None
    durable = bd["raw"]["bytes"] + bd["traces"]["bytes"] + bd["episodes"]["bytes"]
    cache = bd[".cache"]["bytes"]
    return {
        "traces": n_traces,
        "raw_each": bd["raw"]["bytes"] / n_traces,
        "trace_each": bd["traces"]["bytes"] / bd["traces"]["n"],
        "episode_each": bd["episodes"]["bytes"] / bd["episodes"]["n"],
        "durable_each": durable / n_traces,
        "cache_each": cache / n_traces,
        "disk_each": (durable + cache) / n_traces,
        "tracked_each": (bd["raw"]["bytes"] + bd["traces"]["bytes"]) / n_traces,
        "cache_share": cache / (durable + cache) if (durable + cache) else 0.0,
        "breakdown": bd,
    }


def index_pages(db_path: Path) -> list[tuple[str, int]]:
    """Per-table page usage of the inverted index (SQLite dbstat)."""
    if not db_path.is_file():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [(n, s) for n, s in con.execute(
            "select name, sum(pgsize) from dbstat group by name order by 2 desc"
        )]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def main() -> int:
    print("=" * 72)
    print("tmem storage overhead -- measured from the real experiment roots")
    print("=" * 72)

    print("\n[1] the round-3 E2E fixture, after 18 real sessions")
    pt = per_trace(R3_FIXTURE)
    if not pt:
        print(f"    MISSING: {R3_FIXTURE} (the fixture with captured sessions)")
    else:
        bd = pt["breakdown"]
        for sub in (*SUBS, "root_files"):
            b = bd[sub]
            share = 100 * b["bytes"] / bd["total"]["bytes"] if bd["total"]["bytes"] else 0
            print(f"    {sub:12s} n={b['n']:3d}  total {kb(b['bytes']):>12s}  {share:5.1f}%")
        print(f"    {'TOTAL':12s} n={bd['total']['n']:3d}  total {kb(bd['total']['bytes']):>12s}")
        print()
        print(f"    captured traces            : {pt['traces']}")
        print(f"    raw/ per trace             : {kb(int(pt['raw_each']))}")
        print(f"    traces/ per trace          : {kb(int(pt['trace_each']))}")
        print(f"    episodes/ per episode      : {kb(int(pt['episode_each']))}")
        print(f"    memory body per trace      : {kb(int(pt['durable_each']))}")
        print(f"    cache amortised per trace  : {kb(int(pt['cache_each']))}")
        print(f"    DISK per trace             : {kb(int(pt['disk_each']))}")
        print(f"    committed (git) per trace  : {kb(int(pt['tracked_each']))}")
        print(f"    cache share of disk        : {100 * pt['cache_share']:.1f}%")
        pages = index_pages(R3_FIXTURE / ".tmem" / ".cache" / "index.db")
        if pages:
            print("\n    index.db page usage (top 6):")
            for name, size in pages[:6]:
                print(f"      {name:40s} {kb(size):>12s}")

    print("\n[1b] the source the store was distilled from: dsh session transcripts")
    tr_sizes: list[int] = []
    for scene in sorted(Path("/home/kent/e2e/_harness/raw-r3").glob("*/scene.json")):
        try:
            tp = json.loads(scene.read_text(encoding="utf-8")).get("transcript_path")
        except (OSError, json.JSONDecodeError):
            continue
        if tp and Path(tp).is_file():
            tr_sizes.append(Path(tp).stat().st_size)
    if tr_sizes:
        tr_sizes.sort()
        print(f"    dsh transcripts (round 3): n={len(tr_sizes)}"
              f"  total {kb(sum(tr_sizes))}"
              f"  median {kb(tr_sizes[len(tr_sizes) // 2])}"
              f"  min {kb(tr_sizes[0])}  max {kb(tr_sizes[-1])}")
        print("    -> the plugin's raw/ archive (~105 KB per trace) is the same order of")
        print("       magnitude as the session transcript it was distilled from: a slice,")
        print("       not a summary.")
    else:
        print("    MISSING: /home/kent/e2e/_harness/raw-r3 (the session transcripts)")

    print("\n[2] the same facts, two carriers (carrier-ablation A1 vs B2)")
    prose = ABL_ARMS / "B2" / "rep1" / "MEMORY.md"
    rendered = ABL_ARMS / "A1" / "rep1" / "MEMORIES.md"
    a1 = breakdown(ABL_ARMS / "A1" / "rep1" / ".tmem")
    if not a1:
        print(f"    MISSING: {ABL_ARMS}/A1/rep1/.tmem")
    else:
        cache = a1[".cache"]["bytes"]
        disk = a1["total"]["bytes"]
        print(f"    .tmem on disk               : {kb(disk)}")
        print(f"      of which rebuildable cache: {kb(cache)}")
        if prose.is_file():
            p = prose.stat().st_size
            print(f"    prose carrier MEMORY.md     : {kb(p)}")
            print(f"      disk / prose              : {disk / p:.1f}x")
            print(f"      (disk - cache) / prose    : {(disk - cache) / p:.1f}x")
        if rendered.is_file():
            r = rendered.stat().st_size
            print(f"    tmem rendered MEMORIES.md    : {kb(r)}  ({r / prose.stat().st_size:.1f}x the prose)")
    seed = breakdown(ABL_SEED / ".tmem")
    if seed:
        print(f"    seed store (before any session): {kb(seed['total']['bytes'])}"
              f"  episodes={seed['episodes']['n']} raw={seed['raw']['n']}")

    print("\n[3] what a project actually pays")
    for rel in (".dsh/plugins/tmem-dsh-plugin.mjs", ".dsh/skills/tmem/SKILL.md", ".dsh/tmem.patch.yml"):
        p = REPO / rel
        if p.is_file():
            print(f"    {rel:42s} {p.stat().st_size:7d} B")
    gi = REPO / ".tmem" / ".gitignore"
    if gi.is_file():
        cur = gi.read_text().split()
        # what the plugin writes for a project (tmem/store.mjs, project_dir init)
        plugin_lines = [".cache/", "runtime_state.json", "telemetry.jsonl", "episodes/"]
        print("\n    plugin's .tmem/.gitignore (tmem/store.mjs):")
        print(f"      {' '.join(plugin_lines)}")
        print(f"    this repository's copy: {' '.join(cur) or '(empty)'}")
        missing = [ln for ln in plugin_lines if ln not in cur]
        if missing:
            print(f"      (stale: missing {' '.join(missing)} -- the store appends it on next init)")
        print("    -> .cache/, runtime_state.json, telemetry.jsonl and episodes/ never reach the")
        print("       repository; raw/ and traces/ do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
