"""
Deterministic Backward Trace Slicer (Program Slicing applied to Agent Execution Traces).

Zero-LLM-alchemy compiler that takes a raw agent session + Git ground truth and performs:
1. Ground Truth Sink Identification (via git diff / status or mutated files)
2. Dead Mutation Elimination (DME)
3. Error -> Fix Causal Pair Extraction (L2 Universal Invariants)
4. Read Shrinking (pruning exploratory dead-end reads)
5. Speculative Guard Probe Generation (<5ms AST/file assertions)
"""

from __future__ import annotations

import datetime
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tmem import logger
from tmem.adapters.base import (
    ParsedSession,
    extract_local_imports,
    extract_symbols_for_line_ranges,
    extract_traceback_files,
    is_code_file,
    is_doc_file,
    is_filler_user_intent,
    is_high_entropy_error_signature,
    is_inline_script_command,
    is_meta_non_task_intent,
    is_source_code_file,
    is_test_file,
    scrub_secrets,
)
from tmem.cochange import find_cochange_for_files
from tmem.guards import check_symbol_in_ast
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    RawExecutionTrace,
    TracePlaybook,
)

TOOLCHAIN_INSTALL_RE = re.compile(
    r"\b(?:pip|pip3|uv\s+pip|uv|poetry|npm|pnpm|yarn|bun|cargo|go|apt|apt-get|brew)\s+(?:install|add|get|mod\s+tidy)\b|"
    r"\bcorepack\s+enable\b|"
    r"\bpython(?:3)?\s+setup\.py\s+(?:build|develop|build_ext|install)\b"
)

RE_TEST_SCRIPT_CMD = re.compile(
    r"^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:python\d*(?:\.\d+)?|node|bash|sh)\s+(?!-c\b)[\w\./\\-]*?(?:test|tests|reproduce|repro|simulate|check|verify|runtests)[\w\./\\-]*\b",
    re.I,
)

VERIFY_WHITELIST_RE = re.compile(
    r"\b(?:pytest|unittest|tox|nox|coverage|mypy|ruff|flake8|black|isort|pylint|"
    r"runtests(?:\.py)?|manage\.py\s+test|"
    r"python(?:3)?\s+setup\.py\s+build_ext\b|"
    r"npm\s+(?:test|run\s+(?:test|build|check|lint)\w*)|pnpm\s+(?:test|run\s+(?:test|build|check|lint)\w*)|"
    r"yarn\s+(?:test|run\s+(?:test|build|check|lint)\w*)|bun\s+test|"
    r"cargo\s+(?:test|check|build|clippy)|go\s+(?:test|build|vet)|"
    r"tsc|eslint|vitest|jest|mocha|make|cmake|gradle|mvn|"
    r"dsh\s+plugin\s+\S+)\b"
)

EXPLORATORY_CLAUSE_RE = re.compile(
    r"^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:ls|pwd|cd|cat|head|tail|echo|rm|mv|cp|mkdir|ln|chmod|chown|curl|wget|"
    r"find|grep|rg|sed|awk|which|whoami|id|uname|ps|kill|sleep|true|false|"
    r"git\s+(?:status|log|diff|add|commit|push|pull|fetch|remote|branch|checkout|config|clone|show|rev-parse|diff-tree)|"
    r"npm\s+view)\b"
)


def get_git_ground_truth(
    workspace_root: str,
    target_files: Optional[Set[str]] = None,
) -> Tuple[Optional[str], Set[str], Dict[str, List[str]], Optional[str]]:
    """
    Inspect Git repository to obtain:
    - current HEAD commit hash
    - set of actually modified/committed files (Ground Truth Sink)
    - touched symbols per file from git diff
    - commit subject line if working tree is clean (e.g. post-commit hook)
    """
    ws = Path(workspace_root)
    if not (ws / ".git").exists():
        return None, set(), {}, None

    commit_hash = None
    commit_subject: Optional[str] = None
    modified_files: Set[str] = set()
    file_symbols: Dict[str, List[str]] = {}

    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            commit_hash = res.stdout.strip()

        # Check uncommitted working tree changes first; if clean, inspect HEAD~1..HEAD
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if status_res.returncode == 0 and status_res.stdout.strip():
            for line in status_res.stdout.splitlines():
                if len(line) > 3:
                    path_str = line[3:].strip().strip('"')
                    if " -> " in path_str:
                        path_str = path_str.split(" -> ")[-1]
                    if not path_str.startswith(".tmem/"):
                        if not target_files or path_str in target_files:
                            modified_files.add(path_str)
        else:
            # Post-commit hook case: working tree is clean, inspect last commit
            subj_res = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if subj_res.returncode == 0 and subj_res.stdout.strip():
                subj = subj_res.stdout.strip()
                if not subj.startswith("chore(tmem):"):
                    commit_subject = subj
            names_res = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if names_res.returncode == 0:
                for line in names_res.stdout.splitlines():
                    p = line.strip()
                    if p and not p.startswith(".tmem/"):
                        if not target_files or p in target_files:
                            modified_files.add(p)

        diff_cmd = (
            ["git", "diff", "-U0", "HEAD~1", "HEAD"]
            if not status_res.stdout.strip()
            else ["git", "diff", "-U0", "HEAD"]
        )
        if target_files:
            diff_cmd.extend(["--"] + sorted(target_files))
        diff_res = subprocess.run(
            diff_cmd,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        file_ranges: Dict[str, List[Tuple[int, int]]] = {}
        if diff_res.returncode == 0 and diff_res.stdout:
            current_file = None
            hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
            for line in diff_res.stdout.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                elif current_file and line.startswith("@@ "):
                    m_h = hunk_re.match(line)
                    if m_h:
                        start_ln = max(1, int(m_h.group(1)))
                        count = int(m_h.group(2)) if m_h.group(2) is not None else 1
                        end_ln = start_ln if count == 0 else start_ln + count - 1
                        file_ranges.setdefault(current_file, []).append((start_ln, end_ln))

        # Parse source AST (Python stdlib ast or optional tree-sitter) and intersect with modified diff line ranges
        for mod_file in modified_files:
            if not is_code_file(mod_file):
                continue
            full_path = ws / mod_file
            if not full_path.is_file():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            ranges = file_ranges.get(mod_file)
            if not ranges:
                # Newly created untracked file: entire file is modified
                line_cnt = max(1, len(content.splitlines()))
                ranges = [(1, line_cnt)]
            syms = extract_symbols_for_line_ranges(content, mod_file, ranges)
            if syms:
                file_symbols[mod_file] = syms
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError) as e:
        logger.debug("get_git_ground_truth encountered non-fatal error: %s", e)

    return commit_hash, modified_files, file_symbols, commit_subject


def get_git_diff_patch(
    workspace_root: str,
    target_files: Optional[Set[str]] = None,
) -> Optional[str]:
    """Capture unified git diff of uncommitted changes or HEAD commit, optionally bounded to target_files."""
    ws = Path(workspace_root)
    if not (ws / ".git").exists():
        return None
    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        cmd = (
            ["git", "diff", "HEAD~1", "HEAD"]
            if not status_res.stdout.strip()
            else ["git", "diff", "HEAD"]
        )
        if target_files:
            cmd.extend(["--"] + sorted(target_files))
        diff_res = subprocess.run(
            cmd,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if diff_res.returncode == 0 and diff_res.stdout.strip():
            return scrub_secrets(diff_res.stdout.strip())
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError) as e:
        logger.debug("get_git_diff_patch encountered non-fatal error: %s", e)
    return None


def extract_compact_diff_summary(
    diff_patch: Optional[str], target_file: str, max_lines: int = 4
) -> Optional[str]:
    """Extract a compact single-line summary of added/modified code for target_file from git patch."""
    if not diff_patch or not target_file:
        return None
    in_target = False
    added_lines: List[str] = []
    for raw_line in diff_patch.splitlines():
        if raw_line.startswith("diff --git"):
            in_target = f"a/{target_file}" in raw_line or f"b/{target_file}" in raw_line
            continue
        if in_target:
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                stripped = raw_line[1:].strip()
                if stripped and not stripped.startswith(("#", "//", "/*", "*")):
                    added_lines.append(stripped)
                    if len(added_lines) >= max_lines:
                        break
    if not added_lines:
        return None
    summary = " ; ".join(added_lines[:max_lines])
    if len(summary) > 120:
        summary = summary[:117] + "..."
    return f"+ {summary}"


def assess_playbook_triviality(playbook: TracePlaybook) -> Tuple[bool, str]:
    """
    Default-deny triviality gate for compiled playbooks.
    Returns (is_trivial, reason).
    A playbook is non-trivial ONLY if it contains:
    - verified L2 causal error-to-fix repairs, OR
    - multi-file code modifications (>= 2 code files) backed by verification.
    """
    if playbook.l2_causal_repairs:
        return False, "Contains verified L2 causal error-to-fix repairs"

    code_files = [f for f in playbook.touched_files if is_code_file(f)]
    if not code_files:
        return True, "Does not touch any source code files (documentation/metadata only)"

    has_verify = bool(playbook.l1_verification_cmds) or any(
        s.get("action") == "ENV_EXEC" for s in playbook.l0_critical_path
    )
    if not has_verify:
        return True, "Lacks verified test/build execution command"

    if len(code_files) <= 1:
        return (
            True,
            f"Tautological single-file routine on '{code_files[0]}' without error-fix causality",
        )

    return False, f"Verified multi-file change ({len(code_files)} code files)"


def _extract_cmd_runner_and_targets(cmd: str) -> Tuple[str, Set[str]]:
    """Extract canonical runner family and explicit file/dir/module targets from a command."""
    if not cmd:
        return "", set()
    tokens = cmd.strip().split()
    if not tokens:
        return "", set()

    # Strip common env var assignments (e.g. PYTHONPATH=. pytest ...)
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        idx += 1
    tokens = tokens[idx:]
    if not tokens:
        return "", set()

    exe = Path(tokens[0]).name.lower()
    runner = exe
    arg_start = 1

    if exe in {"python", "python3", "py"}:
        if len(tokens) >= 3 and tokens[1] == "-m":
            mod = tokens[2].lower()
            runner = "unittest" if mod.startswith("unittest") else mod
            arg_start = 3
        else:
            runner = "python"
    elif exe in {"uv", "poetry"} and len(tokens) >= 3 and tokens[1] == "run":
        sub = Path(tokens[2]).name.lower()
        runner = sub
        arg_start = 3
    elif exe in {"npm", "pnpm", "yarn", "cargo", "go", "dotnet"} and len(tokens) >= 2:
        runner = f"{exe}:{tokens[1].lower()}"
        arg_start = 2

    targets: Set[str] = set()
    for tok in tokens[arg_start:]:
        if tok in {"discover", "-v", "--verbose"} or tok.startswith("-"):
            continue
        cleaned = tok.strip("\"'")
        if "/" in cleaned or cleaned.endswith(
            (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".cpp", ".c")
        ):
            # Strip pytest nodeid suffix like ::TestCls::test_fn
            file_part = cleaned.split("::", 1)[0].rstrip("/")
            if file_part:
                targets.add(file_part)
        elif runner == "unittest" and "." in cleaned:
            targets.add(cleaned.replace(".", "/") + ".py")
        elif cleaned in {"tests", "test", "src"}:
            targets.add(cleaned)

    return runner, targets


def _commands_are_same_origin(failed_cmd: str, verify_cmd: str) -> bool:
    """
    Check whether verify_cmd re-tests the same runner and compatible target scope as failed_cmd.
    Rejects unrelated passes (e.g. `test_a.py` fails -> `test_b.py` passes, or linter -> test).
    """
    r_fail, t_fail = _extract_cmd_runner_and_targets(failed_cmd)
    r_ver, t_ver = _extract_cmd_runner_and_targets(verify_cmd)
    if not r_fail or not r_ver or r_fail != r_ver:
        return False
    if not t_fail or not t_ver:
        return True
    # Direct match or parent-directory containment (e.g. `tests/test_a.py` inside `tests`)
    for tf in t_fail:
        for tv in t_ver:
            if (
                tf == tv
                or Path(tf).name == Path(tv).name
                or tf.startswith(tv + "/")
                or tv.startswith(tf + "/")
            ):
                return True
    return False


class BackwardTraceSlicer:
    """Compiles a noisy agent session into a minimal, verifiable TracePlaybook."""

    def __init__(self) -> None:
        self.last_sliced_step_indices: List[int] = []
        self.last_git_diff: Optional[str] = None
        self._last_repair_fix_indices: List[List[int]] = []

    def slice_session(
        self,
        session: ParsedSession,
        use_git_ground_truth: bool = True,
    ) -> Optional[TracePlaybook]:
        raw_actions = session.actions
        if not raw_actions:
            self.last_sliced_step_indices = []
            self.last_git_diff = None
            return None

        # 1. Determine Ground Truth Mutated Files (Sink)
        session_mutated_files = {
            a.target_file
            for a in raw_actions
            if a.kind == ActionKind.MUTATE and a.target_file and not a.target_file.startswith(".tmem/")
        }

        # Query git ground truth bounded to session mutated files if available
        commit_hash, git_files, git_symbols, commit_subject = (
            get_git_ground_truth(session.workspace_root, target_files=session_mutated_files or None)
            if use_git_ground_truth
            else (None, set(), {}, None)
        )

        ws = Path(session.workspace_root)
        if session_mutated_files:
            # Boundary isolation: strictly confine ground truth to files touched in this session!
            if git_files:
                effective_files = session_mutated_files & git_files
                if not effective_files:
                    effective_files = {f for f in session_mutated_files if (ws / f).exists()}
            else:
                effective_files = {f for f in session_mutated_files if (ws / f).exists()}
            if not effective_files:
                effective_files = set(session_mutated_files)
        else:
            effective_files = git_files

        git_diff_patch = (
            get_git_diff_patch(session.workspace_root, target_files=effective_files or None)
            if use_git_ground_truth
            else None
        )
        self.last_git_diff = git_diff_patch

        # 2. Extract Error -> Fix Causal Pairs (L2 Universal Invariants)
        causal_repairs, repair_step_indices = self._extract_causal_repairs_with_indices(
            raw_actions, effective_files, workspace_root=session.workspace_root
        )

        # 3. Backward Slicing: Filter READ, MUTATE, ENV_EXEC into minimal critical path
        read_steps: List[Dict[str, object]] = []
        read_step_indices: List[int] = []
        mutate_steps: List[Dict[str, object]] = []
        mutate_step_indices: List[int] = []
        env_steps: List[Dict[str, object]] = []
        env_step_indices: List[int] = []
        seen_reads: Set[str] = set()
        seen_mutations: Set[str] = set()
        mutate_map: Dict[str, Dict[str, object]] = {}
        verification_cmds: List[str] = []
        touched_symbols: Set[str] = set()

        for sym_list in git_symbols.values():
            touched_symbols.update(sym_list)

        # Module stems of modified files to recognize cross-file reads
        generic_stems = {
            "__init__", "utils", "util", "helpers", "helper", "common",
            "config", "constants", "types", "base", "models", "test", "tests",
        }
        effective_modules = {
            Path(f).stem
            for f in effective_files
            if f and len(Path(f).stem) >= 4 and Path(f).stem not in generic_stems
        }

        candidate_read_files: Set[str] = {
            a.target_file
            for a in raw_actions
            if a.kind == ActionKind.READ and a.target_file
        }
        ast_imported_files: Set[str] = set()
        ws_root = Path(session.workspace_root)
        for mod_file in effective_files:
            if is_code_file(mod_file):
                full_mod = ws_root / mod_file
                if full_mod.exists() and full_mod.is_file():
                    try:
                        mod_text = full_mod.read_text(encoding="utf-8", errors="ignore")
                        ast_imported_files.update(
                            extract_local_imports(mod_text, mod_file, candidate_read_files)
                        )
                    except OSError:
                        pass

        per_file_symbols: Dict[str, List[str]] = {
            f: list(syms) for f, syms in git_symbols.items() if syms
        }

        for action in raw_actions:
            if action.kind == ActionKind.READ and action.target_file:
                target_stem = Path(action.target_file).stem
                is_direct = action.target_file in effective_files
                is_ast_import = action.target_file in ast_imported_files
                is_specific_stem = len(target_stem) >= 4 and target_stem not in generic_stems
                is_dependency = is_specific_stem and (
                    target_stem in effective_modules
                    or any(
                        re.search(rf"\b{re.escape(target_stem)}\b", sym)
                        for sym in touched_symbols
                    )
                )
                if (is_direct or is_ast_import or is_dependency) and action.target_file not in seen_reads:
                    seen_reads.add(action.target_file)
                    read_step_indices.append(action.step_idx)
                    step_entry: Dict[str, object] = {
                        "step": action.step_idx,
                        "action": ActionKind.READ.value,
                        "target": action.target_file,
                    }
                    if action.rationale:
                        step_entry["rationale"] = action.rationale
                    read_steps.append(step_entry)

            elif action.kind == ActionKind.MUTATE and action.target_file:
                # Dead Mutation Elimination: drop mutations to files not in effective_files
                if action.target_file in effective_files:
                    touched_symbols.update(action.symbols_touched)
                    if action.symbols_touched:
                        existing_file_syms = per_file_symbols.setdefault(action.target_file, [])
                        for sym in action.symbols_touched:
                            if sym not in existing_file_syms:
                                existing_file_syms.append(sym)
                    mutate_step_indices.append(action.step_idx)
                    if action.target_file not in seen_mutations:
                        seen_mutations.add(action.target_file)
                        file_syms = sorted(
                            set(action.symbols_touched) | set(git_symbols.get(action.target_file, []))
                        )
                        diff_sum = extract_compact_diff_summary(git_diff_patch, action.target_file)
                        step_entry = {
                            "step": action.step_idx,
                            "action": ActionKind.MUTATE.value,
                            "target": action.target_file,
                        }
                        if file_syms:
                            step_entry["symbols"] = file_syms
                        if action.rationale:
                            step_entry["rationale"] = action.rationale
                        if diff_sum:
                            step_entry["diff_summary"] = diff_sum
                        mutate_steps.append(step_entry)
                        mutate_map[action.target_file] = step_entry
                    else:
                        existing_entry = mutate_map[action.target_file]
                        merged_syms = sorted(
                            set(existing_entry.get("symbols") or [])  # type: ignore[arg-type]
                            | set(action.symbols_touched)
                        )
                        if merged_syms:
                            existing_entry["symbols"] = merged_syms
                        if action.rationale and not existing_entry.get("rationale"):
                            existing_entry["rationale"] = action.rationale

            elif action.kind == ActionKind.ENV_EXEC and action.command:
                # Keep only canonical verification or toolchain install commands (deduplicated)
                if action.exit_code in (0, None) and not action.error_signature:
                    canonical_cmd = self._extract_canonical_verification_cmd(action.command)
                    if canonical_cmd and canonical_cmd not in verification_cmds:
                        verification_cmds.append(canonical_cmd)
                        env_step_indices.append(action.step_idx)
                        step_entry = {
                            "step": action.step_idx,
                            "action": ActionKind.ENV_EXEC.value,
                            "command": canonical_cmd,
                        }
                        if action.rationale:
                            step_entry["rationale"] = action.rationale
                        env_steps.append(step_entry)

        # Keep at most the last 2 canonical verification/toolchain commands in L0 critical path,
        # sorted chronologically by original step index so failure->fix->verify causality is preserved.
        sliced_steps: List[Dict[str, object]] = sorted(
            read_steps + mutate_steps + env_steps[-2:],
            key=lambda s: int(s.get("step") or 0),
        )
        sliced_env_indices = env_step_indices[-2:]

        collected_indices = (
            read_step_indices
            + mutate_step_indices
            + sliced_env_indices
            + repair_step_indices
        )
        self.last_sliced_step_indices = sorted(set(collected_indices))

        if not sliced_steps and not causal_repairs:
            return None

        # 4. Generate <5ms Pre-flight Speculative Guards
        sorted_files = sorted(effective_files)
        guards = self._build_guards(
            session.workspace_root, sorted_files, per_file_symbols
        )

        # 5. Build L1 Co-change Topology (statistical history first; focused session fallback)
        cochange_topology = find_cochange_for_files(
            session.workspace_root, set(sorted_files)
        )
        if not cochange_topology:
            focused_files = [
                f for f in sorted_files if is_source_code_file(f) or is_test_file(f)
            ]
            if 2 <= len(focused_files) <= 5:
                cochange_topology.append(
                    f"Co-edited in session: {' <-> '.join(focused_files)}"
                )

        behavior_parts: List[str] = []
        if read_steps:
            behavior_parts.append(f"read {len(read_steps)} file(s)")
        if mutate_steps:
            behavior_parts.append(f"edited {len(mutate_steps)} file(s)")
        if causal_repairs:
            behavior_parts.append(f"repaired {len(causal_repairs)} error(s)")
        if verification_cmds:
            behavior_parts.append(f"verified (`{verification_cmds[-1]}`)")
        behavior_digest = f"Behavior: {' -> '.join(behavior_parts)}" if behavior_parts else ""

        problem_intent, semantic_summary = self._synthesize_semantic_topic(
            session=session,
            sorted_files=sorted_files,
            sorted_symbols=sorted(touched_symbols),
            commit_subject=commit_subject,
            behavior_digest=behavior_digest,
        )
        if problem_intent == "non_task":
            return None
        error_sigs = sorted(
            {r.error_signature for r in causal_repairs if r.error_signature}
            | set(session.active_errors)
        )

        root_cause = (
            f"Error '{causal_repairs[0].error_signature}' resolved by '{causal_repairs[0].repair_action}'"
            if causal_repairs
            else None
        )

        # Deterministic content-addressed ID
        fingerprint = f"{problem_intent}|{'|'.join(sorted_files)}|{'|'.join(error_sigs)}"
        trace_id = "tr_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:10]

        playbook = TracePlaybook(
            trace_id=trace_id,
            agent_source=session.agent_name,
            scope="project",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            git_commit=commit_hash,
            schema_version=3,
            source_session_ids=[session.session_id] if session.session_id else [],
            problem_intent=problem_intent[:300],
            semantic_summary=semantic_summary[:350],
            error_signatures=error_sigs,
            touched_files=sorted_files,
            touched_symbols=sorted(touched_symbols),
            guards=guards,
            l0_critical_path=sliced_steps,
            l1_cochange_topology=cochange_topology,
            l1_verification_cmds=verification_cmds[-3:],
            l2_causal_repairs=causal_repairs,
            root_cause=root_cause,
            pitfalls=[],
            raw_steps_count=len(raw_actions),
            sliced_steps_count=len(sliced_steps),
        )

        is_trivial, trivial_reason = assess_playbook_triviality(playbook)
        playbook.is_trivial = is_trivial
        if is_trivial:
            playbook.pitfalls.append(f"Trivial trace: {trivial_reason}")

        # Recalibrate confidence based on deterministic evidence:
        # - Base confidence: 0.30
        # - Verified verification command executed successfully: +0.15
        # - Genuine L2 causal repair with matching stack/fix: +0.15
        # - Multi-file co-change or concrete AST symbols touched: +0.10
        # - Single-session cap: maximum 0.70 (trivial capped at 0.40)
        conf = 0.30
        if verification_cmds:
            conf += 0.15
        if causal_repairs:
            conf += 0.15
        if len(sorted_files) > 1 or touched_symbols:
            conf += 0.10

        if is_trivial:
            conf = min(conf, 0.40)
        else:
            conf = min(conf, 0.70)

        playbook.confidence = round(conf, 2)
        return playbook

    slice = slice_session

    def _extract_causal_repairs(
        self,
        actions: List[ActionNode],
        effective_files: Set[str],
        workspace_root: str = "",
    ) -> List[CausalRepairPair]:
        repairs, _ = self._extract_causal_repairs_with_indices(
            actions, effective_files, workspace_root=workspace_root
        )
        return repairs

    def _extract_causal_repairs_with_indices(
        self,
        actions: List[ActionNode],
        effective_files: Set[str],
        workspace_root: str = "",
    ) -> Tuple[List[CausalRepairPair], List[int]]:
        """
        Scan action trajectory within a bounded causal window for pattern:
        [Failed canonical test/build/install or command with error signature] -> [Intervening MUTATE or toolchain install] -> [Succeeded verification ENV_EXEC]
        Subject to rigorous L2 causal gate conditions (rejecting inline scripts, traceback mismatch, and doc/test-only fixes on source errors).
        """
        repairs: List[CausalRepairPair] = []
        step_indices: List[int] = []
        self._last_repair_fix_indices = []
        seen_sigs: Set[str] = set()

        for i, act in enumerate(actions):
            is_failure = act.kind == ActionKind.ENV_EXEC and (
                act.exit_code not in (0, None) or bool(act.error_signature)
            )
            if not is_failure:
                continue

            # Condition ①: Reject inline scripts or heredocs (python -c, node -e, multiline scripts)
            if is_inline_script_command(act.command):
                continue

            failed_canonical = self._extract_canonical_verification_cmd(act.command or "")
            if not failed_canonical:
                continue
            failed_cmd = failed_canonical

            err_sig = act.error_signature
            if not err_sig or not is_high_entropy_error_signature(err_sig):
                continue
            if err_sig in seen_sigs:
                continue

            intervening_fixes: List[str] = []
            intervening_indices: List[int] = []
            intervening_mutated_files: Set[str] = set()
            is_env = False
            verified_cmd = ""
            verify_idx: Optional[int] = None

            for j in range(i + 1, min(len(actions), i + 36)):
                nxt = actions[j]
                if nxt.kind == ActionKind.MUTATE and nxt.target_file in effective_files:
                    intervening_mutated_files.add(nxt.target_file)
                    sym_note = f" ({', '.join(nxt.symbols_touched[:3])})" if nxt.symbols_touched else ""
                    fix_desc = f"edit {nxt.target_file}{sym_note}"
                    if fix_desc not in intervening_fixes:
                        intervening_fixes.append(fix_desc)
                        intervening_indices.append(nxt.step_idx)
                elif nxt.kind == ActionKind.ENV_EXEC and nxt.command:
                    install_clause = self._extract_toolchain_install_clause(nxt.command)
                    if install_clause and nxt.exit_code in (0, None):
                        fix_desc = f"run `{install_clause}`"
                        if fix_desc not in intervening_fixes:
                            intervening_fixes.append(fix_desc)
                            intervening_indices.append(nxt.step_idx)
                        is_env = True
                    elif nxt.exit_code == 0 and not nxt.error_signature and intervening_fixes:
                        cand_verify = self._extract_canonical_verification_cmd(nxt.command)
                        if cand_verify and _commands_are_same_origin(failed_cmd, cand_verify):
                            verified_cmd = cand_verify
                            verify_idx = nxt.step_idx
                            break

            # Quality gate on causal repair pairs:
            # Must have non-empty fixes and verified command.
            if intervening_fixes and verified_cmd:
                if not is_env:
                    # Reject if intervening edits only modified documentation files
                    if all(is_doc_file(f) for f in intervening_mutated_files):
                        continue

                    # Condition ②: Traceback files vs intervening mutated files intersection check
                    tb_files = extract_traceback_files(
                        act.raw_output or act.output_snippet, workspace_root
                    )
                    if tb_files and not (tb_files & intervening_mutated_files):
                        continue

                    # Condition ③: Error comes from source code, but fix only touched tests or docs
                    if any(is_source_code_file(f) for f in tb_files):
                        if all(
                            is_test_file(f) or is_doc_file(f) for f in intervening_mutated_files
                        ):
                            continue

                    has_concrete_target = any(
                        "(" in fix or len(effective_files) > 1 for fix in intervening_fixes
                    )
                    if not has_concrete_target:
                        continue

                seen_sigs.add(err_sig)
                repairs.append(
                    CausalRepairPair(
                        error_signature=err_sig,
                        failed_command=failed_cmd,
                        repair_action=" -> ".join(intervening_fixes[:3]),
                        verification_cmd=verified_cmd,
                        is_env_or_toolchain=is_env,
                    )
                )
                self._last_repair_fix_indices.append(list(intervening_indices))
                step_indices.append(act.step_idx)
                step_indices.extend(intervening_indices)
                if verify_idx is not None:
                    step_indices.append(verify_idx)
        return repairs, step_indices

    def build_raw_trace(
        self,
        session: ParsedSession,
        playbook: TracePlaybook,
        diff_patch: Optional[str] = None,
    ) -> RawExecutionTrace:
        """Construct full-fidelity RawExecutionTrace from a parsed session and its compiled playbook."""
        patch = diff_patch or self.last_git_diff
        return RawExecutionTrace(
            trace_id=playbook.trace_id,
            session_id=session.session_id,
            agent_source=session.agent_name,
            workspace_root=session.workspace_root,
            created_at=playbook.created_at,
            git_commit=playbook.git_commit,
            user_intents=list(session.user_intents),
            assistant_conclusions=list(session.assistant_conclusions),
            total_steps=len(session.actions),
            actions=[a.to_dict() for a in session.actions],
            sliced_step_indices=list(self.last_sliced_step_indices),
            diff_patch=patch,
            metadata={
                "raw_steps_count": len(session.actions),
                "sliced_steps_count": len(playbook.l0_critical_path),
                "causal_repairs_count": len(playbook.l2_causal_repairs),
            },
        )

    def _build_guards(
        self,
        workspace_root: str,
        files: List[str],
        per_file_symbols: Dict[str, List[str]],
    ) -> List[GuardProbe]:
        guards: List[GuardProbe] = []
        ws = Path(workspace_root)
        ordered_files = sorted(
            files,
            key=lambda f: (
                0 if is_source_code_file(f) else 1 if is_test_file(f) else 2,
                f,
            ),
        )[:8]
        for f in ordered_files:
            guards.append(GuardProbe(probe_type="file_exists", target=f))
            if not is_code_file(f):
                continue
            cand_syms = per_file_symbols.get(f, [])
            if not cand_syms:
                continue
            full_path = ws / f
            if full_path.exists() and full_path.is_file():
                try:
                    text = full_path.read_text(encoding="utf-8", errors="ignore")
                    added_for_file = 0
                    for sym in cand_syms:
                        if check_symbol_in_ast(text, f, sym):
                            guards.append(
                                GuardProbe(
                                    probe_type="symbol_present",
                                    target=f,
                                    expected=sym,
                                )
                            )
                            added_for_file += 1
                            if added_for_file >= 3:
                                break
                except Exception:
                    pass
        return guards

    def _synthesize_semantic_topic(
        self,
        session: ParsedSession,
        sorted_files: List[str],
        sorted_symbols: List[str],
        commit_subject: Optional[str],
        behavior_digest: str = "",
    ) -> Tuple[str, str]:
        """
        Synthesize both:
        1. `problem_intent` (Semantic Topic): High-signal task title combining the most informative
           user goal and/or Git commit subject rather than raw short conversational replies.
        2. `semantic_summary` (Semantic Resolution Summary): Concise explanation of what was solved,
           combining the assistant's final conclusion and key touched modules/symbols.
        """
        # Pick the most informative user intent from the session, filtering conversational filler
        clean_intents = [
            u.strip()
            for u in session.user_intents
            if not is_filler_user_intent(u) and not u.strip().startswith(("/", "<tmem"))
        ]
        raw_user = ""
        if clean_intents:
            if len(clean_intents) == 1:
                raw_user = clean_intents[0]
            else:
                first_u = clean_intents[0]
                last_u = clean_intents[-1]
                if len(first_u) >= 20 and len(last_u) >= 20 and first_u != last_u:
                    raw_user = f"{first_u} — {last_u}"
                else:
                    raw_user = max(clean_intents, key=len)
        elif session.user_intents:
            raw_user = session.user_intents[-1].strip()

        # Quality Gate 0.4: Detect meta-request (evaluation, review, generic optimize project)
        # and mark as non_task so it does not pollute project memory.
        if is_meta_non_task_intent(raw_user) or any(
            is_meta_non_task_intent(u) for u in clean_intents
        ):
            return "non_task", "non_task"

        if raw_user:
            problem_intent = raw_user
        elif commit_subject:
            problem_intent = commit_subject
        else:
            problem_intent = f"Modify {', '.join(sorted_files[:3])}"

        # Build semantic resolution summary from assistant conclusion + commit subject + scope
        summary_parts: List[str] = []
        if commit_subject and commit_subject != problem_intent:
            summary_parts.append(commit_subject)
        if session.assistant_conclusions:
            summary_parts.append(session.assistant_conclusions[-1])
        if sorted_symbols:
            summary_parts.append(f"Key symbols: {', '.join(sorted_symbols[:6])}")
        elif sorted_files:
            summary_parts.append(f"Key modules: {', '.join(sorted_files[:4])}")
        if behavior_digest:
            summary_parts.append(behavior_digest)

        semantic_summary = " | ".join(summary_parts)
        return problem_intent, semantic_summary

    def _extract_toolchain_install_clause(self, cmd: str) -> Optional[str]:
        """Extract clean package/toolchain installation sub-clause from a command string."""
        if not cmd:
            return None
        for raw_part in re.split(r"&&|\n|;", cmd):
            part = raw_part.strip()
            if not part or "tmem.cli" in part:
                continue
            if TOOLCHAIN_INSTALL_RE.search(part):
                return part
        return None

    def _extract_canonical_verification_cmd(self, cmd: str) -> Optional[str]:
        """
        Extract only genuine, repeatable verification/test/build/install sub-clauses from a shell command,
        stripping chained exploratory commands (`ls`, `find`, `cat`), inline one-liners (`python3 -c`, `node -e`),
        git commit/push plumbing, and `tmem.cli` self-invocations.
        """
        if not cmd:
            return None
        stripped = cmd.strip()
        if any(
            k in stripped
            for k in ("for pkg in ", "which ")
        ):
            return None

        kept_clauses: List[str] = []
        for raw_part in re.split(r"&&|\n|;", stripped):
            part = raw_part.strip()
            if not part:
                continue
            # Strip trailing pipe to grep/head/tail for config dumps
            base_part = re.split(r"\|\s*(?:grep|head|tail|cat)\b", part)[0].strip()
            if not base_part:
                continue
            if "tmem.cli" in base_part:
                continue
            if EXPLORATORY_CLAUSE_RE.match(base_part):
                continue
            if re.match(r"^(?:python\d*(?:\.\d+)?\s+-c\b|node\s+-e\b)", base_part):
                continue
            # Reject lines with code assignment syntax or python/js statement keywords
            if re.search(r"\s+=\s+", base_part) or re.match(
                r"^(?:[A-Za-z_]\w*\s*=\s*|print\(|def\s+|class\s+|import\s+|from\s+)",
                base_part,
            ):
                continue
            if (
                VERIFY_WHITELIST_RE.search(base_part)
                or TOOLCHAIN_INSTALL_RE.search(base_part)
                or RE_TEST_SCRIPT_CMD.match(base_part)
            ):
                if base_part not in kept_clauses:
                    kept_clauses.append(base_part)

        if not kept_clauses:
            return None
        return " && ".join(kept_clauses)
