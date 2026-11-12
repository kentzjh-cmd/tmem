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
| [`tr_580560d0b4`](.tmem/traces/tr_580560d0b4.json) | ⚪ `TRIVIAL` | **Modify tmem/slicer.py**<br>_Key modules: tmem/slicer.py_ | `tmem/slicer.py` | `python3 -m unittest discover -s tests -v` | 50% | [`raw`](.tmem/raw/tr_580560d0b4.raw.json) |
| [`tr_5870455d8e`](.tmem/traces/tr_5870455d8e.json) | 🔵 `L2_INVARIANT` | **feat(core): implement optimization roadmap (P0 triage, P1 empirical benchmarks, P3 contraction)**<br>_Key symbols: BenchmarkEvaluator, BenchmarkSummary, ProgressiveDisclosureResult, ProjectMemoryStore, SweBenchAdapter, TestPragmaticOptimizations_<br>🎯 <i>Error 'AssertionError: 'tmem Upstream Memory Compilation' not found in 'tmem command completed.'' resolved by 'edit t...</i> | `README.md`<br>`README_EN.md`<br>`benchmarks/eval_downstream_transfer.py`<br>_+11 more_ | `python3 -m unittest discover -s benchmarks/ab_tests/task2_rst/group_b && python3 -m unittest discover -s benchmarks/a...` | 70% | [`raw`](.tmem/raw/tr_5870455d8e.raw.json) |
| [`tr_fa0063862d`](.tmem/traces/tr_fa0063862d.json) | ⚪ `TRIVIAL` | **Modify tmem/slicer.py**<br>_Key symbols: assess_playbook_triviality_ | `tmem/slicer.py` | `python3 -m unittest discover -s tests -v` | 50% | [`raw`](.tmem/raw/tr_fa0063862d.raw.json) |

---

## 2. Universal Causal Invariant Repairs (L2)

| Error Signature | Failed Command | Minimal Repair Action | Verification Command |
| :--- | :--- | :--- | :--- |
| `AssertionError: 'tmem Upstream Memory Compilation' not found in 'tmem command completed.'` | `python3 -m unittest tests/test_dsh_e2e.py -k test_live_dsh_cordis_runtime_and_cli_e2e -v` | `edit tests/test_dsh_e2e.py -> edit tmem/evaluate.py` | `python3 -m unittest tests/test_evaluation_benchmark.py -v` |
| `AssertionError: '轨迹切片与特征描述评测' not found in 'tmem command completed.'` | `python3 -m unittest discover -s tests` | `edit tmem/evaluate.py` | `python3 -m unittest tests/test_evaluation_benchmark.py -v` |
| `ValueError("Expected n_neighbors > 0")` | `diff -u benchmarks/ab_tests/task3_neighbors/group_a/neighbors.py benchmarks/ab_tests/task3_neighbors/group_b/neighbor...` | `edit tmem/adapters/swe_bench.py -> edit benchmarks/eval_retrieval.py -> edit tmem/store.py` | `python3 -m unittest discover -s benchmarks/ab_tests/task1_validator/group_a` |

---

## 3. Co-Change Architectural Topologies

- 🔗 Primary target module: tmem/slicer.py
- 🔗 Co-change contract: modifying README.md requires synchronized updates to README_EN.md, benchmarks/eval_downstream_transfer.py, benchmarks/eval_retrieval.py, benchmarks/eval_slicing_fidelity.py, package.json, pyproject.toml, tests/test_dsh_e2e.py, tests/test_optimizations.py, tmem/adapters/base.py, tmem/adapters/swe_bench.py, tmem/cli.py, tmem/evaluate.py, tmem/store.py

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
