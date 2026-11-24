#!/usr/bin/env python3
"""Score the carrier ablation study (docs/dsh-memory-carrier-ablation.md).

Reads the pre-registered oracle (prompts, keypoints, thresholds -- all frozen
before execution) and the raw sessions produced by run_ablation.py, then
computes, per arm and variant:

  * knowledge_tokens = injected block tokens + tool-output tokens
                        (the SAME accounting as Round 2's score_h6.py)
  * hit_rate        = 3 memory-exclusive keypoint questions, scored on stdout
  * validity        = mechanical carrier-confusion checks (the protocol's
                      invalidity rules: a control arm that shows tmem tools or a
                      tmem memory block is invalid, not silently kept)

Writes  metrics-carrier-ablation.json  +  metrics-carrier-ablation.md

Usage
-----
  python3 score_carrier_ablation.py --exp-root /home/kent/e2e/abl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path

HARNESS = Path("/home/kent/e2e/_harness")
ABL = HARNESS / "ablation"

try:
    sys.path.insert(0, str(HARNESS / ".pylibs"))
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENC = None


def ntok(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    # fallback: CJK chars ~1 token, ASCII ~4 chars/token
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    other = len(text) - cjk
    return cjk + max(1, other // 4)


# ------------------------------------------------------------------ transcript
def decode_transcript(path: Path) -> str:
    raw = path.read_bytes()
    try:
        import zstandard  # type: ignore

        dctx = zstandard.ZstdDecompressor()
        chunks, pos = [], 0
        while pos < len(raw):
            d = dctx.decompressobj()
            out = d.decompress(raw[pos:])
            chunks.append(out)
            consumed = len(raw) - pos - len(d.unused_data)
            if consumed <= 0:
                break
            pos += consumed
        return b"".join(chunks).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        import subprocess

        return subprocess.run(["zstd", "-dc", str(path)], capture_output=True).stdout.decode(
            "utf-8", "replace"
        )


def transcript_events(path: Path) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    events = []
    for line in decode_transcript(Path(path)).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def classify_path(path: str) -> str:
    """Which carrier a read belongs to.

    The free variant lets the agent read source code as well as the memory
    carrier. Code reading is problem-solving cost, NOT knowledge-acquisition
    cost -- so the split is reported as a descriptive decomposition (no
    threshold depends on it). Paths may be absolute or relative.
    """
    p = (path or "").lower().replace("\\", "/")
    if any(k in p for k in ("memory.md", "memories.md", "/mem.md", ".tmem/")):
        return "carrier"
    if any(k in p for k in ("src/", "tests/", "test_", "pyproject", "readme")):
        return "code"
    return "other"


# --------------------------------------------------------- isolation audit
# Amendment A7 (oracle v1.2): a session that reaches OUTSIDE the container's
# mount allow-list has left its arm -- that sample is invalid, mechanically,
# and the reason is machine readable. Phase 1's contamination was found by a
# manual audit after the fact; this makes it a property of the apparatus.
EXACT_ONLY_ROOTS = {"/", "/home", "/root", "/srv", "/opt", "/mnt", "/media", "/run", "/sys"}
_ABS_PATH_RE = re.compile(r"(?<![\w~.$])/[A-Za-z0-9_.+@%/-]+")


def _allowed(path: str, allowlist: list[str]) -> bool:
    p = os.path.normpath(path)
    for entry in allowlist:
        e = entry.rstrip("/") or "/"
        if e in EXACT_ONLY_ROOTS:
            if p == e:
                return True
            continue
        if p == e or p.startswith(e + "/"):
            return True
    return False


def audit_out_of_arm(events: list[dict], allowlist: list[str]) -> dict:
    """Every path the session touched, split into allowed / out-of-arm."""
    touched: list[str] = []
    out_of_arm: list[dict] = []
    for ev in events:
        if ev.get("type") != "tool/call":
            continue
        data = ev.get("data") or {}
        name = str(data.get("name") or "")
        raw = data.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        candidates: list[str] = []
        for key in ("file_path", "path", "filename", "file", "notebook_path"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        cmd = args.get("command") or args.get("CommandLine") or args.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            candidates.extend(_ABS_PATH_RE.findall(cmd))
        for cand in candidates:
            if not cand.startswith("/"):
                continue  # relative -> resolved inside the workspace
            norm = os.path.normpath(cand)
            touched.append(norm)
            if not _allowed(norm, allowlist):
                out_of_arm.append({"tool": name, "path": norm})
    # de-duplicate, keep the order of first appearance
    seen, uniq = set(), []
    for item in out_of_arm:
        key = (item["tool"], item["path"])
        if key not in seen:
            seen.add(key)
            uniq.append(item)
    return {
        "paths_touched": sorted(set(touched)),
        "out_of_arm": uniq,
        "out_of_arm_count": len(uniq),
    }


def session_cost(events: list[dict]) -> dict:
    injected_text = ""
    read_outputs: list[str] = []
    read_calls = 0
    read_paths: list[str] = []
    tmem_tool_calls: list[str] = []
    by_category: dict[str, int] = {"carrier": 0, "code": 0, "other": 0}

    # exact pairing: the tool/call event carries a callId, and the tool/result
    # message carries the same callId (results are batched and may come back
    # in a different order than the calls, so positional pairing drifts)
    call_category: dict[str, str] = {}
    for ev in events:
        etype = ev.get("type")
        if etype == "user/message":
            s = json.dumps(ev, ensure_ascii=False)
            if "<tmem_speculative_memory>" in s:
                injected_text += s
            elif '"kind": "md-carrier"' in s or '"kind":"md-carrier"' in s:
                injected_text += s
            continue
        if etype == "tool/call":
            data = ev.get("data") or {}
            name = str(data.get("name") or "")
            raw = data.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                args = {}
            if "tmem" in name.lower():
                tmem_tool_calls.append(name)
                cat = "carrier"
            elif name in ("read", "read_file", "view_file"):
                read_calls += 1
                fp = args.get("file_path") or args.get("path") or args.get("Path")
                if fp:
                    read_paths.append(str(fp))
                cat = classify_path(str(fp or ""))
            else:
                cmd = ""
                if name in ("bash", "pwsh", "run_command"):
                    cmd = str(args.get("command") or args.get("CommandLine") or "")
                    if cmd.strip():
                        read_outputs.append(cmd)
                cat = "other"
            cid = data.get("callId")
            if cid:
                call_category[str(cid)] = cat
            continue
        if etype == "tool/result":
            data = ev.get("data") or {}
            msg = data.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            txt = ""
            if isinstance(content, list):
                txt = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            elif isinstance(content, str):
                txt = content
            if not txt and isinstance(msg, str):
                txt = msg
            if txt:
                read_outputs.append(txt)
                cat = "other"
                cid = None
                if isinstance(msg, dict):
                    cid = msg.get("toolCallId") or (msg.get("source") or {}).get("callId")
                if not cid:
                    m = re.search(r"callId['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", txt)
                    cid = m.group(1) if m else None
                if cid:
                    cat = call_category.get(str(cid), "other")
                by_category[cat] = by_category.get(cat, 0) + ntok(txt)

    return {
        "tmem_block_injected": "<tmem_speculative_memory>" in injected_text,
        "md_carrier_injected": '"kind": "md-carrier"' in injected_text
        or '"kind":"md-carrier"' in injected_text,
        "injected_bytes": len(injected_text.encode("utf-8")),
        "injected_tokens": ntok(injected_text),
        "read_calls": read_calls,
        "read_paths": read_paths,
        "read_output_bytes": sum(len(o.encode("utf-8")) for o in read_outputs),
        "read_output_tokens": ntok("\n".join(read_outputs)),
        "read_output_tokens_by_category": by_category,
        "tmem_tool_calls": tmem_tool_calls,
        "event_count": len(events),
    }


# ------------------------------------------------------------------- scoring
def score_answers(text: str, keypoints: list[dict]) -> dict:
    low = (text or "").lower()
    rows = {}
    for q in keypoints:
        req = [k.lower() for group in q["groups"] for k in group]
        groups = [[k.lower() for k in group] for group in q["groups"]]
        positions, missing, missing_groups = [], [], []
        for group in groups:
            found = None
            for k in group:
                idx = low.find(k)
                if idx >= 0:
                    found = (idx, k)
                    break
            if found is None:
                missing_groups.append(group)
            else:
                positions.append(found)
        hit = not missing_groups
        note = ""
        if hit and q.get("ordered") and len(groups) > 1:
            ordered = [k for _, k in sorted(positions)]
            expected = [g[0] for g in groups]
            if ordered != expected:
                hit = False
                note = "all keypoints present but in the wrong order"
        rows[q["id"]] = {
            "ask": q.get("ask"),
            "hit": hit,
            "matched_groups": [g for g, _ in positions],
            "missing_groups": missing_groups,
            "note": note,
        }
    hits = sum(1 for r in rows.values() if r["hit"])
    return {"questions": rows, "hits": hits, "total": len(rows), "hit_rate": round(hits / len(rows), 3) if rows else 0.0}


def mean_ci(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "n": 0, "ci95": None, "values": []}
    m = statistics.mean(vals)
    if len(vals) == 1:
        return {"mean": round(m, 4), "n": 1, "ci95": None, "values": [round(v, 4) for v in vals]}
    sd = statistics.stdev(vals)
    tcrit = 4.302652729911275  # t(0.975, df=2) for n=3
    half = tcrit * sd / math.sqrt(len(vals))
    return {
        "mean": round(m, 4),
        "n": len(vals),
        "sd": round(sd, 4),
        "ci95": [round(m - half, 4), round(m + half, 4)],
        "values": [round(v, 4) for v in vals],
    }


# -------------------------------------------------------------------- driver
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp-root", default="/home/kent/e2e/abl")
    ap.add_argument("--oracle", default=str(ABL / "oracle_carrier_ablation.json"))
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-md", default="")
    args = ap.parse_args()
    root = Path(args.exp_root)

    oracle = json.loads(Path(args.oracle).read_text(encoding="utf-8"))
    allowlist = oracle.get("isolation_allowlist", [])
    keypoints = oracle["keypoints"]
    manifest = json.loads((root / "arms_manifest.json").read_text(encoding="utf-8"))

    sessions = []
    for entry in manifest:
        for variant in oracle["variants"]:
            d = root / "raw" / entry["arm"] / f"rep{entry['rep']}" / variant
            if not (d / "scene.json").exists():
                continue
            rec = json.loads((d / "scene.json").read_text(encoding="utf-8"))
            stdout = (d / "stdout.log").read_text(encoding="utf-8") if (d / "stdout.log").exists() else ""
            events = transcript_events(Path(rec.get("transcript_path") or ""))
            cost = session_cost(events)
            ans = score_answers(stdout, keypoints)
            isolation = audit_out_of_arm(events, allowlist) if allowlist else {"out_of_arm": [], "out_of_arm_count": 0, "paths_touched": []}

            reasons = []
            arm = entry["arm"]
            stderr_txt = (d / "stderr.log").read_text(encoding="utf-8") if (d / "stderr.log").exists() else ""
            quota_hit = "RATE_LIMIT" in stderr_txt or "429" in stderr_txt
            if rec.get("exit_code") != 0:
                reasons.append(
                    "session failed (exit=%s)%s"
                    % (rec.get("exit_code"), ", model API quota (429)" if quota_hit else "")
                )
            if not events:
                reasons.append("transcript missing or empty")
            for item in isolation.get("out_of_arm", []):
                reasons.append("out_of_arm_read:%s (%s)" % (item["path"], item["tool"]))
            if arm in ("B1", "B2", "C"):
                if cost["tmem_tool_calls"]:
                    reasons.append(f"carrier confusion: tmem tools used {cost['tmem_tool_calls']}")
                if cost["tmem_block_injected"]:
                    reasons.append("carrier confusion: tmem memory block injected")
            elif rec.get("exit_code") == 0 and events:
                if not (cost["tmem_block_injected"] or cost["tmem_tool_calls"]):
                    reasons.append("tmem not mounted (no injection and no tool call)")
            if arm == "B1" and rec.get("exit_code") == 0 and events and not cost["md_carrier_injected"]:
                reasons.append("prose carrier not injected")
            if arm in ("A1", "A2") and rec.get("exit_code") == 0 and cost["md_carrier_injected"]:
                reasons.append("carrier confusion: prose block injected in a tmem arm")

            sessions.append(
                {
                    "arm": arm,
                    "rep": entry["rep"],
                    "variant": variant,
                    "valid": not reasons,
                    "invalid_reasons": reasons,
                    "exit_code": rec.get("exit_code"),
                    "elapsed_sec": rec.get("elapsed_sec"),
                    "knowledge_tokens": cost["injected_tokens"] + cost["read_output_tokens"],
                    "injected_tokens": cost["injected_tokens"],
                    "read_output_tokens": cost["read_output_tokens"],
                    "read_output_tokens_by_category": cost["read_output_tokens_by_category"],
                    "read_calls": cost["read_calls"],
                    "read_paths": cost["read_paths"][:12],
                    "tmem_block_injected": cost["tmem_block_injected"],
                    "md_carrier_injected": cost["md_carrier_injected"],
                    "tmem_tool_calls": cost["tmem_tool_calls"],
                    "hit_rate": ans["hit_rate"],
                    "hits": ans["hits"],
                    "questions": ans["questions"],
                    "isolation": {
                        "mechanism": rec.get("isolation"),
                        "container_workspace": rec.get("container_workspace"),
                        "out_of_arm_count": isolation.get("out_of_arm_count", 0),
                        "out_of_arm": isolation.get("out_of_arm", []),
                        "paths_touched": isolation.get("paths_touched", [])[:40],
                    },
                }
            )

    # aggregate
    agg: dict = {}
    for s in sessions:
        key = f"{s['arm']}|{s['variant']}"
        a = agg.setdefault(
            key,
            {
                "arm": s["arm"],
                "variant": s["variant"],
                "knowledge_tokens": [],
                "hit_rate": [],
                "injected_tokens": [],
                "read_output_tokens": [],
                "read_calls": [],
                "invalid": [],
            },
        )
        if not s["valid"]:
            a["invalid"].append({"rep": s["rep"], "reasons": s["invalid_reasons"]})
            continue
        a["knowledge_tokens"].append(s["knowledge_tokens"])
        a["hit_rate"].append(s["hit_rate"])
        a["injected_tokens"].append(s["injected_tokens"])
        a["read_output_tokens"].append(s["read_output_tokens"])
        a["read_calls"].append(s["read_calls"])
    for a in agg.values():
        a["knowledge_tokens_stats"] = mean_ci(a["knowledge_tokens"])
        a["hit_rate_stats"] = mean_ci(a["hit_rate"])
        a["injected_tokens_stats"] = mean_ci(a["injected_tokens"])
        a["read_output_tokens_stats"] = mean_ci(a["read_output_tokens"])
        a["read_calls_stats"] = mean_ci(a["read_calls"])

    def g(arm: str, variant: str, field: str = "mean"):
        a = agg.get(f"{arm}|{variant}")
        if not a:
            return None
        if field == "mean":
            return a["knowledge_tokens_stats"]["mean"]
        return a["hit_rate_stats"]["mean"]

    # ---- gates (thresholds come from the frozen oracle) ----
    facts_path = root / "carrier" / "carrier_facts.json"
    equivalence = json.loads(facts_path.read_text(encoding="utf-8")) if facts_path.exists() else {}
    gates = {}

    def add_gate(key: str, value, threshold: str, passed) -> None:
        gates[key] = {
            "name": oracle["gates"][key]["name"],
            "rule": oracle["gates"][key]["rule"],
            "threshold": threshold,
            "value": value,
            "pass": passed,
        }

    if oracle["gates"]["CA-5"].get("executed"):
        ok = bool(equivalence.get("equivalence_ok"))
        add_gate(
            "CA-5",
            {
                "episodes": equivalence.get("episode_count"),
                "facts": equivalence.get("facts"),
                "md_bytes": equivalence.get("md_bytes"),
            },
            oracle["gates"]["CA-5"]["threshold"],
            ok,
        )

    for key, arm_b in (("CA-1", "B2"), ("CA-1b", "B1")):
        if not oracle["gates"][key].get("executed"):
            continue
        a1, bx = g("A1", "free"), g(arm_b, "free")
        passed = None if a1 is None or bx is None else a1 <= 0.30 * bx
        add_gate(
            key,
            {
                "A1_free_mean_knowledge_tokens": a1,
                f"{arm_b}_free_mean_knowledge_tokens": bx,
                "ratio": None if not a1 or bx is None else round(a1 / bx, 4),
            },
            oracle["gates"][key]["threshold"],
            passed,
        )

    if oracle["gates"]["CA-4"].get("executed"):
        a1, a2 = g("A1", "free"), g("A2", "free")
        b1, b2 = g("B1", "free"), g("B2", "free")
        hr = {k: (agg.get(f"{k}|free") or {}).get("hit_rate_stats", {}).get("mean") for k in ("A1", "A2", "B1", "B2")}
        passed = None
        if None not in (a1, a2, b1, b2):
            passed = (
                a1 <= a2
                and b1 <= b2
                and (hr["A1"] or 0) >= (hr["A2"] or 0)
                and (hr["B1"] or 0) >= (hr["B2"] or 0)
            )
        add_gate(
            "CA-4",
            {"knowledge_tokens": {"A1": a1, "A2": a2, "B1": b1, "B2": b2}, "hit_rate": hr},
            oracle["gates"]["CA-4"]["threshold"],
            passed,
        )

    if oracle["gates"]["CA-E1"].get("executed"):
        # CA-E1 is judged on the `zero` variant (amendment A2-CA-E1-variant):
        # only there is no way to read the answer out of the source code.
        a1 = (agg.get("A1|zero") or {}).get("hit_rate_stats", {}).get("mean")
        c = (agg.get("C|zero") or {}).get("hit_rate_stats", {}).get("mean")
        b2 = (agg.get("B2|zero") or {}).get("hit_rate_stats", {}).get("mean")
        passed = None if None in (a1, c, b2) else (a1 > c and a1 >= b2)
        add_gate(
            "CA-E1",
            {
                "variant": "zero",
                "A1_zero_hit_rate": a1,
                "C_zero_hit_rate": c,
                "B2_zero_hit_rate": b2,
                "free_variant_hit_rate_descriptive": {
                    k: (agg.get(f"{k}|free") or {}).get("hit_rate_stats", {}).get("mean")
                    for k in ("A1", "A2", "B1", "B2", "C")
                },
            },
            oracle["gates"]["CA-E1"]["threshold"],
            passed,
        )

    for key, spec in oracle["gates"].items():
        if not spec.get("executed") and key not in gates:
            gates[key] = {
                "name": spec.get("name", key),
                "rule": spec.get("rule"),
                "threshold": spec.get("threshold"),
                "value": None,
                "pass": None,
                "not_executed": spec.get("reason"),
            }

    # ---- descriptive statistics (no thresholds, no verdicts) ----
    def m(arm: str, variant: str, field: str):
        a = agg.get(f"{arm}|{variant}") or {}
        return (a.get(f"{field}_stats") or {}).get("mean")

    a1_inj = m("A1", "free", "injected_tokens")
    b2_total = m("B2", "free", "knowledge_tokens")
    a1_total = m("A1", "free", "knowledge_tokens")
    descriptive = {
        "injection_only_ratio": None if not a1_inj or not b2_total else round(a1_inj / b2_total, 4),
        "free_variant_total_ratio": None if not a1_total or not b2_total else round(a1_total / b2_total, 4),
        "A1_free_mean_injected_tokens": a1_inj,
        "A1_free_mean_knowledge_tokens": a1_total,
        "B2_free_mean_knowledge_tokens": b2_total,
        "read_output_tokens_by_category": {
            arm: {
                "carrier": round(
                    statistics.mean(
                        [
                            s["read_output_tokens_by_category"].get("carrier", 0)
                            for s in sessions
                            if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                        ]
                    ),
                    1,
                )
                if [
                    s
                    for s in sessions
                    if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                ]
                else None,
                "code": round(
                    statistics.mean(
                        [
                            s["read_output_tokens_by_category"].get("code", 0)
                            for s in sessions
                            if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                        ]
                    ),
                    1,
                )
                if [
                    s
                    for s in sessions
                    if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                ]
                else None,
                "other": round(
                    statistics.mean(
                        [
                            s["read_output_tokens_by_category"].get("other", 0)
                            for s in sessions
                            if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                        ]
                    ),
                    1,
                )
                if [
                    s
                    for s in sessions
                    if s["arm"] == arm and s["variant"] == "free" and s["valid"]
                ]
                else None,
            }
            for arm in ("A1", "A2", "B1", "B2", "C")
        },
        "note": "carrier = 读取记忆载体（MEMORY.md / MEMORIES.md / .tmem）；code = 读取源码与测试；other = 其余工具输出（如命令回显）。知识获取 token 的正式口径把三者都计入，因此 free 变体的绝对值含了解题成本。",
    }

    out = {
        "study": "carrier-ablation",
        "oracle_file": args.oracle,
        "oracle_frozen_at": oracle["frozen_at"],
        "oracle_amended_at": oracle.get("amended_at"),
        "exp_root": str(root),
        "arms": oracle["arms"],
        "sessions": sessions,
        "aggregate": agg,
        "descriptive": descriptive,
        "gates": gates,
    }
    out_json = Path(args.out_json) if args.out_json else (root / "metrics-carrier-ablation.json")
    out_md = Path(args.out_md) if args.out_md else (root / "metrics-carrier-ablation.md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- markdown ----
    lines = [
        "# 载体消融课题 · 机器判定结果",
        "",
        f"> 预注册时间：{oracle['frozen_at']}（阈值与提示词执行前冻结）｜ 装置：`{ABL}`",
        "",
        "## 逐臂结果（free 变体 = 允许查阅项目记忆）",
        "",
        "| 臂 | 载体/获取 | 有效样本 | 知识获取 token（均值, 95%CI） | 注入 token | 读取 token | 读取次数 | 命中率（均值, 95%CI） |",
        "| :--- | :--- | :---: | :--- | :---: | :---: | :---: | :--- |",
    ]
    desc = {
        "A1": "tmem / 自动注入",
        "A2": "tmem / 按需检索",
        "B1": "散文 MD / 全量注入",
        "B2": "散文 MD / 按需读取",
        "C": "无记忆",
    }
    for arm in ("A1", "A2", "B1", "B2", "C"):
        a = agg.get(f"{arm}|free") or {}
        kt = a.get("knowledge_tokens_stats", {})
        hr = a.get("hit_rate_stats", {})
        ci = kt.get("ci95")
        hci = hr.get("ci95")
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                arm,
                desc[arm],
                kt.get("n", 0),
                f"{kt.get('mean')} ({ci[0]}–{ci[1]})" if ci else kt.get("mean"),
                (a.get("injected_tokens_stats") or {}).get("mean"),
                (a.get("read_output_tokens_stats") or {}).get("mean"),
                (a.get("read_calls_stats") or {}).get("mean"),
                f"{hr.get('mean')} ({hci[0]}–{hci[1]})" if hci else hr.get("mean"),
            )
        )
    lines += ["", "## 描述性统计（不构成门槛判定）", "", "```", json.dumps(descriptive, ensure_ascii=False, indent=2), "```", ""]
    lines += ["", "## 门槛判定", "", "| 门槛 | 判定 | 值 | 阈值 |", "| :--- | :---: | :--- | :--- |"]
    for key, gg in gates.items():
        verdict = "—" if gg.get("pass") is None else ("✅ PASS" if gg["pass"] else "❌ FAIL")
        val = gg.get("value")
        val_s = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
        if len(val_s) > 160:
            val_s = val_s[:157] + "…"
        lines.append(f"| {key} {gg.get('name','')} | {verdict} | `{val_s}` | {gg.get('threshold')} |")
    invalid = [
        (s["arm"], s["rep"], s["variant"], s["invalid_reasons"])
        for s in sessions
        if not s["valid"]
    ]
    lines += ["", "## 作废样本（机械判定，不允许人工挑选）", ""]
    if invalid:
        for arm, rep, variant, reasons in invalid:
            lines.append(f"- **{arm} rep{rep} {variant}**：{'；'.join(reasons)}")
    else:
        lines.append("- 无")
    (out_md).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
