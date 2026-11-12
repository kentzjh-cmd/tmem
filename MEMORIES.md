# 🧠 Project Execution Memories (`MEMORIES.md`)

> **Maintained autonomously by [tmem](https://github.com/kentzjh-cmd/tmem)**.
> This file is a git-versioned living index of verified execution playbooks, co-change topologies, and universal causal repairs.
> AI agents (Antigravity, DeepSeek Harness, Cursor, Claude Code, Copilot, etc.) and human engineers can navigate this index to follow verified paths and bypass known errors.

## ⚡ Progressive Memory Disclosure Tiers
- **Tier 0 (Catalog Index)**: This `MEMORIES.md` file provides zero-token exploration and instant navigation.
- **Tier 1 (Playbooks)**: Detailed pre-flight guarded playbooks with AST/filesystem assertions in [`.tmem/traces/`](.tmem/traces/).
- **Tier 2 (Raw Execution Traces)**: Full-fidelity forensic logs, tool inputs/outputs, and diff patches in [`.tmem/raw/`](.tmem/raw/).

---

## 1. Active Verified Execution Playbooks (L0/L1)

| Trace ID | Tier Status | Problem Intent / Summary | Key Files & Symbols | Verification Command | Confidence | Raw Trace |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| [`tr_580560d0b4`](.tmem/traces/tr_580560d0b4.json) | 🟢 `L0_FULL` | **Modify tmem/slicer.py**<br>_Key symbols: BackwardTraceSlicer_<br>🎯 <i>Error 'Command failed (exit 2): python3 -m tmem.cli inspect tr_464b40ddcd --level l0' resolved by 'edit tmem/slicer.py'</i> | `tmem/slicer.py` | `python3 -m unittest discover -s tests -v` | 100% | [`raw`](.tmem/raw/tr_580560d0b4.raw.json) |
| [`tr_cc3a687f02`](.tmem/traces/tr_cc3a687f02.json) | ⚪ `TRIVIAL` | **Modify tmem/slicer.py**<br>_Key symbols: AgyAdapter, BackwardTraceSlicer, _extract_canonical_verification_cmd, _extract_toolchain_install_clause_ | `tmem/slicer.py` | `python3 -m unittest discover -s tests -v` | 50% | `-` |
| [`tr_fa0063862d`](.tmem/traces/tr_fa0063862d.json) | 🟢 `L0_FULL` | **fix(slicer): refine trivial playbook assessment to evaluate code files and verification status**<br>_Key symbols: assess_playbook_triviality_<br>🎯 <i>Error 'FAILED (failures=1)' resolved by 'edit tmem/slicer.py'</i> | `tmem/slicer.py` | `python3 -m unittest discover -s tests -v` | 100% | [`raw`](.tmem/raw/tr_fa0063862d.raw.json) |

---

## 2. Universal Causal Invariant Repairs (L2)

| Error Signature | Failed Command | Minimal Repair Action | Verification Command |
| :--- | :--- | :--- | :--- |
| `Command failed (exit 2): python3 -m tmem.cli inspect tr_464b40ddcd --level l0` | `python3 -m tmem.cli inspect tr_464b40ddcd --level l0` | `edit tmem/slicer.py` | `python3 -m unittest discover -s tests -v` |
| `FAILED (failures=1)` | `python3 -m unittest discover -s tests -v` | `edit tmem/slicer.py` | `python3 -m unittest discover -s tests -v` |

---

## 3. Co-Change Architectural Topologies

- 🔗 Primary target module: tmem/slicer.py

---

## 🛠️ Memory Commands & Forensic Inspection

| Command | Description |
| :--- | :--- |
| `python3 -m tmem.cli inspect <trace_id> --level [index\|playbook\|raw\|diff]` | Progressive disclosure: inspect at index, playbook, raw steps, or diff level |
| `python3 -m tmem.cli replay <trace_id>` | Step-by-step forensic terminal playback of critical path vs pruned noise |
| `python3 -m tmem.cli diff <trace_id>` | View unified git diff patch recorded during the session |
| `python3 -m tmem.cli sync` | Re-index SQLite cache, validate L0 guards, and regenerate `MEMORIES.md` |
| `python3 -m tmem.cli status` | Show project-level memory statistics |
| `python3 -m tmem.cli gc` | Run metabolic garbage collection and prune stale paths |
| `/tmem [intent]` | Capture current session execution path into project memory (`.tmem/traces/`) |
