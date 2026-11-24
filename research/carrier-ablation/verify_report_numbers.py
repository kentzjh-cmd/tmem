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
import os
import re
import sys
from pathlib import Path
from statistics import mean

REPO = Path(os.environ.get("TMEM_VERIFY_ROOT", Path(__file__).resolve().parents[2]))
RES = REPO / "research" / "carrier-ablation" / "results"
REPORT = REPO / "docs" / "dsh-memory-carrier-ablation-report.md"
PROTOCOL = REPO / "docs" / "dsh-memory-carrier-ablation.md"
MAIN_REPORT = REPO / "docs" / "dsh-independent-e2e-report.md"
R2_RES = REPO / "research" / "round2" / "results"
R3_RES = REPO / "research" / "round3" / "results"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # research/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # research/carrier-ablation/
from trajectory_lib import digest_events, edited_files, read_events, sha256_file, workspace_root  # noqa: E402
from score_carrier_ablation import mean_ci  # noqa: E402  (the scorer's own stats formula)

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

    failures += check_round3()
    failures += check_aggregate_recomputed(metrics)
    failures += check_trajectories()
    failures += check_numeric_claims()

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
    print("  round-3 cross-check     : 7/9 (clean) vs 6/9 (dirty tree) verified, stale wording absent")
    print("  aggregate re-derived    : every arm×variant mean/CI recomputed from the sessions")
    print("  trajectories            : archived transcripts re-derive the published records")
    print("  contradicting figures   : no labelled number contradicts the archives")
    return 0


# ---------------------------------------------------------------- round 3 part
def check_round3() -> list[str]:
    """Cross-check the MAIN report against the archived Round 3 verdicts.

    The Round 3 numbers live in the main report (docs/dsh-independent-e2e-report.md);
    this module lives under the carrier-ablation apparatus, so the check is done
    here to keep one single "does the prose match the machine" entry point.
    """
    fails: list[str] = []
    r3_dir = REPO / "research" / "round3" / "results"
    clean = json.loads((r3_dir / "metrics-r3-recheck-clean-tree.json").read_text(encoding="utf-8"))
    dirty = json.loads((r3_dir / "metrics-r3-first-pass-dirty-tree.json").read_text(encoding="utf-8"))
    main = MAIN_REPORT.read_text(encoding="utf-8")

    def gate(doc: dict, gid: str) -> dict:
        for x in doc["gates"]:
            if (x.get("gate") or x.get("id")) == gid:
                return x
        return {}

    n_clean = sum(1 for x in clean["gates"] if x.get("pass"))
    n_dirty = sum(1 for x in dirty["gates"] if x.get("pass"))
    if (n_clean, n_dirty) != (7, 6):
        fails.append(f"round3 gate counts: clean={n_clean} (want 7), dirty={n_dirty} (want 6)")
    if gate(dirty, "G9").get("pass") is not False or gate(clean, "G9").get("pass") is not True:
        fails.append("round3: the archived pair must show G9 FAIL (dirty tree) and PASS (clean tree)")

    g6 = gate(clean, "G6").get("value", {})
    g3 = gate(clean, "G3").get("value", {})
    g8 = gate(clean, "G8").get("value", {})
    for value in (f"{g6.get('lookup')}", f"{g6.get('search')}"):
        if value not in main:
            fails.append(f"round3 G6 value '{value}' not in the main report")
    if f"{g3.get('precision')}" not in main:
        fails.append(f"round3 G3 precision '{g3.get('precision')}' not in the main report")
    tel = g8.get("l2_injected_telemetry_events")
    if f"遥测 **{tel} 次**" not in main and f"遥测 {tel} 次" not in main:
        fails.append(f"round3 G8 telemetry '{tel}' not in the main report")

    # stale / retracted wording must NOT reappear
    forbidden = [
        ("遥测 11 次", "stale G8 telemetry figure (the verdict says %s)" % tel),
        ("30/30 场有效", "stale validity claim (the verdict says 29/30)"),
        ("零越界读取", "stale isolation claim (one session had 9 out-of-arm reads)"),
        ("归因清晰可指认", "retracted causal wording for the confounded cross-round G6 delta"),
    ]
    for text in (main, REPORT.read_text(encoding="utf-8"), PROTOCOL.read_text(encoding="utf-8")):
        for needle, why in forbidden:
            if needle in text:
                fails.append(f"forbidden phrase '{needle}' present -- {why}")
    return fails


# ------------------------------------------------------- aggregate re-derivation
def check_aggregate_recomputed(metrics: dict) -> list[str]:
    """The published aggregate block must be re-derivable from the per-session records.

    The verdict file stores BOTH the per-session records and a pre-aggregated
    block.  Checking the prose against the pre-aggregated block only proves the
    prose matches the summary; it does not prove the summary matches the
    sessions that were actually run.  This recomputes the block from the
    sessions, using the scorer's own `mean_ci` so the formula is identical.
    """
    fails: list[str] = []
    sessions = metrics["sessions"]
    agg = metrics["aggregate"]

    for key, a in sorted(agg.items()):
        arm, variant = key.split("|")
        rows = sorted(
            (s for s in sessions if s["arm"] == arm and s["variant"] == variant and s["valid"]),
            key=lambda s: s["rep"],
        )
        for field in ("knowledge_tokens", "hit_rate", "injected_tokens", "read_output_tokens", "read_calls"):
            want = [s[field] for s in rows]
            got = a.get(field) or []
            if [round(v, 4) for v in want] != [round(v, 4) for v in got]:
                fails.append(f"aggregate {key}.{field}: sessions give {want}, published {got}")
            stats = a.get(f"{field}_stats") or {}
            recomputed = mean_ci(list(want))
            for stat_key in ("mean", "n", "values", "sd"):
                if stats.get(stat_key) != recomputed.get(stat_key):
                    fails.append(
                        f"aggregate {key}.{field}_stats.{stat_key}: "
                        f"recomputed {recomputed.get(stat_key)} != published {stats.get(stat_key)}"
                    )
            if stats.get("ci95") != recomputed.get("ci95"):
                fails.append(
                    f"aggregate {key}.{field}_stats.ci95: "
                    f"recomputed {recomputed.get('ci95')} != published {stats.get('ci95')}"
                )
        invalid = sorted(
            (s["rep"] for s in sessions
             if not s["valid"] and s["arm"] == arm and s["variant"] == variant)
        )
        published_invalid = a.get("invalid") or []
        if isinstance(published_invalid, list) and published_invalid and isinstance(published_invalid[0], dict):
            got_invalid = sorted(int(x.get("rep")) for x in published_invalid)
        else:
            got_invalid = published_invalid
        if got_invalid != invalid:
            fails.append(f"aggregate {key}.invalid: recomputed {invalid} != published {published_invalid}")

    def m(arm: str, variant: str, field: str = "knowledge_tokens"):
        return agg[f"{arm}|{variant}"][f"{field}_stats"]["mean"]

    desc = metrics["descriptive"]
    recomputed_desc = {
        "injection_only_ratio": round(m("A1", "free", "injected_tokens") / m("B2", "free"), 4),
        "free_variant_total_ratio": round(m("A1", "free") / m("B2", "free"), 4),
        "A1_free_mean_injected_tokens": m("A1", "free", "injected_tokens"),
        "A1_free_mean_knowledge_tokens": m("A1", "free"),
        "B2_free_mean_knowledge_tokens": m("B2", "free"),
    }
    for key, value in recomputed_desc.items():
        if desc.get(key) != value:
            fails.append(f"descriptive {key}: recomputed {value} != published {desc.get(key)}")

    # per-arm read-output categories (free variant, valid sessions)
    for arm, cats in (desc.get("read_output_tokens_by_category") or {}).items():
        rows = [s for s in sessions if s["arm"] == arm and s["variant"] == "free" and s["valid"]]
        for cat, want in cats.items():
            got = round(mean([s["read_output_tokens_by_category"][cat] for s in rows]), 1)
            if abs(got - want) > 0.05:
                fails.append(f"descriptive category {arm}.{cat}: recomputed {got} != published {want}")

    # the mean values quoted inside the gate verdicts
    for gkey, gv in metrics["gates"].items():
        value = gv.get("value")
        if not isinstance(value, dict):
            continue
        for k, v in value.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            if not (k.endswith("_mean_knowledge_tokens") or k.endswith("_mean_injected_tokens")):
                continue
            arm = k.split("_", 1)[0]
            variant = "free" if "_free_" in k else "zero"
            field = "knowledge_tokens" if "knowledge" in k else "injected_tokens"
            got = m(arm, variant, field)
            if abs(round(got, 4) - round(v, 4)) > 1e-6:
                fails.append(f"gate {gkey}.{k}: recomputed {got} != published {v}")
    return fails


# ------------------------------------------------------------------ trajectories
def _check_manifest_files(raw_dir: Path, sess: dict, label: str) -> list[str]:
    """Every archived file must exist with exactly the recorded size and digest."""
    fails: list[str] = []
    for name, meta in sorted(sess["files"].items()):
        path = raw_dir / name
        if not path.is_file():
            fails.append(f"{label} {sess['id']}: archived file '{name}' is missing")
            continue
        if path.stat().st_size != meta["bytes"]:
            fails.append(
                f"{label} {sess['id']}/{name}: size {path.stat().st_size} != manifest {meta['bytes']}"
            )
        digest = sha256_file(path)
        if digest != meta["sha256"]:
            fails.append(
                f"{label} {sess['id']}/{name}: sha256 {digest[:12]} != manifest {meta['sha256'][:12]}"
            )
    return fails


def _check_digest(raw_dir: Path, sess: dict, label: str) -> list[str]:
    """The manifest's trajectory digest must be re-derivable from the archived transcript."""
    fails: list[str] = []
    want = sess.get("trajectory") or {}
    if not want.get("events"):
        return fails
    transcript = raw_dir / "session.v4.jsonl.zstd"
    if not transcript.is_file():
        return [f"{label} {sess['id']}: transcript missing, cannot re-derive the trajectory digest"]
    try:
        events = read_events(transcript)
    except Exception as exc:  # a tampered or truncated archive must fail, not crash
        return [f"{label} {sess['id']}: transcript cannot be decoded ({exc.__class__.__name__})"]
    digest = digest_events(events)
    for key in ("events", "tool_calls", "tmem_tool_calls", "user_messages", "assistant_messages"):
        if digest[key] != want.get(key):
            fails.append(
                f"{label} {sess['id']}: trajectory digest '{key}' differs from the archived transcript"
            )
    if sha256_file(transcript) != want.get("transcript_sha256"):
        fails.append(f"{label} {sess['id']}: transcript sha256 differs from the manifest")
    root = workspace_root(events)
    if want.get("workspace_root") != root:
        fails.append(f"{label} {sess['id']}: workspace root differs from the archived transcript")
    if want.get("edited_files") != edited_files(events, root):
        fails.append(f"{label} {sess['id']}: edited files differ from the archived transcript")
    return fails


def check_trajectories() -> list[str]:
    """Bind the published per-session numbers to the archived real trajectories.

    The chain is: archived transcript -> manifest digest -> published per-session
    record -> aggregate -> report.  Every link is re-derived here.
    """
    fails: list[str] = []

    # ---- carrier ablation: 30 sessions, bound to the published records ----
    man = json.loads((RES / "raw_manifest.json").read_text(encoding="utf-8"))
    verdict = json.loads((RES / "metrics-carrier-ablation-v1.2.json").read_text(encoding="utf-8"))
    records = {f"{s['arm']}_r{s['rep']}_{s['variant']}": s for s in verdict["sessions"]}
    archived: set[str] = set()
    for sess in man["sessions"]:
        raw = RES / "raw" / sess["id"]
        fails += _check_manifest_files(raw, sess, "ablation")
        fails += _check_digest(raw, sess, "ablation")
        key = f"{sess['arm']}_r{sess['rep']}_{sess['variant']}"
        archived.add(key)
        rec = records.get(key)
        if rec is None:
            fails.append(f"ablation {sess['id']}: no published record for this session")
            continue
        derived = (sess.get("trajectory") or {}).get("tmem_tool_calls")
        if derived != rec.get("tmem_tool_calls"):
            fails.append(
                f"ablation {sess['id']}: tool calls from the transcript {derived} "
                f"!= published record {rec.get('tmem_tool_calls')}"
            )
        for field in ("exit_code", "elapsed_sec"):
            if sess.get(field) != rec.get(field):
                fails.append(
                    f"ablation {sess['id']}: {field} {sess.get(field)} != published {rec.get(field)}"
                )
        scene_file = raw / "scene.json"
        if scene_file.is_file():
            scene = json.loads(scene_file.read_text(encoding="utf-8"))
            for counter, name in (("stdout_bytes", "stdout.log"), ("stderr_bytes", "stderr.log")):
                target = raw / name
                if target.is_file() and scene.get(counter) != target.stat().st_size:
                    fails.append(
                        f"ablation {sess['id']}: scene {counter}={scene.get(counter)} "
                        f"!= archived {name} size {target.stat().st_size}"
                    )
    missing = sorted(set(records) - archived)
    if missing:
        fails.append(f"ablation: {len(missing)} published sessions have no archived trajectory: {missing}")

    # ---- round 3: the E2E scenes, bound to the gate details ----
    man3 = json.loads((R3_RES / "raw_manifest.json").read_text(encoding="utf-8"))
    clean = json.loads((R3_RES / "metrics-r3-recheck-clean-tree.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in man3["sessions"]}
    for sess in man3["sessions"]:
        raw = R3_RES / "raw" / sess["id"]
        fails += _check_manifest_files(raw, sess, "round3")
        fails += _check_digest(raw, sess, "round3")

    # the scene keys of the E2E runs (S1_baseline .. S6_l2_transfer)
    detail_keys: set[str] = set()
    for g in clean["gates"]:
        det = g.get("detail")
        if isinstance(det, dict):
            detail_keys.update(det.keys())

    # A scene appears in more than one gate's detail (e.g. S1_baseline is
    # quoted by G1 and by G3), so every recorded copy must agree with the
    # transcript -- a merge would let one stale copy hide another.
    scene_edits: dict[str, list[list[str]]] = {}
    for g in clean["gates"]:
        det = g.get("detail")
        if not isinstance(det, dict):
            continue
        for dkey, d in det.items():
            if not isinstance(d, dict):
                continue
            se = d.get("session_edited_files")
            if isinstance(se, list):
                scene_edits.setdefault(dkey, []).append(sorted(se))

    for dkey, recorded in sorted(scene_edits.items()):
        if dkey not in by_id:
            continue
        raw = R3_RES / "raw" / dkey
        transcript = raw / "session.v4.jsonl.zstd"
        if not transcript.is_file():
            continue
        try:
            events = read_events(transcript)
        except Exception:
            continue
        derived = edited_files(events, workspace_root(events))
        for published in recorded:
            if derived != published:
                fails.append(
                    f"round3 {dkey}: files edited per the transcript {derived} "
                    f"!= verdict {published}"
                )

    g9 = next((g for g in clean["gates"] if (g.get("gate") or g.get("id")) == "G9"), {})
    published_elapsed = ((g9.get("value") or {}).get("session_elapsed_sec"))
    if published_elapsed is not None:
        # the main E2E scenes are S<n>_<name>; S6b is the controlled variant and
        # is not part of G9's session list
        order = sorted(k for k in detail_keys if re.match(r"^S\d+_", k))
        elapsed = [(by_id.get(k) or {}).get("elapsed_sec") for k in order]
        if elapsed != published_elapsed:
            fails.append(
                f"round3 G9: archived scene elapsed times {elapsed} != verdict {published_elapsed}"
            )

    # ---- round 2: integrity only, no attribution is asserted ----
    r2_verdict = R2_RES / "metrics-r2.json"
    if not r2_verdict.is_file():
        fails.append(f"round2: the cited verdict {r2_verdict.name} is not archived")
    man2 = json.loads((R2_RES / "raw_manifest.json").read_text(encoding="utf-8"))
    for sess in man2["sessions"]:
        raw = R2_RES / "raw" / sess["id"]
        fails += _check_manifest_files(raw, sess, "round2")
        fails += _check_digest(raw, sess, "round2")
    return fails


# ------------------------------------------------------- contradicting figures
def _numbers_in(value) -> set[float]:
    """Every numeric leaf of a gate value."""
    out: set[float] = set()
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, dict):
        for v in value.values():
            out |= _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= _numbers_in(v)
    return out


def _gate(doc: dict, gid: str) -> dict:
    for g in doc.get("gates", []):
        if (g.get("gate") or g.get("id")) == gid:
            return g
    return {}


def _md_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            current.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _collect_label_values(paths: list[Path], label: str) -> set[float]:
    """Every numeric leaf whose key path mentions `label`, across the archives.

    This is what makes the contradiction check machine-derived: a figure may be
    stated in the prose only if some archived verdict records a value for that
    metric label.  The universe therefore closes over the whole research
    history without anyone hand-listing the permitted numbers.
    """
    out: set[float] = set()

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)) and label in path.lower():
            out.add(float(node))

    for p in paths:
        if not p.is_file():
            continue
        try:
            walk(json.loads(p.read_text(encoding="utf-8")), "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return out


def check_numeric_claims() -> list[str]:
    """Catch figures that contradict the archives.

    Presence checks cannot do this: appending "G6 lookup 0.95" leaves every
    published number still present.  Two mechanisms close that hole:

    * every gate row of the main report is parsed and the numbers in its live
      columns must be numbers the corresponding archived verdict states;
    * every *labelled* figure in the prose ("lookup 0.75", "precision 0.908")
      must be a value some archived verdict records for that metric label.
    """
    fails: list[str] = []
    main = MAIN_REPORT.read_text(encoding="utf-8")
    report = REPORT.read_text(encoding="utf-8")
    protocol = PROTOCOL.read_text(encoding="utf-8")

    clean = json.loads((R3_RES / "metrics-r3-recheck-clean-tree.json").read_text(encoding="utf-8"))
    dirty = json.loads((R3_RES / "metrics-r3-first-pass-dirty-tree.json").read_text(encoding="utf-8"))
    r2 = json.loads((R2_RES / "metrics-r2.json").read_text(encoding="utf-8"))
    abl = json.loads((RES / "metrics-carrier-ablation-v1.2.json").read_text(encoding="utf-8"))
    abl_p1 = json.loads((RES / "metrics-carrier-ablation.json").read_text(encoding="utf-8"))

    # ---- every gate row of the main report must agree with its archive ----
    # the columns are located through the table header when the table has one,
    # otherwise the standard layout (gate | Round 2 | Round 3 | notes) is used
    for table in _md_tables(main):
        header = table[0]
        has_header = any("Round 2" in c for c in header) and any("Round 3" in c for c in header)
        col_r2 = next((i for i, c in enumerate(header) if "Round 2" in c), None) if has_header else 1
        col_r3 = next((i for i, c in enumerate(header) if "Round 3" in c), None) if has_header else 2
        for row in table:
            if not row or not re.match(r"\*\*G\d\*\*", row[0]):
                continue
            gid = re.match(r"\*\*(G\d)\*\*", row[0]).group(1)
            for doc, col, label in ((r2, col_r2, "Round 2"), (clean, col_r3, "Round 3")):
                if col is None or col >= len(row):
                    continue
                want = _numbers_in(_gate(doc, gid).get("value"))
                got = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", row[col])}
                if not got.issubset(want):
                    fails.append(
                        f"gate table {gid} ({label} column): states {sorted(got)}, "
                        f"the archive says {sorted(want)}"
                    )

    # ---- labelled figures in the prose, bound to the archived value universe ----
    verdict_paths = sorted(
        p
        for d in ("research/round1/results", "research/round2/results", "research/round3/results")
        for p in (REPO / d).glob("*.json")
    ) + [RES / "metrics-carrier-ablation-v1.2.json", RES / "metrics-carrier-ablation.json"]
    label_anchors = {
        "lookup": r"lookup\s*\**\s*([0-9]+(?:\.[0-9]+)?)",
        "search": r"search\s*\**\s*([0-9]+(?:\.[0-9]+)?)",
        "precision": r"precision\s*\**\s*([0-9]+(?:\.[0-9]+)?)",
        "recall": r"recall\s*\**\s*([0-9]+(?:\.[0-9]+)?)",
    }
    for label, pattern in label_anchors.items():
        allowed = _collect_label_values(verdict_paths, label)
        if not allowed:
            fails.append(f"numeric claims: no archived value found for the label '{label}'")
            continue
        for text, name in ((main, "main"), (report, "ablation report"), (protocol, "protocol")):
            for hit in re.findall(pattern, text):
                if float(hit) not in allowed:
                    fails.append(
                        f"{name}: '{label} {hit}' is not a value any archived verdict records "
                        f"(permitted: {sorted(allowed)})"
                    )

    # ---- G8 telemetry / L1 counts and the validity count ----
    g8_r2 = _gate(r2, "G8").get("value") or {}
    g8_r3 = _gate(clean, "G8").get("value") or {}
    allowed = {
        "G8 l2_injected telemetry events": {
            g8_r2.get("l2_injected_telemetry_events"),
            g8_r3.get("l2_injected_telemetry_events"),
        },
        "G8 l1 co-change hints": {g8_r2.get("l1_cochange_hints"), g8_r3.get("l1_cochange_hints")},
        "G8 l1 outcomes": {g8_r2.get("l1_outcomes"), g8_r3.get("l1_outcomes")},
    }
    anchors = [
        (r"遥测\s*\**(\d+)\s*次", allowed["G8 l2_injected telemetry events"]),
        (r"l1_cochange_hint(?:ed|s)?`?\s*\**(\d+)\s*次", allowed["G8 l1 co-change hints"]),
        (r"l1_outcome`?\s*\**(\d+)\s*次", allowed["G8 l1 outcomes"]),
        (r"L1 共变提示(?:发出)?\s*\**(\d+)\s*[条次]", allowed["G8 l1 co-change hints"]),
    ]
    for text, name in ((main, "main"), (report, "ablation report"), (protocol, "protocol")):
        for pattern, ok_values in anchors:
            ok_values = {v for v in ok_values if isinstance(v, int)}
            if not ok_values:
                continue
            for hit in re.findall(pattern, text):
                if int(hit) not in ok_values:
                    fails.append(
                        f"{name}: '{pattern}' states {hit}, the archive says {sorted(ok_values)}"
                    )

    sessions = abl["sessions"]
    n_valid = sum(1 for s in sessions if s["valid"])
    n_total = len(sessions)
    for text, name in ((main, "main"), (report, "ablation report"), (protocol, "protocol")):
        for hit in re.findall(r"(\d+)\s*/\s*(\d+)\s*场有效", text):
            if (int(hit[0]), int(hit[1])) != (n_valid, n_total):
                fails.append(
                    f"{name}: states {hit[0]}/{hit[1]} sessions valid, the verdict says {n_valid}/{n_total}"
                )
        for hit in re.findall(r"(?<![\d/])(\d+)\s*场有效", text):
            if int(hit) != n_valid:
                fails.append(
                    f"{name}: states {hit} sessions valid, the verdict says {n_valid}"
                )

    # ---- the ablation report's descriptive table ----
    desc = abl["descriptive"]
    desc_p1 = abl_p1["descriptive"]

    def _cell_number(cell: str) -> float | None:
        hits = re.findall(r"\d+(?:\.\d+)?", cell)
        return float(hits[0]) if hits else None

    for table in _md_tables(report):
        for row in table:
            if len(row) < 3 or not row[0]:
                continue
            # the descriptive table's rows are identified by their exact first
            # cell (other tables may mention the same metric name)
            for metric, live, phase1 in (
                ("`injection_only_ratio`", desc["injection_only_ratio"], desc_p1["injection_only_ratio"]),
                ("`free_variant_total_ratio`", desc["free_variant_total_ratio"], desc_p1["free_variant_total_ratio"]),
            ):
                if row[0].strip() != metric:
                    continue
                got, got_p1 = _cell_number(row[1]), _cell_number(row[2])
                if got != live:
                    fails.append(
                        f"ablation report: {metric} cell {row[1]!r} != archived {live}"
                    )
                if got_p1 != phase1:
                    fails.append(
                        f"ablation report: phase-1 {metric} cell {row[2]!r} != archived {phase1}"
                    )
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
