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
    infer_modified_symbols,
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

ENV_ASSIGN_PREFIX_RE = re.compile(
    r"^(?:sudo\s+)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+"
)
SWALLOW_EXIT_RE = re.compile(r"(?:\|\||;)\s*(?:true|:)\s*$")
SCRATCH_FILE_RE = re.compile(
    r"(?:^|/)(?:repro|reproduce|simulate|scratch|debug|tmp|temp)(?:[_-][^/]*)?\.py$|(?:^|/)(?:scratch|tmp)/",
    re.IGNORECASE,
)
ROOT_SCRIPT_DRIVER_RE = re.compile(
    r"^(?:test(?:[_-].*)?|repro.*|simulate.*|check[_-].*|verify[_-].*)\.py$",
    re.IGNORECASE,
)


def _normalize_rel_path(path_str: str) -> str:
    cleaned = path_str.strip().strip("\"'")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _split_shell_clauses(cmd: str) -> List[str]:
    """Split shell command on unquoted `&&`, `;`, or newlines while preserving quoted strings."""
    clauses: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch in (";", "\n"):
                part = "".join(buf).strip()
                if part:
                    clauses.append(part)
                buf.clear()
                i += 1
                continue
            if ch == "&" and i + 1 < n and cmd[i + 1] == "&":
                part = "".join(buf).strip()
                if part:
                    clauses.append(part)
                buf.clear()
                i += 2
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        clauses.append(tail)
    return clauses


def _extract_killed_targets_from_command(cmd: str, known_files: Set[str]) -> Set[str]:
    """Identify file paths destroyed or reverted by rm/unlink/git checkout|restore|reset."""
    if not cmd:
        return set()
    killed: Set[str] = set()
    norm_known = {_normalize_rel_path(f): f for f in known_files if f}

    for part in _split_shell_clauses(cmd):
        if not part:
            continue
        try:
            tokens = part.split()
        except Exception:
            continue
        if not tokens:
            continue

        idx = 0
        if tokens[0] == "sudo":
            idx += 1
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
            idx += 1
        tokens = tokens[idx:]
        if not tokens:
            continue

        head = Path(tokens[0]).name
        candidates: List[str] = []

        if head in {"rm", "unlink"}:
            after_sep = False
            for tok in tokens[1:]:
                if tok == "--":
                    after_sep = True
                    continue
                if not after_sep and tok.startswith("-"):
                    continue
                candidates.append(tok)
        elif head == "git" and len(tokens) >= 2 and tokens[1] in {"checkout", "restore", "reset"}:
            sub = tokens[1]
            # Avoid matching `git checkout -b ...` branch creation
            if sub == "checkout" and any(t in {"-b", "-B", "--orphan"} for t in tokens[2:]):
                continue
            after_sep = "--" in tokens[2:]
            seen_sep = False
            for tok in tokens[2:]:
                if tok == "--":
                    seen_sep = True
                    continue
                if after_sep and not seen_sep:
                    continue
                if tok.startswith("-"):
                    continue
                if sub == "reset" and tok in {"HEAD", "HEAD~1", "ORIG_HEAD"} and not after_sep:
                    continue
                candidates.append(tok)

        for cand in candidates:
            nc = _normalize_rel_path(cand)
            if not nc:
                continue
            if nc == "." and head == "git":
                killed.update(known_files)
                continue
            if nc in norm_known:
                killed.add(norm_known[nc])
                continue
            for nk, orig in norm_known.items():
                if nk.startswith(nc + "/"):
                    killed.add(orig)
    return killed


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

    def _identify_scratch_files(
        self,
        raw_actions: List[ActionNode],
        candidate_files: Set[str],
        importer_map: Dict[str, Set[str]],
    ) -> Set[str]:
        """Identify temporary reproduction/scratch scripts created alongside permanent repo files."""
        if len(candidate_files) <= 1:
            return set()

        has_permanent_repo_file = any(
            "/" in f
            and not SCRATCH_FILE_RE.search(f)
            and (is_source_code_file(f) or is_test_file(f))
            for f in candidate_files
        )

        first_read: Dict[str, int] = {}
        first_mutate: Dict[str, int] = {}
        executed_scripts: Set[str] = set()

        for act in raw_actions:
            if act.kind == ActionKind.READ and act.target_file:
                first_read.setdefault(act.target_file, act.step_idx)
            elif (
                act.kind == ActionKind.MUTATE
                and act.target_file
                and act.exit_code in (0, None)
                and not act.error_signature
            ):
                first_mutate.setdefault(act.target_file, act.step_idx)
            elif act.kind == ActionKind.ENV_EXEC and act.command:
                _, cmd_targets = _extract_cmd_runner_and_targets(act.command)
                executed_scripts.update(cmd_targets)

        scratch: Set[str] = set()
        for f in candidate_files:
            if importer_map.get(f):
                continue
            if SCRATCH_FILE_RE.search(f):
                scratch.add(f)
                continue
            if (
                has_permanent_repo_file
                and "/" not in f
                and (f not in first_read or first_read[f] > first_mutate.get(f, 10**9))
                and (bool(ROOT_SCRIPT_DRIVER_RE.match(f)) or f in executed_scripts)
            ):
                scratch.add(f)

        # Never prune every file if the entire session only touched scratch files
        if len(scratch) >= len(candidate_files):
            return set()
        return scratch

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

        # 0. Pre-scan destroy/revert barriers and valid mutations for Temporal Causality
        known_files: Set[str] = {
            a.target_file
            for a in raw_actions
            if a.target_file and not a.target_file.startswith(".tmem/")
        }
        last_kill_step: Dict[str, int] = {}
        last_valid_mutate_step: Dict[str, int] = {}

        for act in raw_actions:
            if (
                act.kind == ActionKind.MUTATE
                and act.target_file
                and not act.target_file.startswith(".tmem/")
                and act.exit_code in (0, None)
                and not act.error_signature
            ):
                prev_m = last_valid_mutate_step.get(act.target_file, -1)
                if act.step_idx > prev_m:
                    last_valid_mutate_step[act.target_file] = act.step_idx
            elif act.kind == ActionKind.ENV_EXEC and act.command and act.exit_code in (0, None):
                for kf in _extract_killed_targets_from_command(act.command, known_files):
                    if act.step_idx > last_kill_step.get(kf, -1):
                        last_kill_step[kf] = act.step_idx

        # 1. Determine Ground Truth Mutated Files (Surviving Sink)
        session_mutated_files = {
            f
            for f, m_step in last_valid_mutate_step.items()
            if m_step > last_kill_step.get(f, -1)
        }
        killed_files: Set[str] = {
            f
            for f, k_step in last_kill_step.items()
            if k_step >= last_valid_mutate_step.get(f, -1)
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
            effective_files = set(git_files)

        candidate_read_files: Set[str] = {
            a.target_file
            for a in raw_actions
            if a.kind == ActionKind.READ and a.target_file
        }
        known_import_targets = candidate_read_files | effective_files
        ast_imported_files: Set[str] = set()
        importer_map: Dict[str, Set[str]] = {}
        ws_root = Path(session.workspace_root)
        for mod_file in effective_files:
            if is_code_file(mod_file):
                full_mod = ws_root / mod_file
                if full_mod.exists() and full_mod.is_file():
                    try:
                        mod_text = full_mod.read_text(encoding="utf-8", errors="ignore")
                        imported = extract_local_imports(mod_text, mod_file, known_import_targets)
                        ast_imported_files.update(imported)
                        for dep in imported:
                            importer_map.setdefault(dep, set()).add(mod_file)
                    except OSError:
                        pass

        # Prune standalone scratch/reproduction scripts when not tracked by git ground truth
        scratch_files: Set[str] = set()
        if not git_files and effective_files:
            scratch_files = self._identify_scratch_files(
                raw_actions, effective_files, importer_map
            )
            if scratch_files:
                effective_files = effective_files - scratch_files

        dead_target_files = killed_files | scratch_files
        effective_last_mutate_step: Dict[str, int] = {
            f: last_valid_mutate_step[f]
            for f in effective_files
            if f in last_valid_mutate_step
        }
        max_effective_mutate_step = max(effective_last_mutate_step.values(), default=-1)

        git_diff_patch = (
            get_git_diff_patch(session.workspace_root, target_files=effective_files or None)
            if use_git_ground_truth
            else None
        )
        self.last_git_diff = git_diff_patch

        per_file_symbols: Dict[str, List[str]] = {
            f: list(syms) for f, syms in git_symbols.items() if syms
        }

        # 2. Extract Error -> Fix Causal Pairs (L2 Universal Invariants)
        causal_repairs, repair_step_indices = self._extract_causal_repairs_with_indices(
            raw_actions,
            effective_files,
            workspace_root=session.workspace_root,
            dead_files=dead_target_files,
            last_kill_step=last_kill_step,
            file_symbols=per_file_symbols,
        )

        # 3. Backward Slicing: Filter READ, MUTATE, ENV_EXEC into minimal critical path
        read_steps: List[Dict[str, object]] = []
        read_step_indices: List[int] = []
        mutate_steps: List[Dict[str, object]] = []
        mutate_step_indices: List[int] = []
        seen_reads: Set[str] = set()
        seen_mutations: Set[str] = set()
        mutate_map: Dict[str, Dict[str, object]] = {}
        touched_symbols: Set[str] = set()

        for sym_list in git_symbols.values():
            touched_symbols.update(sym_list)

        # Module stems of modified files to recognize cross-file test/impl reads
        generic_stems = {
            "__init__", "utils", "util", "helpers", "helper", "common",
            "config", "constants", "types", "base", "models", "test", "tests",
        }
        effective_modules = {
            Path(f).stem
            for f in effective_files
            if f and len(Path(f).stem) >= 4 and Path(f).stem not in generic_stems
        }

        consumer_last_mutate: Dict[str, int] = {
            dep: max(
                (effective_last_mutate_step[c] for c in importers if c in effective_last_mutate_step),
                default=-1,
            )
            for dep, importers in importer_map.items()
        }

        # Traceback dependencies linked to failing runs followed by effective mutations
        traceback_dep_last_mutate: Dict[str, int] = {}
        for act in raw_actions:
            if act.kind == ActionKind.ENV_EXEC and (
                act.exit_code not in (0, None) or bool(act.error_signature)
            ):
                later_mutate = max(
                    (m for m in effective_last_mutate_step.values() if m > act.step_idx),
                    default=-1,
                )
                if later_mutate >= 0:
                    for tf in extract_traceback_files(
                        act.raw_output or act.output_snippet, session.workspace_root
                    ):
                        if later_mutate > traceback_dep_last_mutate.get(tf, -1):
                            traceback_dep_last_mutate[tf] = later_mutate

        env_install_entries: List[Tuple[int, str, Dict[str, object]]] = []
        install_cmds_seen: Set[str] = set()
        latest_verify_by_cmd: Dict[str, Tuple[int, Dict[str, object]]] = {}
        fallback_verify_by_cmd: Dict[str, Tuple[int, Dict[str, object]]] = {}

        for action in raw_actions:
            if action.kind == ActionKind.READ and action.target_file:
                if action.step_idx <= last_kill_step.get(action.target_file, -1):
                    continue
                target_stem = Path(action.target_file).stem
                is_direct = (
                    action.target_file in effective_files
                    and action.step_idx <= effective_last_mutate_step.get(action.target_file, -1)
                )
                is_ast_import = (
                    action.target_file in ast_imported_files
                    and action.step_idx <= consumer_last_mutate.get(action.target_file, -1)
                )
                is_traceback_dep = (
                    action.target_file in traceback_dep_last_mutate
                    and action.step_idx <= traceback_dep_last_mutate[action.target_file]
                )
                is_specific_stem = len(target_stem) >= 4 and target_stem not in generic_stems
                is_module_dep = (
                    is_specific_stem
                    and action.target_file not in dead_target_files
                    and action.step_idx <= max_effective_mutate_step
                    and (
                        target_stem in effective_modules
                        or any(
                            target_stem == f"test_{m}" or m == f"test_{target_stem}"
                            for m in effective_modules
                        )
                    )
                )
                if (
                    (is_direct or is_ast_import or is_traceback_dep or is_module_dep)
                    and action.target_file not in seen_reads
                ):
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
                # Filter failed mutations and pre-kill/dead-file mutations
                if action.exit_code not in (0, None) or action.error_signature:
                    continue
                if action.step_idx <= last_kill_step.get(action.target_file, -1):
                    continue
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
                if action.exit_code in (0, None) and not action.error_signature:
                    canonical_cmd = self._extract_canonical_verification_cmd(action.command)
                    if not canonical_cmd:
                        continue
                    _, cmd_targets = _extract_cmd_runner_and_targets(canonical_cmd)
                    is_dead_target_cmd = bool(
                        cmd_targets
                        and all(
                            t in dead_target_files or action.step_idx <= last_kill_step.get(t, -1)
                            for t in cmd_targets
                        )
                    )

                    # Classify chained clauses: an install-only command (e.g. `pip install redis pytest`)
                    # must NOT be misclassified as VERIFY_GATE just because a package is named pytest/mypy.
                    clauses = [
                        ENV_ASSIGN_PREFIX_RE.sub("", c.strip()).strip()
                        for c in canonical_cmd.split("&&")
                        if c.strip()
                    ]
                    all_install = bool(clauses) and all(
                        TOOLCHAIN_INSTALL_RE.match(c) for c in clauses
                    )

                    step_entry = {
                        "step": action.step_idx,
                        "action": ActionKind.ENV_EXEC.value,
                        "command": canonical_cmd,
                    }
                    if action.rationale:
                        step_entry["rationale"] = action.rationale

                    if all_install:
                        if canonical_cmd not in install_cmds_seen:
                            install_cmds_seen.add(canonical_cmd)
                            env_install_entries.append((action.step_idx, canonical_cmd, step_entry))
                    elif is_dead_target_cmd:
                        # Keep post-edit verification on repro/temp scripts ONLY as fallback
                        # if permanent edits survive and no permanent repo verification gate exists.
                        earliest_kill = min(
                            (last_kill_step.get(t, 10**9) for t in cmd_targets),
                            default=10**9,
                        )
                        if effective_files and action.step_idx < earliest_kill:
                            if canonical_cmd in fallback_verify_by_cmd:
                                del fallback_verify_by_cmd[canonical_cmd]
                            fallback_verify_by_cmd[canonical_cmd] = (action.step_idx, step_entry)
                    else:
                        # Update latest occurrence so final post-edit verification supersedes stale baseline runs
                        if canonical_cmd in latest_verify_by_cmd:
                            del latest_verify_by_cmd[canonical_cmd]
                        latest_verify_by_cmd[canonical_cmd] = (action.step_idx, step_entry)

        # Select persistent ENV_INSTALL steps (before final active step) and final-epoch VERIFY_GATE steps
        effective_verify_map = latest_verify_by_cmd or fallback_verify_by_cmd
        all_verify_items = [
            (idx, cmd, entry) for cmd, (idx, entry) in effective_verify_map.items()
        ]
        post_edit_verify = [
            item for item in all_verify_items if item[0] >= max_effective_mutate_step
        ]
        chosen_verify_items = (post_edit_verify if post_edit_verify else all_verify_items)[-2:]

        last_active_step = max(
            [max_effective_mutate_step] + [item[0] for item in chosen_verify_items],
            default=-1,
        )
        chosen_install_items = [
            item for item in env_install_entries if last_active_step < 0 or item[0] <= last_active_step
        ][:3]

        chosen_env_items = chosen_install_items + chosen_verify_items
        env_steps = [item[2] for item in chosen_env_items]
        sliced_env_indices = [item[0] for item in chosen_env_items]
        verification_cmds: List[str] = [item[1] for item in env_install_entries] + [
            item[1] for item in all_verify_items
        ]

        # Sort chronologically by original step index so install -> read -> mutate -> verify causality holds
        sliced_steps: List[Dict[str, object]] = sorted(
            read_steps + mutate_steps + env_steps,
            key=lambda s: int(s.get("step") or 0),
        )

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
        dead_files: Optional[Set[str]] = None,
        last_kill_step: Optional[Dict[str, int]] = None,
        file_symbols: Optional[Dict[str, List[str]]] = None,
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
        dead_set = dead_files or set()
        kill_map = last_kill_step or {}
        sym_map = file_symbols or {}

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
            _, fail_targets = _extract_cmd_runner_and_targets(failed_canonical)
            if fail_targets and all(
                t in dead_set or act.step_idx <= kill_map.get(t, -1) for t in fail_targets
            ):
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
                if (
                    nxt.kind == ActionKind.MUTATE
                    and nxt.target_file in effective_files
                    and nxt.exit_code in (0, None)
                    and not nxt.error_signature
                    and nxt.step_idx > kill_map.get(nxt.target_file, -1)
                ):
                    intervening_mutated_files.add(nxt.target_file)
                    node_syms = list(nxt.symbols_touched)
                    if not node_syms and nxt.target_file:
                        if sym_map.get(nxt.target_file):
                            node_syms = list(sym_map[nxt.target_file])
                        elif workspace_root and nxt.raw_input:
                            raw_in = nxt.raw_input
                            snip = str(
                                raw_in.get("content")
                                or raw_in.get("new_string")
                                or raw_in.get("new_str")
                                or raw_in.get("file_text")
                                or raw_in.get("replacement")
                                or ""
                            )
                            if snip:
                                node_syms = infer_modified_symbols(
                                    workspace_root, nxt.target_file, new_snippet=snip
                                )
                    sym_note = f" ({', '.join(node_syms[:3])})" if node_syms else ""
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
                has_symbol_or_multifile = True
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

                    has_symbol_or_multifile = any(
                        "(" in fix or len(effective_files) > 1 for fix in intervening_fixes
                    )
                    has_source_fix = any(
                        is_source_code_file(f) for f in intervening_mutated_files
                    )
                    if not (has_symbol_or_multifile or has_source_fix):
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
                if has_symbol_or_multifile or is_env:
                    step_indices.append(act.step_idx)
                    step_indices.extend(intervening_indices)
                    if verify_idx is not None:
                        step_indices.append(verify_idx)
                else:
                    step_indices.extend(intervening_indices)
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
        for part in _split_shell_clauses(cmd):
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
        if "for pkg in " in stripped:
            return None

        kept_clauses: List[str] = []
        for part in _split_shell_clauses(stripped):
            if not part:
                continue
            # Reject non-gating commands that explicitly swallow failures (e.g. `cmd || true`)
            if SWALLOW_EXIT_RE.search(part):
                continue
            # Strip trailing pipe to grep/head/tail for config dumps
            base_part = re.split(r"\|\s*(?:grep|head|tail|cat)\b", part)[0].strip()
            if not base_part:
                continue
            if "tmem.cli" in base_part:
                continue
            if EXPLORATORY_CLAUSE_RE.match(base_part):
                continue
            # Strip leading POSIX env-var prefixes (e.g. `PYTHONPATH=. python3 ...`) before syntax & inline checks
            cmd_for_syntax = ENV_ASSIGN_PREFIX_RE.sub("", base_part).strip()
            if not cmd_for_syntax:
                continue
            if is_inline_script_command(cmd_for_syntax):
                continue
            # Reject lines with code assignment syntax or python/js statement keywords
            if re.search(r"\s+=\s+", cmd_for_syntax) or re.match(
                r"^(?:[A-Za-z_]\w*\s*=\s*|print\(|def\s+|class\s+|import\s+|from\s+)",
                cmd_for_syntax,
            ):
                continue
            if (
                VERIFY_WHITELIST_RE.search(cmd_for_syntax)
                or TOOLCHAIN_INSTALL_RE.search(cmd_for_syntax)
                or RE_TEST_SCRIPT_CMD.match(base_part)
            ):
                if base_part not in kept_clauses:
                    kept_clauses.append(base_part)

        if not kept_clauses:
            return None
        return " && ".join(kept_clauses)
