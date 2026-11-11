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
| [`tr_cc3a687f02`](.tmem/traces/tr_cc3a687f02.json) | 🟢 `L0_FULL` | **Modify tmem/slicer.py**<br>_Key symbols: AgyAdapter, BackwardTraceSlicer, _extract_canonical_verification_cmd, _extract_toolchain_install_clause_ | `tmem/slicer.py` | `PYTHONPATH=/home/ubuntu/proj python3 -m unittest discover -s tests -v` | 100% | `-` |

---

## 2. Universal Causal Invariant Repairs (L2)

_No error-to-fix invariant pairs extracted yet._

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
| `python3 -m tmem.cli status` | Show project-level and global-level memory statistics |
| `python3 -m tmem.cli gc` | Run metabolic garbage collection and prune stale paths |
| `/tmem [project\|global\|both]` | Capture current session execution path into requested scope |
