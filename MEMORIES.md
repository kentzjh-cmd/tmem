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

- 🔗 .gitignore <-> tmem/dsh_plugin.mjs (co-changed 2x, 100% conf)
- 🔗 package.json <-> tmem/dsh_plugin.mjs (co-changed 2x, 50% conf)

---

## 🛠️ Memory Commands & Forensic Inspection

| Command | Description |
| :--- | :--- |
| `tmem inspect <trace_id> --level [index\|playbook\|raw\|diff]` | Progressive disclosure: inspect at index, playbook, raw steps, or diff level |
| `tmem replay <trace_id>` | Step-by-step forensic terminal playback of critical path vs pruned noise |
| `tmem diff <trace_id>` | View unified git diff patch recorded during the session |
| `tmem sync` | Re-index SQLite cache, validate L0 guards, and regenerate `MEMORIES.md` |
| `tmem status` | Show project-level memory statistics |
| `tmem gc` | Run metabolic garbage collection and prune stale paths |
| `/tmem [intent]` | Capture current session execution path into project memory (`.tmem/traces/`) |
