#!/usr/bin/env python3
"""Mount check for the carrier-ablation arms.

Before spending 30 real probe sessions, prove that each arm is mounted the way
the protocol demands:

  A1 : tmem plugin mounted (tmem_search / tmem_expand in the tool list)
  A2 : tmem plugin mounted as well, but the auto-injection branch is OFF
  B1 : no tmem tools, but the prose carrier IS injected (<md-carrier> notice)
  B2 : no tmem tools, no injection (the prose carrier is just a file)
  C  : no tmem tools, no injection, no memory at all

One trivial session per arm ("只回复 OK") is enough: it costs a handful of
tokens and cannot pollute the study (its transcript lives in the arm's own
DSH_HOME and is not part of the probe dataset).

Usage
-----
  python3 mount_check.py --exp-root /home/kent/e2e/abl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ABL = Path("/home/kent/e2e/_harness/ablation")
HARNESS = Path("/home/kent/e2e/_harness")
sys.path.insert(0, str(ABL))

from run_ablation import arm_env, newest_transcript, read_api_key  # noqa: E402

_spec = importlib.util.spec_from_file_location("analyze_arm", HARNESS / "analyze_arm_contamination.py")
_analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analyze)

TASK = "只回复 OK。不要修改任何文件，不要运行任何命令。"


def audit(ws: Path, dsh_home: Path) -> dict:
    """Tool list + injection markers of the newest session of this workspace."""
    encoded = "--" + str(ws).strip("/").replace("/", "-") + "--"
    root = dsh_home / "sessions" / encoded
    out = {"session_dir": str(root), "tools": [], "tmem_injection": False, "md_injection": False}
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        f = d / "session.v4.jsonl.zstd"
        if not f.exists():
            continue
        text = _analyze.decompress(f)
        tools, injections = set(), 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "request/header":
                # the tool list lives in data.header.tools (verified against
                # analyze_arm_contamination.py -- a flat lookup here produced a
                # false negative on every arm)
                header = (ev.get("data") or {}).get("header") or {}
                for t in header.get("tools") or []:
                    name = t.get("name") if isinstance(t, dict) else t
                    if name:
                        tools.add(str(name))
            if ev.get("type") == "user/message":
                data = ev.get("data") or {}
                payload = "".join(
                    c.get("text", "")
                    for c in (data.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                if "<tmem_speculative_memory>" in payload:
                    injections += 1
                if "项目工程决策记录" in payload:
                    out["md_injection"] = True
        out["tools"] = sorted(tools)
        out["tmem_injection"] = injections > 0
        out["tmem_tools_present"] = bool(_analyze.TMEM_TOOLS & set(tools))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp-root", default="/home/kent/e2e/abl")
    args = ap.parse_args()
    root = Path(args.exp_root)
    manifest = json.loads((root / "arms_manifest.json").read_text(encoding="utf-8"))
    out_dir = root / "mount_check"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for entry in manifest:
        if entry["rep"] != 1:
            continue
        arm = entry["arm"]
        ws = Path(entry["workspace"])
        home = Path(entry["dsh_home"])
        env = arm_env(root, arm)
        cmd = ["dsh", "--profile", "headless"]
        if entry.get("patch") and Path(entry["patch"]).exists():
            cmd += ["--patch", entry["patch"]]
        cmd += ["headless", TASK]
        print(f"[mount] {arm}: {' '.join(cmd[:5])} ...", flush=True)
        proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, timeout=600)
        (out_dir / f"{arm}.stdout.log").write_text(proc.stdout, encoding="utf-8")
        (out_dir / f"{arm}.stderr.log").write_text(proc.stderr, encoding="utf-8")

        a = audit(ws, home)
        a.update({"arm": arm, "exit_code": proc.returncode, "workspace": str(ws), "dsh_home": str(home)})
        if arm in ("A1", "A2"):
            ok = a.get("tmem_tools_present") and not a.get("tmem_injection")
        elif arm == "B1":
            ok = (not a.get("tmem_tools_present")) and a.get("md_injection")
        else:
            ok = (not a.get("tmem_tools_present")) and (not a.get("tmem_injection")) and (not a.get("md_injection"))
        a["expectation_met"] = ok
        results.append(a)
        print(
            "[mount] %s: tmem_tools=%s tmem_injection=%s md_injection=%s -> %s"
            % (
                arm,
                a.get("tmem_tools_present"),
                a.get("tmem_injection"),
                a.get("md_injection"),
                "OK" if ok else "FAIL",
            ),
            flush=True,
        )

    (out_dir / "mount_check.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [r["arm"] for r in results if not r["expectation_met"]]
    print("[mount] all arms OK" if not bad else f"[mount] FAILED arms: {bad}")
    return 0 if not bad else 5


if __name__ == "__main__":
    raise SystemExit(main())
