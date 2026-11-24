#!/usr/bin/env python3
"""Negative tests for the drift guard: every published number must be *falsifiable*.

A guard that only passes is not a guard.  This script copies the repository's
published artefacts into a scratch tree, mutates them one at a time, and
asserts that the verifier refuses each mutation with a non-zero exit code.

    python3 research/carrier-ablation/test_drift_guard.py

Cases
-----
prose    a contradicting figure appended to the main report ("G6 lookup 0.95")
prose    a stale figure re-introduced ("遥测 11 次", "30/30 场有效", ...)
traj     one byte of an archived session transcript changed
record   a published per-session record no longer matches its transcript
aggregate the verdict's pre-aggregated block no longer matches the sessions
table   a Round-2 column figure contradicts the archived Round-2 verdict
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFY = REPO / "research" / "carrier-ablation" / "verify_report_numbers.py"
MAIN = "docs/dsh-independent-e2e-report.md"
ABL = "docs/dsh-memory-carrier-ablation-report.md"
ABL_VERDICT = "research/carrier-ablation/results/metrics-carrier-ablation-v1.2.json"
ABL_RAW = "research/carrier-ablation/results/raw/A1/rep1/free/session.v4.jsonl.zstd"
R3_VERDICT = "research/round3/results/metrics-r3-recheck-clean-tree.json"


def build_tree(dest: Path) -> None:
    """A scratch copy of everything the verifier reads."""
    for rel in (
        "docs",
        "research/carrier-ablation",
        "research/round3",
        "research/round2",
    ):
        src = REPO / rel
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, out, copy_function=shutil.copy2)


def run(dest: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(VERIFY)],
        capture_output=True,
        text=True,
        env={**os.environ, "TMEM_VERIFY_ROOT": str(dest)},
    )
    return proc.returncode, proc.stdout + proc.stderr


MUTATED: set[str] = set()


def restore(dest: Path, rel: str) -> Path:
    """Replace a scratch file with a pristine copy before mutating it."""
    path = dest / rel
    pristine = REPO / rel
    data = pristine.read_bytes()
    path.unlink()
    path.write_bytes(data)
    MUTATED.add(rel)
    return path


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n" + text + "\n")


def edit_json(path: Path, fn) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cases() -> list[tuple[str, str, callable]]:
    """(name, expected message fragment, mutator)"""

    def prose_contradiction(dest: Path) -> None:
        append(restore(dest, MAIN), "| **G6** 知识检索 | FAIL lookup 0.625 / search 0.750 | **PASS lookup 0.95 / search 0.625** | 矛盾值 |")

    def prose_stale_telemetry(dest: Path) -> None:
        append(restore(dest, MAIN), "G8 的 l2_injected 遥测 11 次。")

    def prose_stale_validity(dest: Path) -> None:
        append(restore(dest, ABL), "本次实验 30/30 场有效，零越界读取。")

    def trajectory_byte(dest: Path) -> None:
        path = restore(dest, ABL_RAW)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        path.write_bytes(bytes(raw))

    def record_mismatch(dest: Path) -> None:
        def fn(data: dict) -> None:
            for s in data["sessions"]:
                if s["arm"] == "A1" and s["rep"] == 1 and s["variant"] == "free":
                    s["tmem_tool_calls"] = ["tmem_search"]
                    return
            raise AssertionError("session not found")

        edit_json(restore(dest, ABL_VERDICT), fn)

    def aggregate_mismatch(dest: Path) -> None:
        def fn(data: dict) -> None:
            data["aggregate"]["A1|free"]["knowledge_tokens_stats"]["mean"] = 99999

        edit_json(restore(dest, ABL_VERDICT), fn)

    def round2_column_mismatch(dest: Path) -> None:
        path = restore(dest, MAIN)
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "| **G6** 知识检索 | **FAIL** lookup 0.625 / search 0.750 |",
            "| **G6** 知识检索 | **FAIL** lookup 0.95 / search 0.750 |",
        )
        path.write_text(text, encoding="utf-8")

    def edited_files_mismatch(dest: Path) -> None:
        def fn(data: dict) -> None:
            for g in data["gates"]:
                d = g.get("detail")
                if isinstance(d, dict) and "S1_baseline" in d:
                    d["S1_baseline"]["session_edited_files"] = ["README.md"]
                    return
            raise AssertionError("detail not found")

        edit_json(restore(dest, R3_VERDICT), fn)

    def retracted_zero_headline(dest: Path) -> None:
        append(restore(dest, ABL), '**tmem 的优势在"零检索可得"，不在"总 token 更少"。**')

    def retracted_carrier_claim(dest: Path) -> None:
        append(restore(dest, MAIN), "本课题稳健确立了 tmem 载体在效果上优于散文载体。")

    return [
        ("contradicting figure in the prose", "gate table G6", prose_contradiction),
        ("stale telemetry figure", "遥测 11 次", prose_stale_telemetry),
        ("stale validity claim", "forbidden phrase", prose_stale_validity),
        ("tampered session transcript", "sha256", trajectory_byte),
        ("published record vs transcript", "tool calls from the transcript", record_mismatch),
        ("pre-aggregated block vs sessions", "aggregate A1|free", aggregate_mismatch),
        ("Round-2 column vs archived verdict", "gate table G6", round2_column_mismatch),
        ("edited files vs verdict", "files edited per the transcript", edited_files_mismatch),
        ("retracted zero-regime headline", "forbidden phrase", retracted_zero_headline),
        ("retracted carrier claim", "forbidden phrase", retracted_carrier_claim),
    ]


def main() -> int:
    failures: list[str] = []
    scratch_root = Path("/home/kent/e2e/_harness")
    scratch_root.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tmem-drift-guard-", dir=str(scratch_root))
    try:
        dest = Path(tmp) / "repo"
        build_tree(dest)

        rc, out = run(dest)
        if rc != 0:
            failures.append(f"pristine copy should PASS but exited {rc}:\n{out[:800]}")
        else:
            print("PASS  pristine copy accepted")

        for name, expect, mutate in cases():
            # every case starts from the pristine tree: undo the previous case
            for rel in sorted(MUTATED):
                restore(dest, rel)
            mutate(dest)
            rc, out = run(dest)
            if rc == 0:
                failures.append(f"{name}: the verifier accepted the mutation (exit 0)")
            elif expect not in out:
                failures.append(f"{name}: exit {rc} but the message does not mention {expect!r}:\n{out[:600]}")
            else:
                line = next((ln.strip() for ln in out.splitlines() if expect in ln), "")
                print(f"PASS  {name} -> {line[:110]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("\nFAIL — the guard did not catch every mutation:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nPASS — all {len(cases())} mutations were refused with a specific reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
