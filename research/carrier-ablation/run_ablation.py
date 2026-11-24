#!/usr/bin/env python3
"""Run the carrier-ablation probe sessions.

One session per (arm, rep, variant). The task text is FROZEN in
`oracle_carrier_ablation.json` (pre-registered) and is byte-identical across
arms -- the arms differ only in what memory carrier is mounted and how it can
be reached.

Per-arm environment
-------------------
  A1 : DSH_HOME=plain  (no tmem in the home patch)  + workspace tmem patch
       -> tmem mounted, auto-injection ON
  A2 : same, but PATH resolves a PATCHED tmem package and
       TMEM_ABLATE_NO_INJECT=1 -> tmem mounted, auto-injection OFF
  B1 : DSH_HOME=md     (md-carrier plugin) + `tmem` CLI blocked on PATH
       -> prose MEMORY.md injected in full
  B2 : DSH_HOME=plain, `tmem` CLI blocked -> prose MEMORY.md as a plain file
  C  : DSH_HOME=plain, `tmem` CLI blocked -> no memory at all

Usage
-----
  python3 run_ablation.py --exp-root /home/kent/e2e/abl --arms A1,A2,B1,B2,C \
      --variants free,zero --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HARNESS = Path("/home/kent/e2e/_harness")
ABL = HARNESS / "ablation"
CRED_FILE = Path(os.environ.get("DSH_CREDENTIALS", str(Path.home() / ".dsh" / ".credentials.yaml")))
API_KEY_ENV = "MINIMAX_CN_API_KEY"


def read_api_key() -> str:
    """Read the API key from the DSH credential store without echoing it."""
    text = CRED_FILE.read_text(encoding="utf-8")
    m = re.search(rf"^\s*{re.escape(API_KEY_ENV)}\s*:\s*(.+)$", text, re.MULTILINE)
    if not m:
        raise SystemExit(f"cannot find {API_KEY_ENV} in {CRED_FILE}")
    return m.group(1).strip().strip("'\"")


def arm_env(root: Path, arm: str) -> dict:
    env = dict(os.environ)
    env[API_KEY_ENV] = read_api_key()
    homes = root / "homes"
    if arm == "B1":
        env["DSH_HOME"] = str(homes / "md")
    else:
        env["DSH_HOME"] = str(homes / "plain")
    # the global `tmem` CLI must be unreachable in every arm except the tmem ones
    if arm in ("B2", "C"):
        env["PATH"] = f"{root / 'blockbin'}:{env.get('PATH', '')}"
    elif arm == "A2":
        env["PATH"] = f"{root / 'a2bin'}:{env.get('PATH', '')}"
        env["TMEM_ABLATE_NO_INJECT"] = "1"
    return env


def newest_transcript(ws: Path, dsh_home: Path) -> str | None:
    encoded = "--" + str(ws).strip("/").replace("/", "-") + "--"
    root = dsh_home / "sessions"
    best, best_mtime = None, -1.0
    if not root.exists():
        return None
    for sess in root.iterdir():
        if sess.name != encoded:
            continue
        for path in sess.rglob("session.v4.jsonl*"):
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = path, m
    return str(best) if best else None


def run_one(root: Path, entry: dict, variant: str, prompt: str, timeout: int) -> dict:
    ws = Path(entry["workspace"])
    home = Path(entry["dsh_home"])
    out_dir = root / "raw" / entry["arm"] / f"rep{entry['rep']}" / variant
    out_dir.mkdir(parents=True, exist_ok=True)

    patch = entry.get("patch")
    cmd = ["dsh", "--profile", "headless"]
    if patch and Path(patch).exists():
        cmd += ["--patch", patch]
    cmd += ["headless", prompt]

    env = arm_env(root, entry["arm"])
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, timeout=timeout)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        code = -1
    elapsed = time.time() - started

    (out_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (out_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    record = {
        "arm": entry["arm"],
        "rep": entry["rep"],
        "variant": variant,
        "workspace": str(ws),
        "dsh_home": str(home),
        "exit_code": code,
        "elapsed_sec": round(elapsed, 1),
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "prompt_sha": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest()[:16],
        "transcript_path": newest_transcript(ws, home),
    }
    (out_dir / "scene.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def already_done(root: Path, entry: dict, variant: str) -> bool:
    """A session counts as done only if it exited 0 AND produced a transcript.

    (The quota wall of 2026-11-23 23:10 left several sessions with exit=1 and
    no transcript; those must be re-run, not skipped.)
    """
    d = root / "raw" / entry["arm"] / f"rep{entry['rep']}" / variant
    f = d / "scene.json"
    if not f.exists():
        return False
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if rec.get("exit_code") != 0:
        return False
    tp = rec.get("transcript_path")
    return bool(tp) and Path(tp).exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp-root", default="/home/kent/e2e/abl")
    ap.add_argument("--arms", default="A1,A2,B1,B2,C")
    ap.add_argument("--variants", default="free,zero")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--resume", action="store_true", help="skip sessions that already succeeded")
    args = ap.parse_args()

    root = Path(args.exp_root)
    oracle = json.loads((ABL / "oracle_carrier_ablation.json").read_text(encoding="utf-8"))
    prompts = oracle["prompts"]
    manifest = json.loads((root / "arms_manifest.json").read_text(encoding="utf-8"))

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    summary = []
    for entry in manifest:
        if entry["arm"] not in arms:
            continue
        if entry["rep"] > args.reps:
            continue
        for variant in variants:
            if args.resume and already_done(root, entry, variant):
                print("[abl] skip %s rep%s %s (already succeeded)" % (entry["arm"], entry["rep"], variant), flush=True)
                continue
            prompt = prompts[variant]
            print(
                "[abl] %s rep%s %s ..." % (entry["arm"], entry["rep"], variant),
                flush=True,
            )
            rec = run_one(root, entry, variant, prompt, args.timeout)
            summary.append(rec)
            print(
                "[abl] done %s rep%s %s: exit=%s elapsed=%ss out=%sB"
                % (
                    entry["arm"],
                    entry["rep"],
                    variant,
                    rec["exit_code"],
                    rec["elapsed_sec"],
                    rec["stdout_bytes"],
                ),
                flush=True,
            )
            (root / "run_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print("[abl] all sessions done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
