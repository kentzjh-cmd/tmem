# `tmem` — Invisible Execution Trace Memory Compiler for Coding Agents

[中文文档](./README.md) | **English**

`tmem` is a zero-UI, dual-tier (**Project `.tmem/`** + **Global `~/.tmem/`**) execution trace memory compiler, forensic replay system, and living memory index for coding agents (`agy`, `@deepseek-ai/dsh`, `codex`, `opencode`, `pi`).

Instead of storing unstructured conversational summaries, `tmem` treats agent execution logs (`transcript.jsonl` / `session.v4.jsonl`) as unoptimized execution paths. By anchoring on **Git ground-truth surviving states** and **final passing verification commands (`exit_code == 0`)**, it executes **deterministic backward trace slicing**, automatically pruning exploratory dead-ends, scratch files, and noise commands, compiling them into **3-layer structured playbooks (L0/L1/L2)** guarded by `<5ms` zero-token pre-flight probes.

---

## Key Capabilities

### 1. Git Ground-Truth Backward Slicing ([`tmem/slicer.py`](file:///home/ubuntu/proj/tmem/slicer.py))
- **Ground-Truth Anchoring**: Correlates `git status` and `git diff` to identify files that actually survived on disk at session completion, eliminating abandoned edits.
- **Causal Slicing**: Backtracks from successful test/build commands to retain the minimal sequence of `READ -> MUTATE -> ENV_EXEC(VERIFY)` actions.
- **Causal Invariant Extraction (L2)**: Captures error-to-fix causal pairs `[Error Signature] -> [Minimal Repair Action] -> [Verification Command]`.
- **Automatic Secret Scrubbing ([`tmem/adapters/base.py`](file:///home/ubuntu/proj/tmem/adapters/base.py))**: Masks GitHub tokens (`github_pat_*`, `ghp_*`), OpenAI/Anthropic/Google API keys, and sensitive bearer tokens before storage.

### 2. 3-Tier Abstraction & Speculative Guarded Recall ([`tmem/models.py`](file:///home/ubuntu/proj/tmem/models.py) & [`tmem/guards.py`](file:///home/ubuntu/proj/tmem/guards.py))
Each compiled `TracePlaybook` features three layers of abstraction protected by pre-flight guard probes (`guards`):

| Tier | Name | Content Structure | Retrieval & Degradation Behavior |
| :--- | :--- | :--- | :--- |
| **L0** | **Critical Execution Path (`L0_FULL`)** | `READ(file) -> MUTATE(file#symbol) -> ENV_EXEC(cmd)` | Injected only when all `file_exists` and `symbol_present` filesystem/AST assertions pass (<5ms, zero LLM tokens). Guides the agent directly to the target without blind scans. |
| **L1** | **Architectural Co-Change Contract (`L1_TOPOLOGY`)** | File co-change topology + mandatory verification commands | Automatically degrades to L1 when code drift invalidates L0 symbols. Suppresses stale path details while enforcing architecture-level change integrity. |
| **L2** | **Cross-Repo Invariant Repairs (`L2_INVARIANT`)** | `Error Signature -> Minimal Repair Action -> Verification` | Triggers instantly on matching compiler/test error signatures. 100% portable across projects and environments. |

### 3. Dual-Tier Storage Architecture ([`tmem/store.py`](file:///home/ubuntu/proj/tmem/store.py))
- **Project Tier (`<repo>/.tmem/traces/*.json`)**: Committed into Git alongside code (single trace per hash ID, e.g. `tr_cc3a687f02.json`, ensuring zero Git merge conflicts). Teammates immediately inherit historical navigation and verification paths upon `git pull`.
- **Global Tier (`~/.tmem/playbooks/*.json`)**: Strips project-private file paths to persist portable toolchain, dependency, and build fix recipes (L2 Invariants) across repositories.

### 4. Full-Fidelity Raw Storage & Multi-Tier Progressive Disclosure ([`tmem/raw_storage.py`](file:///home/ubuntu/proj/tmem/raw_storage.py))
In addition to sliced playbooks, `tmem` preserves the full-fidelity session trajectory in `.tmem/raw/<trace_id>.raw.json` (all tool inputs, outputs, exit codes, and git diff patches):
- **Tier 0 (Catalog Index, ~35 tokens, -98.6% context reduction)**: Maintained in [`MEMORIES.md`](file:///home/ubuntu/proj/MEMORIES.md) at the project root for zero-token discovery.
- **Tier 1 (Guarded Playbook, ~370 tokens, -86.1% context reduction)**: High-density L0/L1/L2 playbooks injected during speculative recall.
- **Tier 2 (Full Raw Replay & Diffs)**: Complete forensic terminal replay (`python3 -m tmem.cli replay <id>`) and unified patch review (`python3 -m tmem.cli diff <id>`).

### 5. Living Memory Index (`MEMORIES.md` & `MEM.md`) ([`tmem/mem_doc.py`](file:///home/ubuntu/proj/tmem/mem_doc.py))
Following the industry standard established by `AGENTS.md` and `ARCHITECTURE.md`:
- **Autonomous Living Document**: Automatically generated and synchronized at workspace root (`MEMORIES.md`, symlinked to `MEM.md`).
- **Universal Agent Portability**: Enables zero-plugin, zero-configuration discovery for **Cursor (`.cursorrules`)**, **Claude Code**, **GitHub Copilot**, **Antigravity**, and **DeepSeek Harness**.
- **Instantaneous Sync**: Updated in ~1.6ms whenever a trace is saved, deleted, garbage collected, or via `/tmem sync`.

---

## Multi-Agent Invisible Integration

Run `python3 -m tmem.cli init --agents agy,dsh` to install invisible integrations:

### 1. Antigravity (`agy`)
- Plugin directory: [`.agents/plugins/tmem/`](file:///home/ubuntu/proj/.agents/plugins/tmem)
- [`hooks.json`](file:///home/ubuntu/proj/.agents/plugins/tmem/hooks.json): Hooks `PreInvocation` (speculative recall + `/tmem` command dispatch + milestone prompting) and `PostToolUse` (tracks mutations, errors, and test passes).
- [`rules/AGENTS.md`](file:///home/ubuntu/proj/.agents/plugins/tmem/rules/AGENTS.md): Instructs agents on consuming `<tmem_speculative_memory>`.
- [`skills/tmem/SKILL.md`](file:///home/ubuntu/proj/.agents/plugins/tmem/skills/tmem/SKILL.md): Natural language trace memory management.

### 2. DeepSeek Harness (`@deepseek-ai/dsh`)
- One-command plugin install: `dsh plugin add github:kentzjh-cmd/tmem`
- Cordis Plugin: [`tmem/dsh_plugin.mjs`](file:///home/ubuntu/proj/tmem/dsh_plugin.mjs) (`agent/pre-step` speculative recall, `tools/post-execute` milestone detection, and native `/tmem` command registration).
- Native session adapter: [`tmem/adapters/dsh.py`](file:///home/ubuntu/proj/tmem/adapters/dsh.py) supporting `session.v4.jsonl`, `v3`, and multi-file `apply_patch` diffs.

---

## Memory Capture & Management Workflows

1. **Autonomous Milestone Prompting**
   When `tmem` detects a complete cycle of `MUTATE -> VERIFY (exit 0)`, it prompts the agent to append a single non-intrusive inquiry:
   > 💡 *Would you like to persist this execution path to memory? (Type `/tmem project`, `/tmem global`, or `/tmem both`)*
2. **Slash Command Dispatch (`/tmem`)**
   - `/tmem project [notes]`: Slices and saves the current session to `.tmem/traces/`.
   - `/tmem global [notes]`: Slices and distills portable L2 repairs to `~/.tmem/playbooks/`.
   - `/tmem both [notes]`: Saves to both project and global tiers.
   - `/tmem status`: Displays stored trace counts across tiers.
   - `/tmem list [project|global|both]`: Lists playbooks and their active guard status.
   - `/tmem inspect <trace_id> [index|playbook|raw|diff]`: Progressively inspects memory artifacts.
   - `/tmem replay <trace_id>`: Forensically plays back terminal executions with critical path highlights.
   - `/tmem diff <trace_id>`: Displays the unified git patch recorded during the session.
   - `/tmem sync`: Refreshes SQLite indexes, evaluates guard probes, and updates `MEMORIES.md`.
   - `/tmem forget <trace_id>`: Removes specified trace and deletes associated raw recordings.
   - `/tmem gc`: Executes metabolic garbage collection, pruning stale L0 paths while preserving L1/L2 invariants.
3. **Automated Git Hook Safeguard (`.git/hooks/post-commit`)**
   Every `git commit` silently triggers incremental background slicing and persistence (`tmem capture --scope auto --quiet`).

---

## Quickstart & CLI Reference

```bash
# Initialize tmem in the current repository
python3 -m tmem.cli init --agents agy,dsh

# View memory statistics across project and global tiers
python3 -m tmem.cli status

# Inspect a trace using progressive disclosure (levels: index | playbook | raw | diff)
python3 -m tmem.cli inspect tr_cc3a687f02 --level playbook

# Play back full forensic terminal execution marking critical paths
python3 -m tmem.cli replay tr_cc3a687f02

# View recorded git unified diff patch
python3 -m tmem.cli diff tr_cc3a687f02

# Synchronize living memory index (MEMORIES.md & MEM.md)
python3 -m tmem.cli sync

# Run evaluation suite on public benchmark trajectories
python3 -m tmem.cli evaluate

# Query memories by intent, file, or error signature
python3 -m tmem.cli lookup "ZeroDivisionError" --file src/billing.py

# Run metabolic garbage collection
python3 -m tmem.cli gc
```

---

## Public Trajectory Datasets & Benchmark Evaluation

`tmem` includes an evaluation engine validating compilation efficiency, downstream agent efficacy, and progressive disclosure token economics against SWE-bench, ATIF, and DSH trajectories:

### 1. Upstream Compilation & Noise Filtering Benchmark

| Trajectory Instance | Trajectory Standard | Raw Turns | L0 Steps | Compression Ratio | Noise Filtered | L1 Files | L2 Repairs | Recall Rank | Slicing Latency | Retrieval Latency |
|---|---|---|---|---|---|---|---|---|---|---|
| `dsh_e2e_session` | `dsh (session.v4.jsonl)` | 9 | 4 | **55.6%** | **100.0%** (3 -> 0) | 3 files | 1 pair | **#1** | 0.49 ms | 2.6 ms |
| `dsh_long_lifecycle_session` | `dsh (session.v4.jsonl)` | 27 | 9 | **66.7%** | **100.0%** (6 -> 0) | 7 files | 1 pair | **#1** | 0.51 ms | 2.5 ms |
| `dsh_multiturn_complex_session` | `dsh (session.v4.jsonl)` | 17 | 6 | **64.7%** | **100.0%** (6 -> 0) | 5 files | 1 pair | **#1** | 0.42 ms | 2.7 ms |
| `astropy__astropy-12907` | `ATIF (trajectory.json)` | 17 | 7 | **58.8%** | **100.0%** (9 -> 0) | 3 files | 1 pair | **#1** | 0.29 ms | 2.6 ms |
| `django__django-11099` | `SWE-bench (.traj)` | 8 | 3 | **62.5%** | **100.0%** (4 -> 0) | 1 file | 1 pair | **#1** | 0.19 ms | 2.5 ms |
| `flask__flask-5014` | `SWE-bench (.traj)` | 6 | 3 | **50.0%** | **100.0%** (3 -> 0) | 1 file | 1 pair | **#1** | 0.16 ms | 2.4 ms |
| `pytest-dev__pytest-7432` | `SWE-bench (.traj)` | 15 | 6 | **60.0%** | **100.0%** (6 -> 0) | 2 files | 1 pair | **#1** | 0.24 ms | 2.6 ms |
| `scikit-learn__scikit-learn-13496` | `SWE-bench (.traj)` | 12 | 6 | **50.0%** | **100.0%** (4 -> 0) | 2 files | 1 pair | **#1** | 0.23 ms | 2.5 ms |
| `sympy__sympy-20590` | `ATIF (trajectory.json)` | 6 | 4 | **33.3%** | **100.0%** (3 -> 0) | 2 files | 0 pair | **#1** | 0.18 ms | 2.4 ms |
| **Overall Average (9 Cases)** | **Multi-Format** | **13.0** | **5.33** | **55.73%** | **100.0%** | **2.89 files** | **8 pairs** | **Top-1: 100% (MRR: 1.0)** | **0.30 ms** | **2.53 ms** |

### 2. Downstream Agent Efficacy & Code Drift Resilience

| Task Instance | Baseline Turns | With tmem | Steps Saved | Baseline Errors | Errors Avoided | First-Action Target Precision | Token Savings | Drift Resilience |
|---|---|---|---|---|---|---|---|---|
| `dsh_e2e_session` | 9 | 4 | **-55.6%** | 1 error | **1 (100.0%)** | `MUTATE(tmem/adapters/dsh.py)` | **-85.2%** | ✅ L2_INVARIANT |
| `dsh_long_lifecycle_session` | 27 | 9 | **-66.7%** | 1 error | **1 (100.0%)** | `MUTATE(src/limiter.py)` | **-88.8%** | ✅ L2_INVARIANT |
| `dsh_multiturn_complex_session` | 17 | 6 | **-64.7%** | 1 error | **1 (100.0%)** | `MUTATE(tmem/adapters/base.py)` | **-88.5%** | ✅ L2_INVARIANT |
| `astropy__astropy-12907` | 17 | 7 | **-58.8%** | 1 error | **1 (100.0%)** | `READ(astropy/modeling/separable.py)` | **-85.9%** | ✅ L2_INVARIANT |
| `django__django-11099` | 8 | 3 | **-62.5%** | 1 error | **1 (100.0%)** | `READ(django/contrib/auth/validators.py)` | **-85.9%** | ✅ L2_INVARIANT |
| `flask__flask-5014` | 6 | 3 | **-50.0%** | 1 error | **1 (100.0%)** | `READ(src/flask/blueprints.py)` | **-82.6%** | ✅ L2_INVARIANT |
| `pytest-dev__pytest-7432` | 15 | 6 | **-60.0%** | 2 errors | **2 (100.0%)** | `READ(src/_pytest/skipping.py)` | **-85.8%** | ✅ L2_INVARIANT |
| `scikit-learn__scikit-learn-13496` | 12 | 6 | **-50.0%** | 2 errors | **2 (100.0%)** | `READ(sklearn/ensemble/iforest.py)` | **-81.4%** | ✅ L2_INVARIANT |
| `sympy__sympy-20590` | 6 | 4 | **-33.3%** | 0 error | **0 (100.0%)** | `READ(sympy/matrices/expressions/matexpr.py)` | **-73.7%** | ✅ L1_TOPOLOGY |
| **Downstream Summary** | **Avg 13.0 turns** | **Avg 5.33 turns** | **-55.73%** | **10 total** | **10 (100.0%)** | **100.0% Precision** | **-84.2%** | **100% Drift Safe** |

### 3. Multi-Tier Progressive Disclosure Footprint Benchmark

| Task Instance | Tier 2 Raw (Tokens) | Tier 1 Playbook (Tokens) | Tier 0 Index (Tokens) | Playbook Savings | Index Savings | Raw Store Verified | Replay Rendered |
|---|---|---|---|---|---|---|---|
| `dsh_e2e_session` | ~2,130 tok | ~320 tok | ~35 tok | **-85.0%** | **-98.4%** | ✅ Verified | ✅ Rendered |
| `dsh_long_lifecycle_session` | ~6,090 tok | ~545 tok | ~35 tok | **-91.1%** | **-99.4%** | ✅ Verified | ✅ Rendered |
| `dsh_multiturn_complex_session` | ~3,890 tok | ~410 tok | ~35 tok | **-89.5%** | **-99.1%** | ✅ Verified | ✅ Rendered |
| `astropy__astropy-12907` | ~3,890 tok | ~455 tok | ~35 tok | **-88.3%** | **-99.1%** | ✅ Verified | ✅ Rendered |
| `django__django-11099` | ~1,910 tok | ~275 tok | ~35 tok | **-85.6%** | **-98.2%** | ✅ Verified | ✅ Rendered |
| `flask__flask-5014` | ~1,470 tok | ~275 tok | ~35 tok | **-81.3%** | **-97.6%** | ✅ Verified | ✅ Rendered |
| `pytest-dev__pytest-7432` | ~3,450 tok | ~410 tok | ~35 tok | **-88.1%** | **-99.0%** | ✅ Verified | ✅ Rendered |
| `scikit-learn__scikit-learn-13496` | ~2,790 tok | ~410 tok | ~35 tok | **-85.3%** | **-98.7%** | ✅ Verified | ✅ Rendered |
| `sympy__sympy-20590` | ~1,470 tok | ~280 tok | ~35 tok | **-81.0%** | **-97.6%** | ✅ Verified | ✅ Rendered |
| **Progressive Summary** | **~3,010 tok** | **~376 tok** | **~35 tok** | **-86.13%** | **-98.57%** | **100% Fidelity** | **Sync: 1.68 ms** |

---

## Directory Structure

```text
tmem/
├── pyproject.toml                 # Packaging & tmem CLI entrypoints
├── README.md                      # Chinese documentation
├── README_EN.md                   # English documentation
├── MEMORIES.md                    # Living memory index for zero-plugin navigation
├── MEM.md                         # Symbolic link alias to MEMORIES.md
├── AGENTS.md                      # Agent memory consumption & milestone protocol
├── benchmarks/                    # Public evaluation datasets
│   └── data/
│       ├── swe_bench_django_11099.traj.json
│       ├── swe_bench_flask_5014.traj.json
│       ├── swe_bench_sympy_20590.atif.json
│       └── dsh_e2e_session.jsonl
├── tmem/
│   ├── models.py                  # ActionNode, GuardProbe, RawExecutionTrace & Playbook models
│   ├── slicer.py                  # Git Ground-Truth backward trace slicer
│   ├── raw_storage.py             # RawExecutionTrace storage, replay & unified diff renderer
│   ├── mem_doc.py                 # MEMORIES.md / MEM.md living index generator & atomic sync
│   ├── guards.py                  # <5ms zero-token pre-flight guard validator & formatter
│   ├── store.py                   # Project (.tmem/) + Global (~/.tmem/) dual-tier store
│   ├── evaluate.py                # Public benchmark evaluator, downstream efficacy & progressive disclosure
│   ├── cli.py                     # CLI parser, /tmem slash commands, inspection & installation
│   ├── dsh_plugin.mjs             # @deepseek-ai/dsh native Cordis runtime plugin
│   └── adapters/
│       ├── base.py                # Session abstraction, secret scrubbing & AST symbols
│       ├── agy.py                 # Antigravity (agy) transcript adapter
│       ├── dsh.py                 # DeepSeek Harness (dsh) session.v4.jsonl adapter
│       └── swe_bench.py           # SWE-bench (.traj) & ATIF (trajectory.json) adapter
└── tests/
    ├── test_tmem_e2e.py           # Core slicing, dual-tier store & guard tests
    ├── test_dsh_e2e.py            # DSH v4 parsing & live Cordis runtime tests
    ├── test_raw_and_memories.py   # Raw storage, progressive disclosure & living index tests
    └── test_evaluation_benchmark.py # Benchmark evaluation automated test suite
```

---

## License

Apache-2.0
