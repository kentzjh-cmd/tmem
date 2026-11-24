#!/usr/bin/env python3
"""Render the prose (MD) memory carrier from the tmem structured carrier.

Why this exists
---------------
The carrier ablation study (docs/dsh-memory-carrier-ablation.md) has ONE source
of truth for the *facts* and TWO ways of *carrying* them:

    tmem carrier : <workspace>/.tmem/**        (structured: episodes/traces)
    prose carrier: <workspace>/MEMORY.md       (a human-written MD record)

Both carriers must contain the SAME fact set (protocol CA-5, content
equivalence), otherwise we would be comparing "who remembers more" instead of
"which carrier is better". So the MD file is *rendered mechanically* from the
episode records of the tmem carrier -- never written by a human or an LLM --
which makes equivalence true by construction and independently checkable.

The renderer is faithful: every sentence comes from an episode field. It never
adds, paraphrases or summarises.

Usage
-----
    python3 render_prose_carrier.py --src <workspace with .tmem> \
        --out <dir for MEMORY.md + carrier_facts.json>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# The probe keypoints are read from the PRE-REGISTERED oracle, so the
# equivalence check validates exactly the keypoints the scorer will use.
# (v1.0 of the oracle carried the Round 2 keypoints; v1.1 re-pointed them at
# the facts this study's own seeding produced -- see oracle.amendments.)
_ORACLE = Path(__file__).with_name("oracle_carrier_ablation.json")
KEYPOINT_GROUPS = json.loads(_ORACLE.read_text(encoding="utf-8"))["keypoints"]


def episode_text(ep: dict) -> str:
    """All human-meaningful fields of one episode, flattened to text."""
    parts: list[str] = []
    for key in ("phase_intent",):
        v = ep.get(key)
        if v:
            parts.append(str(v))
    for d in ep.get("decisions") or []:
        parts.append(str(d))
    for v in ep.get("l2_causal_repairs") or []:
        parts.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
    for v in ep.get("verification_cmds") or []:
        parts.append(str(v))
    for v in ep.get("error_signatures") or []:
        parts.append(str(v))
    for v in ep.get("touched_files") or []:
        parts.append(str(v))
    for v in ep.get("touched_symbols") or []:
        parts.append(str(v))
    return "\n".join(parts)


def render_episode_md(ep: dict) -> str:
    """One episode -> one prose record (same facts, no structure)."""
    lines: list[str] = []
    intent = str(ep.get("phase_intent") or "").strip()
    title = intent.split("|")[0].strip() if intent else (ep.get("episode_id") or "episode")
    lines.append(f"### {title}")
    lines.append("")
    if intent:
        lines.append(f"- 结论与根因：{intent}")
    errs = [str(e) for e in (ep.get("error_signatures") or [])]
    if errs:
        lines.append(f"- 涉及错误签名：{'、'.join(errs)}")
    # repair order: prefer the L2 causal repair chain (it carries the order)
    order: list[str] = []
    for rep in ep.get("l2_causal_repairs") or []:
        if isinstance(rep, dict):
            action = str(rep.get("repair_action") or "")
            for m in re.finditer(r"(?:edit|create|update)\s+([^\s(]+)", action):
                f = m.group(1)
                if f not in order:
                    order.append(f)
            cmd = rep.get("failed_command") or rep.get("verification_cmd")
            if cmd:
                lines.append(f"- 出错/验证命令：`{cmd}`")
    if not order:
        order = [str(f) for f in (ep.get("touched_files") or [])]
    if order:
        lines.append(f"- 改动文件（按修复顺序）：{' → '.join(order)}")
    touched = [str(f) for f in (ep.get("touched_files") or [])]
    if touched and touched != order:
        lines.append(f"- 涉及文件：{'、'.join(touched)}")
    verif = [str(v) for v in (ep.get("verification_cmds") or [])]
    if verif:
        lines.append(f"- 验证命令：`{verif[0]}`")
    for v in verif[1:]:
        lines.append(f"  - `{v}`")
    for d in ep.get("decisions") or []:
        lines.append(f"- 决策要点：{d}")
    lines.append("")
    return "\n".join(lines)


def pick_target_episode(episodes: list[dict]) -> dict | None:
    """The episode that carries the probe keypoints (deterministic)."""
    best = None
    best_score = -1
    for ep in episodes:
        text = episode_text(ep).lower()
        score = 0
        for grp in KEYPOINT_GROUPS:
            if any(any(k.lower() in text for k in alts) for alts in grp["groups"]):
                score += 1
        if score > best_score:
            best, best_score = ep, score
    return best if best_score >= 2 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="workspace containing .tmem/")
    ap.add_argument("--out", required=True, help="output dir for MEMORY.md + carrier_facts.json")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ep_files = sorted((src / ".tmem" / "episodes").glob("*.json"))
    if not ep_files:
        print("[render] FATAL: no episodes found under", src / ".tmem" / "episodes")
        return 3
    episodes = [json.loads(p.read_text(encoding="utf-8")) for p in ep_files]

    md_parts = [
        "# 项目工程决策记录",
        "",
        "> 本文件记录本项目历次修复中沉淀的工程决策与验证方式。",
        "",
    ]
    for ep in episodes:
        md_parts.append(render_episode_md(ep))
    md = "\n".join(md_parts).rstrip() + "\n"
    (out / "MEMORY.md").write_text(md, encoding="utf-8")

    target = pick_target_episode(episodes)
    tmem_text = episode_text(target).lower() if target else ""
    md_low = md.lower()

    facts = []
    for grp in KEYPOINT_GROUPS:
        in_tmem = all(any(k.lower() in tmem_text for k in alts) for alts in grp["groups"])
        in_md = all(any(k.lower() in md_low for k in alts) for alts in grp["groups"])
        facts.append(
            {
                "id": grp["id"],
                "ask": grp.get("ask", grp["id"]),
                "groups": grp["groups"],
                "ordered": grp["ordered"],
                "in_tmem_carrier": in_tmem,
                "in_md_carrier": in_md,
            }
        )

    payload = {
        "source_workspace": str(src),
        "episode_count": len(episodes),
        "target_episode_id": (target or {}).get("episode_id"),
        "target_episode_intent": str((target or {}).get("phase_intent") or "")[:400],
        "md_bytes": len(md.encode("utf-8")),
        "md_file": str(out / "MEMORY.md"),
        "facts": facts,
        "equivalence_ok": all(f["in_tmem_carrier"] and f["in_md_carrier"] for f in facts),
    }
    (out / "carrier_facts.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        "[render] episodes=%d md_bytes=%d target=%s equivalence=%s"
        % (
            len(episodes),
            payload["md_bytes"],
            payload["target_episode_id"],
            "OK" if payload["equivalence_ok"] else "FAIL",
        )
    )
    for f in facts:
        print(
            "   - %s tmem=%s md=%s"
            % (f["id"], f["in_tmem_carrier"], f["in_md_carrier"])
        )
    return 0 if payload["equivalence_ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
