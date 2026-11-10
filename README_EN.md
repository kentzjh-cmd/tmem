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

| Trajectory Instance | Format | Raw | L0 Path | Compression Ratio | Noise Filtered | L2 Repairs | Top-1 Recall | Slicing Latency |
|---|---|---|---|---|---|---|---|---|
| `django__django-11099` | `SWE-bench (.traj)` | 8 | 3 | **62.5%** | **100.0%** (4 -> 0) | 1 pair | **#1** | 0.19 ms |
| `sympy__sympy-20590` | `ATIF (trajectory.json)` | 6 | 4 | **33.3%** | **100.0%** (3 -> 0) | 0 pair | **#1** | 0.18 ms |
| `flask__flask-5014` | `SWE-bench (.traj)` | 6 | 3 | **50.0%** | **100.0%** (3 -> 0) | 1 pair | **#1** | 0.19 ms |
| `dsh_e2e_session` | `dsh (session.v4.jsonl)` | 9 | 4 | **55.6%** | **100.0%** (3 -> 0) | 1 pair | **#1** | 0.49 ms |
| **Overall Average** | **Multi-Format** | **7.25** | **3.5** | **50.35%** | **100.0%** | **3 pairs** | **100% (MRR: 1.0)** | **0.26 ms** |

Run evaluation locally:
```bash
python3 -m tmem.cli evaluate
```
