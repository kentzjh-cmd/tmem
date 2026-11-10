# `tmem` — Invisible Execution Trace Memory Compiler for Coding Agents

[中文文档](./README.md) | **English**

`tmem` is a zero-UI, dual-tier (**Project `.tmem/`** + **Global `~/.tmem/`**) execution trace memory plugin and CLI for coding agents (`agy`, `@deepseek-ai/dsh`, `codex`, `opencode`, `pi`).

Instead of storing raw conversational summaries, `tmem` performs **Git Ground-Truth Backward Slicing** on agent session transcripts (`transcript.jsonl` / `session.v4.jsonl`), pruning dead-end explorations and compiling verified 3-layer playbooks (**L0 Critical Path**, **L1 Co-Change Topology**, **L2 Error-to-Fix Invariants**) guarded by `<5ms` zero-token pre-flight probes.

## Key Capabilities

1. **Git Ground-Truth Backward Slicing (`tmem/slicer.py`)**
   - Anchors on actual surviving modified files (`git status` / `git diff`) and final passing verification commands (`exit_code == 0`).
   - Prunes dead mutations, abandoned file edits, and unrelated exploratory reads.
   - Automatically scrubs secrets (GitHub PATs, API keys, tokens) before persistence.
2. **Speculative Guarded Recall (`tmem/guards.py`)**
   - **L0 (`L0_FULL`)**: Injects the concrete file/command critical path when `file_exists` and `symbol_present` guards pass.
   - **L1 (`L1_TOPOLOGY`)**: Gracefully degrades to architectural co-change contracts and verification commands when code symbols drift.
   - **L2 (`L2_INVARIANT`)**: Matches recurring compiler/test error signatures to causal repair actions across repositories.
3. **Dual-Tier Storage (`tmem/store.py`)**
   - **Project Tier (`<repo>/.tmem/traces/`)**: Versioned alongside the Git repository.
   - **Global Tier (`~/.tmem/playbooks/`)**: Cross-project portable L2 error-to-fix invariants.
4. **Native Invisible Agent Integrations**
   - **Antigravity (`agy`)**: `.agents/plugins/tmem/` (`hooks.json` `PreInvocation` + `PostToolUse`, `rules/AGENTS.md`, `skills/tmem/SKILL.md`).
   - **DeepSeek Harness (`@deepseek-ai/dsh`)**: `.dsh/plugins/tmem-dsh-plugin.mjs` Cordis plugin (`agent/pre-step`, `tools/post-execute`, native `/tmem` slash command in `dsh-commands`), `.dsh/tmem.patch.yml`, `~/.dsh/cordis.patch.yml`, `.agents/skills/tmem/SKILL.md`, and `AGENTS.md`.
   - **Git Hook**: `.git/hooks/post-commit` automatic background trace compilation.

## Public Trajectory Datasets & Benchmark Evaluation

`tmem` natively supports ingesting, compiling, and validating public coding agent trajectories:
- **SWE-bench Experiments ([`SWE-bench/experiments`](https://github.com/SWE-bench/experiments)) & SWE-agent (`.traj`)**
- **ATIF (Agent Trajectory Interchange Format / Harbor `trajectory.json`)**
- **DeepSeek Harness (`session.v4.jsonl`)**

### Benchmark Results (`python3 -m tmem.cli evaluate`)

| Trajectory Instance | Format | Raw | L0 Path | Compression Ratio | Noise Filtered | L1 Files | L2 Repairs | Top-1 Recall | Slicing Latency |
|---|---|---|---|---|---|---|---|---|---|
| `dsh_e2e_session` | `dsh (session.v4.jsonl)` | 9 | 4 | **55.6%** | **100.0%** (3 -> 0) | 3 files | 1 pair | **#1** | 0.51 ms |
| `dsh_long_lifecycle_session` | `dsh (session.v4.jsonl)` | 27 | 9 | **66.7%** | **100.0%** (6 -> 0) | 7 files | 1 pair | **#1** | 0.45 ms |
| `dsh_multiturn_complex_session` | `dsh (session.v4.jsonl)` | 17 | 6 | **64.7%** | **100.0%** (6 -> 0) | 5 files | 1 pair | **#1** | 0.37 ms |
| `astropy__astropy-12907` | `ATIF (trajectory.json)` | 17 | 7 | **58.8%** | **100.0%** (9 -> 0) | 3 files | 1 pair | **#1** | 0.32 ms |
| `django__django-11099` | `SWE-bench (.traj)` | 8 | 3 | **62.5%** | **100.0%** (4 -> 0) | 1 file | 1 pair | **#1** | 0.18 ms |
| `flask__flask-5014` | `SWE-bench (.traj)` | 6 | 3 | **50.0%** | **100.0%** (3 -> 0) | 1 file | 1 pair | **#1** | 0.22 ms |
| `pytest-dev__pytest-7432` | `SWE-bench (.traj)` | 15 | 6 | **60.0%** | **100.0%** (6 -> 0) | 2 files | 1 pair | **#1** | 0.27 ms |
| `scikit-learn__scikit-learn-13496` | `SWE-bench (.traj)` | 12 | 6 | **50.0%** | **100.0%** (4 -> 0) | 2 files | 1 pair | **#1** | 0.29 ms |
| `sympy__sympy-20590` | `ATIF (trajectory.json)` | 6 | 4 | **33.3%** | **100.0%** (3 -> 0) | 2 files | 0 pair | **#1** | 0.18 ms |
| **Overall Average (9 Cases)** | **Multi-Format** | **13.0** | **5.33** | **55.73%** | **100.0%** | **2.89 files** | **8 pairs** | **100% (MRR: 1.0)** | **0.31 ms** |

### Long-Horizon Engineering Enhancements
1. **Dead Mutation Elimination (DME)**: Merges successive edits across 20-40 turn sessions, discarding intermediate scratch attempts.
2. **Scratch File Pruning**: Automatically prunes ephemeral test scripts (`scratch_*.py`) created and removed during debugging loops.
3. **36-Step Expanded Causal Horizon**: Links early test failures to root-cause repairs across deep investigative loops.
4. **Context Injection Deduplication**: Suppresses repeated prompt injections during long multi-step agent actions.
5. **Interactive Benchmarking**: Supports `/tmem bench` and `/tmem evaluate` in both DSH and Antigravity.

Run evaluation locally:
```bash
python3 -m tmem.cli evaluate
```
