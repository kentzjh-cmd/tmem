#!/usr/bin/env python3
"""Check the published numbers against the machine verdict.

Why this exists
---------------
A research report is prose that was typed by hand from a JSON file; the
classic failure is *transcription drift* (a mean copied with the wrong
decimal, a CI that no longer matches, a ratio computed from an older run).
The numbers in the two reports must be derivable from
`results/metrics-carrier-ablation-v1.2.json`.

This script re-derives every published figure from the verdict file and
asserts that the exact string appears in the report. Run it after any edit
to the report:

    python3 research/carrier-ablation/verify_report_numbers.py

Exit code 0 = every published number reproduces; 1 = at least one mismatch
(the mismatching value is printed).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parents[2]
RES = REPO / "research" / "carrier-ablation" / "results"
REPORT = REPO / "docs" / "dsh-memory-carrier-ablation-report.md"
PROTOCOL = REPO / "docs" / "dsh-memory-carrier-ablation.md"
MAIN_REPORT = REPO / "docs" / "dsh-independent-e2e-report.md"

# how a number is written in the report (thousands separated by a space)
def fmt(x: float, nd: int = 1) -> str:
    s = f"{x:,.{nd}f}"
    return s.replace(",", " ")


def main() -> int:
    metrics = json.loads((RES / "metrics-carrier-ablation-v1.2.json").read_text(encoding="utf-8"))
    oracle = json.loads(
        (REPO / "research" / "carrier-ablation" / "oracle_carrier_ablation_v1.2.json").read_text(encoding="utf-8")
    )
    agg = metrics["aggregate"]
    sessions = metrics["sessions"]
    texts = {
        "report": REPORT.read_text(encoding="utf-8"),
        "protocol": PROTOCOL.read_text(encoding="utf-8"),
        "main": MAIN_REPORT.read_text(encoding="utf-8"),
    }

    failures: list[str] = []

    def expect(text_key: str, value: str, label: str) -> None:
        if value not in texts[text_key]:
            failures.append(f"{label}: '{value}' not found in {text_key}")

    # ---- per-arm means (report table §4.2) ----
    for arm in ("A1", "A2", "B1", "B2", "C"):
        for variant in ("free", "zero"):
            key = f"{arm}|{variant}"
            st = agg[key]["knowledge_tokens_stats"]
            hr = agg[key]["hit_rate_stats"]
            expect("report", fmt(st["mean"]), f"knowledge_tokens mean {key}")
            expect("report", fmt(hr["mean"], 3), f"hit_rate mean {key}")

    # ---- gate values (report §4.3, protocol §12.4, main §3.14) ----
    g = metrics["gates"]
    expect("report", fmt(g["CA-1"]["value"]["ratio"], 2) + "×", "CA-1 ratio")
    expect("report", fmt(g["CA-1b"]["value"]["ratio"], 2) + "×", "CA-1b ratio")
    expect("report", fmt(g["CA-1"]["value"]["A1_free_mean_knowledge_tokens"]), "CA-1 A1 mean")
    expect("report", fmt(g["CA-1"]["value"]["B2_free_mean_knowledge_tokens"], 0), "CA-1 B2 mean")
    expect("report", fmt(g["CA-1b"]["value"]["B1_free_mean_knowledge_tokens"]), "CA-1b B1 mean")
    expect("report", fmt(g["CA-4"]["value"]["knowledge_tokens"]["A2"]), "CA-4 A2 mean")

    # ---- descriptive ratios (report §4.4, protocol §12.4, main §0/§6) ----
    inj_ratio = metrics["descriptive"]["injection_only_ratio"]
    total_ratio = metrics["descriptive"]["free_variant_total_ratio"]
    for txt_key in ("report", "protocol", "main"):
        expect(txt_key, f"{inj_ratio:.4f}", "injection_only_ratio")
    expect("report", f"{total_ratio:.4f}", "free_variant_total_ratio")
    # the phase-1 value quoted for comparison must come from the archived phase-1 verdict
    p1 = json.loads((RES / "metrics-carrier-ablation.json").read_text(encoding="utf-8"))
    expect("report", f"{p1['descriptive']['injection_only_ratio']:.4f}", "phase-1 injection_only_ratio")
    expect("protocol", f"{p1['descriptive']['injection_only_ratio']:.4f}", "phase-1 injection_only_ratio")

    # ---- zero-variant hit rates (the CA-E1 comparison) ----
    for arm, expect_txt in (("A1", "1.000"), ("C", "0.000"), ("B2", "0.000")):
        v = agg[f"{arm}|zero"]["hit_rate_stats"]["mean"]
        if f"{v:.3f}" != expect_txt:
            failures.append(f"CA-E1 {arm}|zero hit_rate is {v}, report says {expect_txt}")

    # ---- validity accounting (report §4.1) ----
    n_valid = sum(1 for s in sessions if s["valid"])
    n_total = len(sessions)
    if n_valid != 29 or n_total != 30:
        failures.append(f"validity: {n_valid}/{n_total} sessions valid, report claims 29/30")
    n_out = sum(s["isolation"]["out_of_arm_count"] for s in sessions)
    if n_out != 9:
        failures.append(f"out-of-arm reads: {n_out}, report claims 9")

    # ---- threshold sanity: the thresholds must still be the frozen ones ----
    frozen = json.loads(
        (REPO / "research" / "carrier-ablation" / "oracle_carrier_ablation.json").read_text(encoding="utf-8")
    )
    for key in ("prompts", "keypoints", "metrics", "gates"):
        if frozen[key] != oracle[key]:
            failures.append(f"oracle v1.2 changed '{key}' relative to v1.1 (threshold drift!)")

    # ---- the published gate verdicts must equal a recomputation ----
    def m(arm: str, variant: str, field: str):
        vals = [s[field] for s in sessions if s["arm"] == arm and s["variant"] == variant and s["valid"]]
        return mean(vals) if vals else None

    a1f, b2f, b1f, a2f = (m("A1", "free", "knowledge_tokens"), m("B2", "free", "knowledge_tokens"),
                         m("B1", "free", "knowledge_tokens"), m("A2", "free", "knowledge_tokens"))
    a1z, cz, b2z = m("A1", "zero", "hit_rate"), m("C", "zero", "hit_rate"), m("B2", "zero", "hit_rate")
    recomputed = {
        "CA-1": a1f <= 0.30 * b2f,
        "CA-1b": a1f <= 0.30 * b1f,
        "CA-4": (
            a1f <= a2f
            and b1f <= b2f
            and m("A1", "free", "hit_rate") >= m("A2", "free", "hit_rate")
            and m("B1", "free", "hit_rate") >= m("B2", "free", "hit_rate")
        ),
        "CA-E1": (a1z > cz and a1z >= b2z),
    }
    for key, val in recomputed.items():
        if metrics["gates"][key]["pass"] != val:
            failures.append(f"gate {key}: published {metrics['gates'][key]['pass']} != recomputed {val}")

    if failures:
        print("FAIL — the published numbers do not all reproduce:")
        for f in failures:
            print("  -", f)
        return 1

    print("PASS — every published number reproduces from the machine verdict:")
    print(f"  per-arm means           : {len(agg)} arm×variant rows checked")
    print(f"  gate verdicts            : {len(recomputed)} recomputed and equal")
    print(f"  validity                : {n_valid}/{n_total} valid, {n_out} out-of-arm reads")
    print("  threshold drift         : none (v1.2 == v1.1 on prompts/keypoints/metrics/gates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
