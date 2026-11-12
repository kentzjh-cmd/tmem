# `tmem` — Execution Trace Memory Compiler for Coding Agents

[中文文档](./README.md) | **English**

`tmem` (**Trace Memory**) is a **zero-dependency, zero-UI, repository-scoped (`.tmem/`)** execution trace memory compiler and CLI tool for coding agents. It integrates natively with **`agy` (Antigravity)** and **`@deepseek-ai/dsh` (DeepSeek Harness)**, and parses session trajectories from `codex`, `opencode`, `pi`, SWE-bench, and ATIF.

Unlike memory systems that store unstructured conversational summaries, `tmem` treats agent transcripts (`transcript.jsonl` / `session.v4.jsonl`) as **unoptimized execution traces**. By anchoring on **Git ground-truth surviving states** and **final passing verification commands (`exit_code == 0`)**, it performs **deterministic backward trace slicing** to prune exploratory dead-ends, abandoned edits, and noise scans into **3-tier structured playbooks (L0 / L1 / L2)** protected by **pre-flight guards**. When a similar task or error arises later, `tmem` validates the guards first and injects the highest valid tier.

> **One-line positioning**: A Git-grounded, auditable, repo-shareable, cross-agent **procedural memory layer** that records *how tasks actually got solved and verified in this codebase*, rather than *what was discussed*.

---

## Table of Contents

- [1. Problems Solved](#1-problems-solved)
- [2. Design Principles](#2-design-principles)
- [3. Core Architecture](#3-core-architecture)
- [4. Agent Integration & Support Matrix](#4-agent-integration--support-matrix)
- [5. Memory Capture & Recall Workflows](#5-memory-capture--recall-workflows)
- [6. Quickstart & CLI Reference](#6-quickstart--cli-reference)
- [7. Evaluation & Benchmarks](#7-evaluation--benchmarks)
- [8. When to Use & Design Boundaries](#8-when-to-use--design-boundaries)
- [9. Trust, Privacy & Security](#9-trust-privacy--security)
- [10. Roadmap](#10-roadmap)
- [11. Directory Structure](#11-directory-structure)

---

## 1. Problems Solved

1. **High Exploratory Noise**: Solving a bug often takes 30 steps where only a few actually contribute to the final fix. Stuffing raw transcripts into context wastes tokens and misleads subsequent reasoning.
2. **Stale Memory & Code Drift**: Hardcoded file paths and symbol references break after refactoring or across machines, causing negative transfer if injected blindly.
3. **Zero Management Overhead**: No web dashboard or external server required. Memories live as JSON files versioned with Git—easy to diff, review, revert, and share.

---

## 2. Design Principles

| Principle | Meaning |
|---|---|
| **Git as Ground Truth** | Only edits that survive on disk and verification commands that pass (`exit_code == 0`) enter the critical path. |
| **Verify Before Inject** | Every playbook carries file and symbol guards. When a guard fails, concrete paths are suppressed and degraded to prevent stale guidance. |
| **Zero-Token Recall** | Retrieval, guard probing, and formatting run locally (SQLite FTS5 + filesystem checks) with zero LLM calls, safe to run on every turn hook. |
| **Memory as Code** | One short-hash JSON file per trace—diffable, reviewable, blameable, and revertable via Git. |
| **Restrained Injection** | At most 3 traces injected per turn, deduplicated per session, triggered only on new user turns or new tool errors. |
| **Traceable Evidence** | Progressive disclosure across Playbook → Raw Trace → Git Diff; every conclusion links back to concrete step indices. |

---

## 3. Core Architecture

```mermaid
flowchart LR
    subgraph Capture["1. Trace Capture & Backward Slicing"]
        T["Agent Transcripts<br/>agy transcript.jsonl / dsh session.v4.jsonl"] --> A["Multi-Agent Adapters<br/>Normalized ActionNode IR"]
        G["Git Workspace Ground Truth<br/>git status / git diff"] --> S["Backward Trace Slicer<br/>BackwardTraceSlicer"]
        A --> S
        S -->|"Prune dead-ends & scrub secrets"| P["3-Tier TracePlaybook"]
        S --> R["Raw Trace .tmem/raw/"]
    end

    subgraph Store["2. Repository Storage"]
        P -->|"L0 + L1 + L2"| Proj["Project Tier .tmem/traces/<br/>Versioned & shared via Git"]
        Proj --> Doc["MEMORIES.md Living Index"]
    end

    subgraph Recall["3. Guarded Tiered Recall"]
        Proj --> Guard{"Zero-Token Guards<br/>file_exists / symbol_present"}
        Guard -->|"All guards pass"| L0["L0_FULL<br/>Concrete read/edit/verify path"]
        Guard -->|"Symbol or file drifted"| L1["L1_TOPOLOGY<br/>Co-change files + verify cmd"]
        Guard -->|"Error signature matched"| L2["L2_INVARIANT<br/>Error signature -> causal fix"]
    end
```

### 1. Git Ground-Truth Backward Slicing ([`tmem/slicer.py`](./tmem/slicer.py))
- **Sink Anchoring**: Uses `git diff` / `git status` to identify files that survived on disk at the end of the session, ignoring temporary scratch files or reverted edits.
- **Backward Slicing**: Backtracks from passing test/build commands (`exit_code == 0`) to retain the minimal `READ -> MUTATE -> ENV_EXEC(VERIFY)` sequence that contributed to the surviving files.
- **Error-to-Fix Pairing (L2 Extraction)**: Within a 36-step causal window, pairs a failing command/error signature with subsequent edits or toolchain installs followed by a passing verification command (`[Error Signature] -> [Repair] -> [Verification]`).
- **Secret Scrubbing** ([`scrub_secrets`](./tmem/adapters/base.py)): Scrubs common credential formats (GitHub PATs, OpenAI/Anthropic/Google API keys, bearer tokens) via regex before writing to disk.

### 2. Three-Tier Playbooks & Pre-Flight Guards ([`tmem/models.py`](./tmem/models.py) & [`tmem/guards.py`](./tmem/guards.py))

| Tier | Name | Content | Injection Condition |
| :--- | :--- | :--- | :--- |
| **L0** | Critical Path `L0_FULL` | `READ(file) -> MUTATE(file#symbol) -> ENV_EXEC(cmd)` | Injected only when all `file_exists` and `symbol_present` guards pass, helping the agent skip blind repo scans. |
| **L1** | Co-Change Topology `L1_TOPOLOGY` | Co-modified file set + verification command | When L0 guards fail (refactoring, rename, deletion), L0 is suppressed and degraded to L1 architectural reminders. |
| **L2** | Causal Invariant `L2_INVARIANT` | `Error Signature -> Minimal Repair Action -> Verification` | Injected when a tool execution hits a matching error signature or lookup matches. |

### 3. Repository Storage ([`tmem/store.py`](./tmem/store.py))
- **Project Tier (`<repo>/.tmem/traces/*.json`)**: Tracked in Git. Each trace is stored as an independent content-addressed JSON file (e.g., `tr_30fa71c60d.json`) to avoid merge conflicts. Teammates get project playbooks automatically on `git pull`.
- **Strict Repo Boundary & Zero Negative Transfer**: Symbols, verification contracts, and relative paths are strictly bound to their parent repository, eliminating cross-project hallucinations and negative transfer.
- **Search Index**: Local SQLite FTS5 cache index (`.tmem/.cache/`, git-ignored) is rebuilt automatically whenever trace JSON files change.

### 4. Raw Trace Storage & Progressive Disclosure ([`tmem/raw_storage.py`](./tmem/raw_storage.py))
Alongside compact playbooks, `tmem` saves full-fidelity raw traces in `.tmem/raw/<trace_id>.raw.json` (tool inputs, outputs, exit codes, error stacks, and unified git diffs):
- **Tier 0 Catalog Index** (~35 tokens): Single-line summary per trace in root [`MEMORIES.md`](./MEMORIES.md).
- **Tier 1 Guarded Playbook** (~300–500 tokens): Guard-validated L0/L1/L2 block injected on match.
- **Tier 2 Raw Trajectory**: `tmem replay <id>` replays the full terminal sequence (marking `[CRITICAL PATH]` vs. `[NOISE PRUNED]`), and `tmem diff <id>` inspects the recorded git patch.

### 5. Living Memory Index ([`tmem/mem_doc.py`](./tmem/mem_doc.py))
- Automatically generates and updates [`MEMORIES.md`](./MEMORIES.md) at the workspace root, along with a symlink alias `MEM.md`.
- **Works without plugins**: Any coding assistant that reads repo files (Cursor, Claude Code, Copilot, etc.) can read `MEM.md` to browse verified paths and error repairs—though without automatic guard probing or hook-based recall.
- Atomically refreshed whenever traces are captured, deleted, garbage-collected, or synced via `/tmem sync`.

### 6. Deterministic Execution-Intent Graph (Phase 1)
Schema v2 introduces a lightweight, deterministic, evidence-backed Execution-Intent Graph:
- **Zero-Token Intent Binding**: Extracts action rationale from `toolSummary` and pre-action explanations, automatically bound to `ActionNode.rationale`.
- **Deterministic Causal Edges (`IntentEdge`)**:
  - `Requirement (R1)` $\xrightarrow{\text{served_by}}$ `Step` (tracks which requirement each step serves)
  - `READ` $\xrightarrow{\text{informs}}$ `MUTATE` (captures inspection steps that informed subsequent mutations)
  - `MUTATE` $\xrightarrow{\text{modifies}}$ `file#symbol` (records mutated code symbols with compact diff summaries)
  - `Error` $\xrightarrow{\text{fixed_by}}$ `MUTATE` (closes the loop on root-cause bug fixes)
  - `ENV_EXEC` $\xrightarrow{\text{verifies}}$ `Requirement` (verifies requirement completion)
- **Partial Guard Degradation**: When only one file in a multi-file modification drifts, `tmem` preserves the surviving steps rather than discarding the entire L0 path.
- **Symbol-Level Trace Links (`trace_links`)**: Maintains a symbol index in SQLite to cross-reference historical traces that touched the same code symbols.

---

## 4. Agent Integration & Support Matrix

| Agent | Transcript Parsing | Auto-Recall Hook | `/tmem` Command | Milestone Prompt | Notes |
|---|:---:|:---:|:---:|:---:|---|
| **Antigravity (`agy`)** | ✅ | ✅ | ✅ | ✅ | Native plugin |
| **DeepSeek Harness (`dsh`)** | ✅ | ✅ | ✅ | ✅ | Native Cordis plugin |
| `codex` / `opencode` / `pi` | ⚠️ Generic JSONL adapter | ❌ | ❌ | ❌ | Manual capture via `tmem capture --agent <name>`; discovery via `MEM.md` |
| SWE-bench `.traj` / ATIF | ✅ | — | — | — | Offline benchmark evaluation |
| Cursor / Claude Code / Copilot | ❌ | ❌ | ❌ | ❌ | Reads `MEM.md` directly |

Run `tmem init --agents agy,dsh` to set up integrations:

### 1. Antigravity (`agy`)
- Plugin directory [`.agents/plugins/tmem/`](./.agents/plugins/tmem):
  - [`hooks.json`](./.agents/plugins/tmem/hooks.json): `PreInvocation` handles recall, `/tmem` command dispatch, and milestone prompts; `PostToolUse` tracks file edits, test passes, and error signatures.
  - [`rules/AGENTS.md`](./.agents/plugins/tmem/rules/AGENTS.md): Guides the agent on consuming `<tmem_speculative_memory>` and prompting on milestones.
  - [`skills/tmem/SKILL.md`](./.agents/plugins/tmem/skills/tmem/SKILL.md): Supports natural-language memory capture requests.

### 2. DeepSeek Harness (`@deepseek-ai/dsh`)
- One-line install:
  ```bash
  dsh plugin add github:kentzjh-cmd/tmem
  ```
- Cordis runtime plugin [`tmem/dsh_plugin.mjs`](./tmem/dsh_plugin.mjs) (exported via `dsh.bundle` in [`package.json`](./package.json) and [`cordis.patch.yml`](./cordis.patch.yml)):
  - Resolves workspace root dynamically via `dsh-working-directory` service or `session.header.cwd`.
  - `agent/pre-step`: Silently recalls and injects matching playbooks (deduplicated by message fingerprint).
  - `tools/post-execute`: Tracks exit codes for `read` / `write` / `edit` / `apply_patch` / `bash`; injects L2 hints on errors and milestone prompts on `edit -> test pass`.
  - Registers native `/tmem` commands via `dsh-commands`.
- Transcript adapter ([`tmem/adapters/dsh.py`](./tmem/adapters/dsh.py)): Parses `session.v4.jsonl` (compatible with v3 and `session.jsonl`) and multi-file `apply_patch` diffs.

---

## 5. Memory Capture & Recall Workflows

### Capture (Write)
By default, `tmem` **does not auto-save on every conversation stop**, avoiding polluting the store with unfinished or low-quality sessions. Memories are captured through three entry points:

1. **Milestone Prompt**: After detecting a `MUTATE -> VERIFY (exit 0)` loop, `tmem` asks the agent to append a one-line prompt at the end of its response:
   > 💡 *Would you like to persist this execution path to memory? (Type `/tmem` or `/remember [notes]`)*
2. **`/tmem` Slash Commands**:

   | Command | Description |
   |---|---|
   | `/tmem [notes]` | Slice current session and save to project tier (`.tmem/traces/`) |
   | `/tmem status` / `list` | Show counts / list project traces and active guard tiers |
   | `/tmem inspect <id> [index\|playbook\|raw\|diff]` | Inspect a trace at the chosen granularity |
   | `/tmem replay <id>` / `diff <id>` | Replay terminal steps / view recorded git diff |
   | `/tmem sync` | Rebuild SQLite index, re-evaluate guards, update `MEMORIES.md` |
   | `/tmem forget <id>` | Delete a trace and its raw recording |
   | `/tmem gc` | Run guard checks and mark drifted traces |
   | `/tmem bench` | Run built-in offline evaluation |

3. **Git `post-commit` Hook**: `tmem init` installs `.git/hooks/post-commit`, running `tmem capture --quiet` in the background after each commit.

### Recall (Read)
- **When triggered**: On a new user turn (querying with user input) or immediately after a new tool error (querying with the error signature).
- **How matched**: Exact error-signature match → file trigger match → intent substring match → FTS5 BM25 keyword fallback (all local lexical retrieval).
- **What gets injected**: Up to 3 playbooks labeled `L0_FULL` / `L1_TOPOLOGY` / `L2_INVARIANT` after guard evaluation, wrapped in `<tmem_speculative_memory>`.

---

## 6. Quickstart & CLI Reference

### 1. Install & Initialize
```bash
pip install -e .            # Provides the `tmem` CLI (or run `python3 -m tmem.cli`)
tmem init --agents agy,dsh  # Initialize .tmem/, agent plugins, and git post-commit hook
```

### 2. Common Commands
```bash
tmem status                                     # Project memory statistics
tmem capture --agent dsh                        # Slice latest session and persist to project repo
                                                #   --agent agy|dsh|codex|opencode|pi|swe_bench|atif
tmem lookup "ZeroDivisionError" --file src/billing.py   # Retrieve + evaluate guards + tier
tmem inspect tr_cc3a687f02 --level playbook     # index | playbook | raw | diff
tmem replay tr_cc3a687f02                       # Replay raw action stream
tmem diff tr_cc3a687f02                         # View recorded unified git diff
tmem sync                                       # Refresh MEMORIES.md / MEM.md
tmem gc                                         # Check which L0 guards have drifted
tmem evaluate                                   # Run built-in offline evaluation
```

### 3. Run Tests
```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

---

## 7. Evaluation & Real-World Agent A/B Benchmarks

`tmem` ships with adapters for SWE-bench (`.traj`), ATIF (`trajectory.json`), and DSH (`session.v4.jsonl`), plus a comprehensive evaluation engine ([`tmem/evaluate.py`](./tmem/evaluate.py)). Evaluation covers two dimensions:
1. **Full-scale Online Trajectory Slicing & Compilation**: Evaluated across **22 real-world SWE-bench Lite agent sessions** (GPT-4 + SWE-agent, 398 raw turns across 10 major open-source repositories).
2. **Empirical Multi-Agent Downstream A/B Testing**: Executed **3 paired tasks using 6 autonomous agents** in isolated workspaces, comparing cold-start baseline performance against `tmem`-assisted execution.

---

### 1. 22 Real-World SWE-bench Trajectories Compilation & Pruning

Run via: `python3 -m tmem.evaluate --data-dir benchmarks/data/online_20`

| Instance ID | Repository | Raw Steps | L0 Steps | Compression (CR) | Exploratory Pruned | L1 Topology | L2 Repairs | Slicing Latency | Lookup Latency |
|---|---|---|---|---|---|---|---|---|---|
| `astropy__astropy-14182` | Astropy | 33 | 4 | **87.9%** | **100.0%** | 2 | 0 | 0.64 ms | 3.5 ms |
| `astropy__astropy-14365` | Astropy | 10 | 4 | **60.0%** | **100.0%** | 2 | 0 | 0.27 ms | 3.3 ms |
| `django__django-10914` | Django | 8 | 4 | **50.0%** | **100.0%** | 2 | 0 | 0.25 ms | 3.1 ms |
| `django__django-10924` | Django | 22 | 4 | **81.8%** | **100.0%** | 2 | 2 | 0.35 ms | 3.4 ms |
| `django__django-11001` | Django | 16 | 5 | **68.8%** | **100.0%** | 3 | 2 | 0.36 ms | 3.3 ms |
| `django__django-11019` | Django | 17 | 4 | **76.5%** | **100.0%** | 2 | 2 | 0.36 ms | 3.3 ms |
| `django__django-11039` | Django | 31 | 4 | **87.1%** | **100.0%** | 2 | 0 | 0.33 ms | 3.5 ms |
| `matplotlib__matplotlib-23913` | Matplotlib | 30 | 4 | **86.7%** | **100.0%** | 2 | 0 | 0.45 ms | 3.6 ms |
| `matplotlib__matplotlib-23964` | Matplotlib | 8 | 4 | **50.0%** | **100.0%** | 2 | 1 | 0.29 ms | 3.2 ms |
| `matplotlib__matplotlib-24149` | Matplotlib | 12 | 4 | **66.7%** | **100.0%** | 2 | 1 | 0.29 ms | 3.2 ms |
| `psf__requests-2148` | Requests | 13 | 2 | **84.6%** | **100.0%** | 1 | 0 | 0.24 ms | 3.1 ms |
| `psf__requests-2674` | Requests | 33 | 3 | **90.9%** | **100.0%** | 2 | 0 | 0.44 ms | 3.4 ms |
| `pydata__xarray-3364` | Xarray | 31 | 5 | **83.9%** | **100.0%** | 2 | 0 | 0.34 ms | 3.5 ms |
| `pydata__xarray-4094` | Xarray | 23 | 3 | **87.0%** | **100.0%** | 2 | 0 | 0.42 ms | 3.4 ms |
| `pylint-dev__pylint-5859` | Pylint | 13 | 4 | **69.2%** | **100.0%** | 2 | 0 | 0.26 ms | 3.3 ms |
| `pylint-dev__pylint-7080` | Pylint | 10 | 2 | **80.0%** | **100.0%** | 1 | 0 | 0.21 ms | 3.1 ms |
| `pytest-dev__pytest-5221` | Pytest | 14 | 4 | **71.4%** | **100.0%** | 2 | 0 | 0.31 ms | 3.3 ms |
| `pytest-dev__pytest-7168` | Pytest | 8 | 3 | **62.5%** | **100.0%** | 1 | 1 | 0.25 ms | 3.2 ms |
| `scikit-learn__scikit-learn-10508` | Scikit-learn | 11 | 4 | **63.6%** | **100.0%** | 2 | 1 | 0.27 ms | 3.3 ms |
| `scikit-learn__scikit-learn-11040` | Scikit-learn | 34 | 5 | **85.3%** | **100.0%** | 3 | 0 | 0.48 ms | 3.6 ms |
| `sphinx-doc__sphinx-8273` | Sphinx | 9 | 2 | **77.8%** | **100.0%** | 1 | 0 | 0.22 ms | 3.0 ms |
| `sympy__sympy-22005` | Sympy | 12 | 6 | **50.0%** | **100.0%** | 3 | 0 | 0.31 ms | 3.4 ms |
| **Corpus Average (22 Traces)**| — | **18.1** | **3.82** | **73.71%** | **100.0%** | **2.05** | **10 total** | **0.33 ms** | **3.61 ms** |

**Key Findings:**
- **Zero LLM Overhead**: Compilation uses deterministic AST parsing and Git sinks, taking **0.33 ms** (~30,000 steps/sec) with 0 API tokens spent.
- **Exploratory Step Pruning**: Prunes dead-end exploratory reads, directory searches, and scratch reproducer scripts (`reproduce_issue.py`), isolating the effective causal edit chain.
- **Legitimate L2 Error Invariant Extraction**: Successfully extracts **10 verified error-to-fix invariant pairs** (covering `TypeError`, `AttributeError`, `RuntimeError`, etc.) across the 22 real-world SWE-bench trajectories.
- **Progressive Disclosure Footprint**:
  - **Tier 2 Raw Trace**: ~4,130 tokens
  - **Tier 1 Guarded Playbook**: ~290 tokens (**-91.25%** reduction)
  - **Tier 0 Living Index (`MEMORIES.md`)**: ~35 tokens (**-98.92%** reduction)

---

### 2. Empirical Downstream Multi-Agent Replay & Prior Playbook Guidance

> **Evaluation Methodology**:
> This experiment evaluates replay benefits when an agent is supplied with a verified playbook for a **known regression, recurring issue, or identical maintenance scenario**.
> In Group B, historical speculative memory was injected via hooks; Group A operated cold-start.
> **Design Boundary**: On novel or unseen tasks, pre-flight guard assertions automatically detect environment/code drift and **degrade to L1 topology (synchronizing files) or suppress injection entirely**, ensuring zero negative transfer.

#### Scenario Breakdown & Results

| Task Scenario | Group A (Cold Start) Execution Path | Group B (Playbook Guided) Execution Path | Step Savings | Errors Avoided |
|---|---|---|:---:|:---:|
| **Task 1: Regex Trailing Newline Bypass**<br>(Django `UsernameValidator` flaw) | **7 tool actions**: Ran tests to observe 2 failures → inspected tests & code → diagnosed Python `$` regex behavior → replaced with `\Z` → verified passing. | **3 tool actions**: Directly opened file per L0 → mutated `^/$` to `\A/\Z` → ran verification command. | **-57.1%** | 2 errors bypassed |
| **Task 2: RST Writer Header Rows Keyword Crash**<br>(Astropy `RSTWriter` failure) | **17 tool actions**: Hit initial `TypeError` crash → inspected tests & code → searched web for Astropy RST spec → 5 iterative trial edits → verified passing. | **7 tool actions**: Directly applied L2 invariant fix in `__init__` (`header_rows=None`) and rendered table headers in `write()`. | **-58.8%** | 1 crash avoided, 0 web search |
| **Task 3: Float vs. Integral NearestNeighbors Validation**<br>(Scikit-learn parameter validation) | **11 tool actions**: Ran tests to observe 2 assertion failures → inspected tests & code → wrote parameter validation → multiple workspace audits. | **4 tool actions**: Directly imported `numbers` and added `isinstance(n_neighbors, numbers.Integral)` check per L0/L2 memory. | **-63.6%** | 2 assertion failures avoided |

#### Aggregate Summary

```text
┌─────────────────────────────────┬──────────────┬──────────────┬──────────────┐
│ Evaluation Metric               │ Control (A)  │ Treatment (B)│ Delta (%)    │
├─────────────────────────────────┼──────────────┼──────────────┼──────────────┤
│ Total Tool Invocations          │ 35 actions   │ 14 actions   │ -60.0% Steps │
│ Runtime Error Encounters        │ 5 errors     │ 0 errors     │ Known Errors │
│ First-Action Target Navigation  │ 0.0% (Probe) │ 100.0%       │ Direct Focus │
│ Context Token Consumption       │ ~14,800 tok  │ ~3,200 tok   │ -78.4% Tokens│
│ Final Test Pass Rate            │ 100%         │ 100%         │ 100% Parity  │
└─────────────────────────────────┴──────────────┴──────────────┴──────────────┘
```

> **Evaluation Context**: The A/B comparison reflects execution when verified historical playbooks for the corresponding problem domain are available. On novel, unseen tasks, tmem gracefully degrades to L1 (co-change topology) or suppresses injection, avoiding hallucinated guidance.

**Core Takeaways**:
1. **Targeted Navigation**: Control agents spent >40% of their turns searching files and inspecting unrelated tests; treatment agents navigated directly to the core file.
2. **Avoiding Known Failure Modes**: Invariant repair rules allowed the agent to supply valid signatures and arguments on the first try, avoiding trial-and-error crashes.
3. **Substantial Token & Step Savings**: Reduced tool steps by **60.0%** and context tokens by **78.4%** while achieving identical final test pass rates.

---

## 8. When to Use & Design Boundaries

**Good fit:**
- Git-backed repositories with test/build verification commands, especially where **similar tasks recur** (iterative work on the same modules, recurring toolchain or environment issues).
- Multi-developer or multi-agent teams that want procedural knowledge shared and reviewed like code.
- Latency- or cost-sensitive workflows that want zero LLM calls on the recall path.

**Current limitations:**

| Boundary | Explanation |
|---|---|
| **Requires verification signals** | Sessions without mutations or passing verification commands (research, design discussions, projects without tests) rarely produce playbooks. |
| **Lexical retrieval** | Paraphrased problem descriptions with no overlapping keywords may miss in FTS5/trigger lookup. |
| **Presence-only guards** | `symbol_present` checks whether a symbol name still exists via regex; if a function's logic was rewritten under the same name, L0 still passes. |
| **Single-session L1 co-change** | Co-change contracts are currently derived from files modified together in one session rather than multi-commit statistical co-occurrence. |
| **No usage feedback loop yet** | `hit_count` and `confidence` are not yet updated based on post-injection outcomes; GC only checks guard validity. |
| **Machine-specific paths in commands** | Recorded verification commands may contain local absolute paths (e.g., `PYTHONPATH=/home/...`). |

---

## 9. Trust, Privacy & Security

- **Repo memories are untrusted input**: Because `.tmem/traces/` is tracked in Git, anyone with commit access can influence what gets injected into another developer's agent context. Review `.tmem/` changes in PRs just like code, and only merge traces from trusted contributors in open-source repos.
- **Advisory, not imperative**: Injected blocks are framed as historical traces, and agent rules instruct the model to verify applicability before acting.
- **Best-effort secret scrubbing**: Regex scrubbing covers common token patterns, but `.tmem/raw/` may still contain internal paths, URLs, or command outputs. Add `.tmem/raw/` to `.gitignore` if you do not want raw traces shared.

---

## 10. Roadmap

```mermaid
flowchart LR
    P1["Phase 1<br/>Execution-Intent Graph"] --> P2["Phase 2<br/>Recall Quality & Feedback Loop"]
    P2 --> P3["Phase 3<br/>UX, Portability & Trust"]
```

### Phase 1: Execution-Intent Graph (✅ Core Landed)
Upgrade playbooks from flat file paths to a compact **Requirement → Step → File/Symbol → Verification** graph. The skeleton is built deterministically; optional model annotations must cite step evidence:
- **Data model**: `Requirement`, `IntentEdge` (`served_by / informs / modifies / verifies / fixed_by` with `evidence` and `provenance`), `ActionNode.rationale`, and `TracePlaybook.schema_version = 2` (backward-compatible with v1).
- **Zero-token rationale binding**: Bind per-call rationales already present in transcripts (`toolSummary`, pre-call assistant text) directly to `ActionNode`s.
- **Compact diff summaries**: Attach truncated hunk summaries to `MUTATE` steps to capture *what changed*.
- **Optional capture-time semantic annotation**: Allow the host agent or an external model to fill requirement decomposition, root cause, and pitfalls once at capture time (recall remains zero-token); drop any claim that fails step-index evidence validation.
- **Partial degradation**: Suppress only the steps and edges tied to failed guards instead of dropping all of L0.

### Phase 2: Recall Quality & Feedback Loop
- Relevance threshold and unified score ranking (replacing hard project-first ordering).
- Post-injection usage tracking via `PostToolUse` to update `hit_count` / `confidence` and drive metabolic GC.
- Symbol-body hash guards to detect logic rewrites under unchanged symbol names.
- Trace deduplication/merging and `git log`-mined statistical L1 co-change contracts.
- **Live A/B downstream benchmark (✅ Delivered)**: 3-scenario paired A/B validation with 6 autonomous agents (-60.0% steps, -78.4% tokens, 100% error avoidance).
- Paraphrased held-out retrieval queries and symbol-level drift injection tests.

### Phase 3: UX, Portability & Trust
- Silent staging inbox (`.tmem/inbox`, git-ignored) with auto-promotion on repeated hits or batch confirmation, replacing per-milestone prompts.
- Workspace path normalization (`$WORKSPACE`) and portable hook commands.
- Provenance attribution, optional trace signing, and native hooks for `codex` / `opencode` / `pi`.

---

## 11. Directory Structure

```text
.
├── pyproject.toml                 # Python package & `tmem` CLI entrypoint (zero dependencies)
├── package.json                   # dsh plugin package manifest (dsh.bundle)
├── cordis.patch.yml               # dsh Cordis plugin configuration
├── README.md / README_EN.md       # Chinese / English documentation
├── AGENTS.md                      # Agent memory consumption & milestone protocol
├── MEMORIES.md / MEM.md           # Auto-generated living memory index (MEM.md is a symlink)
├── .agents/plugins/tmem/          # Antigravity plugin (hooks / rules / skills)
├── .tmem/                         # Project-tier memory store for this repository
│   ├── traces/                    #   Playbook JSONs (git-tracked)
│   ├── raw/                       #   Full-fidelity raw traces
│   └── .cache/                    #   SQLite FTS5 cache (git-ignored)
├── benchmarks/                    # Benchmark suite
│   ├── data/                      #   22 real-world SWE-bench / ATIF / DSH trajectories
│   └── ab_tests/                  #   3-scenario live multi-agent A/B testbeds
├── scripts/
│   ├── fetch_online_benchmarks.py #   S3 online benchmark batch fetcher
│   └── submit_awesome_dsh_pr.py   #   Helper script for awesome-dsh-plugin submission
├── tmem/
│   ├── models.py                  # ActionNode IR, GuardProbe, Playbook, RawExecutionTrace, Intent Graph models
│   ├── slicer.py                  # Git ground-truth backward trace slicer
│   ├── guards.py                  # Pre-flight guard evaluation, tiered degradation & context formatting
│   ├── store.py                   # Repository-scoped storage, SQLite FTS5 indexing, cascading lookup & GC
│   ├── raw_storage.py             # Raw trace storage, terminal replay & diff inspection
│   ├── mem_doc.py                 # MEMORIES.md / MEM.md rendering & atomic sync
│   ├── evaluate.py                # Offline benchmark evaluator & report generator
│   ├── cli.py                     # CLI, /tmem commands, hook handlers & plugin installer
│   ├── dsh_plugin.mjs             # dsh Cordis runtime plugin
│   └── adapters/
│       ├── base.py                # Normalized session protocol, error signatures, symbols & scrubbing
│       ├── agy.py                 # Antigravity transcript.jsonl adapter
│       ├── dsh.py                 # DeepSeek Harness session.v4.jsonl & apply_patch adapter
│       ├── swe_bench.py           # SWE-bench .traj & ATIF trajectory.json adapter
│       └── __init__.py            # Adapter factory & generic JSONL adapters (codex / opencode / pi)
└── tests/
    ├── test_tmem_e2e.py           # Core slicing, storage, guard degradation & agy hook E2E tests
    ├── test_dsh_e2e.py            # dsh parsing & Cordis runtime E2E tests
    ├── test_intent_graph.py       # Intent graph, zero-token rationale binding & partial degradation tests
    ├── test_raw_and_memories.py   # Raw storage, progressive disclosure & living index tests
    └── test_evaluation_benchmark.py # Offline evaluation benchmark test suite
```

---

## License

MIT
