# 🧠 Project Execution Memories (`MEMORIES.md`)

> **Maintained autonomously by [tmem](https://github.com/kentzjh-cmd/tmem)**.
> Zero-token deterministic requirement & behavior summaries compiled from sliced historical DSH sessions (with inline `session_id` lineage), co-change topologies, and causal error repairs.
> Both DSH and human engineers can navigate this index to inspect verified execution paths without re-reading raw session logs or paying LLM summarization tokens.

## ⚡ Progressive Memory Disclosure Tiers
- **Tier 0 (Catalog Index)**: This `MEMORIES.md` file provides zero-token exploration and instant navigation.
- **Tier 1 (Playbooks)**: Detailed pre-flight guarded playbooks with AST/filesystem assertions in [`.tmem/traces/`](.tmem/traces/).
- **Tier 2 (Raw Execution Traces)**: Full-fidelity forensic logs, tool inputs/outputs, and diff patches in [`.tmem/raw/`](.tmem/raw/).

---

## 1. Active Verified Execution Playbooks (L0/L1)

_No project execution traces recorded yet. Traces will be automatically compiled on `/tmem`, milestone completions, or `git commit`._

---

## 2. Universal Causal Invariant Repairs (L2)

_No error-to-fix invariant pairs extracted yet._

---

## 3. Co-Change Architectural Topologies

- 🔗 tmem/adapters/base.py <-> tmem/slicer.py (co-changed 4x, 100% conf)
- 🔗 tmem/adapters/base.py <-> tmem/adapters/dsh.py (co-changed 3x, 100% conf)
- 🔗 tmem/adapters/dsh.py <-> tmem/slicer.py (co-changed 3x, 100% conf)
- 🔗 tmem/adapters/dsh.py <-> tmem/store.py (co-changed 3x, 100% conf)
- 🔗 tmem/adapters/base.py <-> tmem/store.py (co-changed 3x, 75% conf)
- 🔗 tmem/slicer.py <-> tmem/store.py (co-changed 3x, 75% conf)
- 🔗 tests/test_dsh_e2e.py <-> tmem/cli.py (co-changed 2x, 100% conf)
- 🔗 tests/test_dsh_e2e.py <-> tmem/dsh_plugin.mjs (co-changed 2x, 100% conf)
- 🔗 tmem/adapters/base.py <-> tmem/guards.py (co-changed 2x, 100% conf)
- 🔗 tmem/adapters/base.py <-> tmem/models.py (co-changed 2x, 100% conf)
- 🔗 tmem/adapters/dsh.py <-> tmem/guards.py (co-changed 2x, 100% conf)
- 🔗 tmem/adapters/dsh.py <-> tmem/models.py (co-changed 2x, 100% conf)
- 🔗 tmem/cli.py <-> tmem/dsh_plugin.mjs (co-changed 2x, 100% conf)
- 🔗 tmem/cli.py <-> tmem/store.py (co-changed 2x, 100% conf)
- 🔗 tmem/dsh_plugin.mjs <-> tmem/store.py (co-changed 2x, 100% conf)
- 🔗 tmem/guards.py <-> tmem/models.py (co-changed 2x, 100% conf)
- 🔗 tmem/guards.py <-> tmem/slicer.py (co-changed 2x, 100% conf)
- 🔗 tmem/guards.py <-> tmem/store.py (co-changed 2x, 100% conf)
- 🔗 tmem/mem_doc.py <-> tmem/store.py (co-changed 2x, 100% conf)
- 🔗 tmem/models.py <-> tmem/slicer.py (co-changed 2x, 100% conf)
- 🔗 tmem/models.py <-> tmem/store.py (co-changed 2x, 100% conf)
- 🔗 tests/test_dsh_e2e.py <-> tmem/store.py (co-changed 2x, 67% conf)

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
