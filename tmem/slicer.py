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

from tmem.adapters.base import ParsedSession, extract_symbols_from_text, is_code_file
from tmem.models import (
    ActionKind,
    ActionNode,
    CausalRepairPair,
    GuardProbe,
    TracePlaybook,
)

TOOLCHAIN_CMD_RE = re.compile(
    r"\b(pip|pip3|uv|poetry|npm|pnpm|yarn|bun|cargo|go\s+get|go\s+mod|apt|apt-get|brew|docker|export|sysctl)\b"
)


def get_git_ground_truth(
    workspace_root: str,
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
        diff_target = ["git", "diff", "HEAD"]
        if status_res.returncode == 0 and status_res.stdout.strip():
            for line in status_res.stdout.splitlines():
                if len(line) > 3:
                    path_str = line[3:].strip().strip('"')
                    if " -> " in path_str:
                        path_str = path_str.split(" -> ")[-1]
                    if not path_str.startswith(".tmem/"):
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
                        modified_files.add(p)

        diff_res = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD"] if not status_res.stdout.strip() else ["git", "diff", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if diff_res.returncode == 0 and diff_res.stdout:
            current_file = None
            for line in diff_res.stdout.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                elif current_file and (line.startswith("+") or line.startswith("@@")):
                    syms = extract_symbols_from_text(line, current_file)
                    if syms:
                        existing = set(file_symbols.get(current_file, []))
                        existing.update(syms)
                        file_symbols[current_file] = sorted(existing)
    except Exception:
        pass

    return commit_hash, modified_files, file_symbols, commit_subject


class BackwardTraceSlicer:
    """Compiles a noisy agent session into a minimal, verifiable TracePlaybook."""

    def slice_session(
        self,
        session: ParsedSession,
        use_git_ground_truth: bool = True,
    ) -> Optional[TracePlaybook]:
        raw_actions = session.actions
        if not raw_actions:
            return None

        commit_hash, git_files, git_symbols, commit_subject = (
            get_git_ground_truth(session.workspace_root)
            if use_git_ground_truth
            else (None, set(), {}, None)
        )

        # 1. Determine Ground Truth Mutated Files (Sink)
        session_mutated_files = {
            a.target_file
            for a in raw_actions
            if a.kind == ActionKind.MUTATE and a.target_file and not a.target_file.startswith(".tmem/")
        }
        if git_files:
            # Intersect if session touched a subset, or union if git diff is authoritative
            effective_files = (session_mutated_files & git_files) or git_files
        else:
            # Verify file still exists on disk (eliminate temporary scratch files that were deleted)
            ws = Path(session.workspace_root)
            effective_files = {
                f for f in session_mutated_files if f and (ws / f).exists()
            }

        # 2. Extract Error -> Fix Causal Pairs (L2 Universal Invariants)
        causal_repairs = self._extract_causal_repairs(raw_actions, effective_files)

        # 3. Backward Slicing: Filter READ, MUTATE, ENV_EXEC into minimal critical path
        sliced_steps: List[Dict[str, object]] = []
        seen_reads: Set[str] = set()
        seen_mutations: Set[str] = set()
        verification_cmds: List[str] = []
        touched_symbols: Set[str] = set()

        for sym_list in git_symbols.values():
            touched_symbols.update(sym_list)

        for action in raw_actions:
            if action.kind == ActionKind.READ and action.target_file:
                # Read Shrinking: only keep first READ of a file that is in effective_files
                if action.target_file in effective_files and action.target_file not in seen_reads:
                    seen_reads.add(action.target_file)
                    sliced_steps.append(
                        {
                            "action": ActionKind.READ.value,
                            "target": action.target_file,
                        }
                    )

            elif action.kind == ActionKind.MUTATE and action.target_file:
                # Dead Mutation Elimination: drop mutations to files not in effective_files
                if action.target_file in effective_files:
                    touched_symbols.update(action.symbols_touched)
                    if action.target_file not in seen_mutations:
                        seen_mutations.add(action.target_file)
                        file_syms = sorted(
                            set(action.symbols_touched) | set(git_symbols.get(action.target_file, []))
                        )
                        step_entry: Dict[str, object] = {
                            "action": ActionKind.MUTATE.value,
                            "target": action.target_file,
                        }
                        if file_syms:
                            step_entry["symbols"] = file_syms
                        sliced_steps.append(step_entry)

            elif action.kind == ActionKind.ENV_EXEC and action.command:
                # Keep successful verification or environment installation commands
                if action.exit_code in (0, None) and not action.error_signature:
                    cmd = action.command.strip()
                    if self._is_meaningful_command(cmd):
                        if cmd not in verification_cmds:
                            verification_cmds.append(cmd)
                        sliced_steps.append(
                            {
                                "action": ActionKind.ENV_EXEC.value,
                                "command": cmd,
                            }
                        )

        if not sliced_steps and not causal_repairs:
            return None

        # 4. Generate <5ms Pre-flight Speculative Guards
        guards = self._build_guards(
            session.workspace_root, sorted(effective_files), sorted(touched_symbols)
        )

        # 5. Build L1 Co-change Topology
        cochange_topology = []
        sorted_files = sorted(effective_files)
        if len(sorted_files) > 1:
            cochange_topology.append(
                f"Co-change contract: modifying {sorted_files[0]} requires synchronized updates to {', '.join(sorted_files[1:])}"
            )
        elif len(sorted_files) == 1:
            cochange_topology.append(f"Primary target module: {sorted_files[0]}")

        problem_intent, semantic_summary = self._synthesize_semantic_topic(
            session=session,
            sorted_files=sorted_files,
            sorted_symbols=sorted(touched_symbols),
            commit_subject=commit_subject,
        )
        error_sigs = sorted(
            {r.error_signature for r in causal_repairs if r.error_signature}
            | set(session.active_errors)
        )

        # Deterministic content-addressed ID
        fingerprint = f"{problem_intent}|{'|'.join(sorted_files)}|{'|'.join(error_sigs)}"
        trace_id = "tr_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:10]

        return TracePlaybook(
            trace_id=trace_id,
            agent_source=session.agent_name,
            scope="project",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            git_commit=commit_hash,
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
            raw_steps_count=len(raw_actions),
            sliced_steps_count=len(sliced_steps),
        )

    def _extract_causal_repairs(
        self, actions: List[ActionNode], effective_files: Set[str]
    ) -> List[CausalRepairPair]:
        """
        Scan action trajectory for pattern:
        [Failed ENV_EXEC with error_signature] -> [Intervening MUTATE or toolchain ENV_EXEC] -> [Succeeded ENV_EXEC]
        """
        repairs: List[CausalRepairPair] = []
        seen_sigs: Set[str] = set()

        for i, act in enumerate(actions):
            if act.kind == ActionKind.ENV_EXEC and (act.exit_code not in (0, None) or act.error_signature):
                err_sig = act.error_signature or f"Command failed (exit {act.exit_code}): {act.command}"
                if err_sig in seen_sigs:
                    continue

                intervening_fixes: List[str] = []
                is_env = False
                verified_cmd = ""

                for j in range(i + 1, len(actions)):
                    nxt = actions[j]
                    if nxt.kind == ActionKind.MUTATE and nxt.target_file in effective_files:
                        sym_note = f" ({', '.join(nxt.symbols_touched)})" if nxt.symbols_touched else ""
                        fix_desc = f"edit {nxt.target_file}{sym_note}"
                        if fix_desc not in intervening_fixes:
                            intervening_fixes.append(fix_desc)
                    elif nxt.kind == ActionKind.ENV_EXEC and nxt.command:
                        if TOOLCHAIN_CMD_RE.search(nxt.command) and nxt.exit_code in (0, None):
                            intervening_fixes.append(f"run `{nxt.command}`")
                            is_env = True
                        elif nxt.exit_code == 0 and not nxt.error_signature and intervening_fixes:
                            verified_cmd = nxt.command
                            break

                if intervening_fixes and verified_cmd:
                    seen_sigs.add(err_sig)
                    repairs.append(
                        CausalRepairPair(
                            error_signature=err_sig,
                            failed_command=act.command or "",
                            repair_action=" -> ".join(intervening_fixes),
                            verification_cmd=verified_cmd,
                            is_env_or_toolchain=is_env,
                        )
                    )
        return repairs

    def _build_guards(
        self, workspace_root: str, files: List[str], symbols: List[str]
    ) -> List[GuardProbe]:
        guards: List[GuardProbe] = []
        ws = Path(workspace_root)
        for f in files[:5]:
            guards.append(GuardProbe(probe_type="file_exists", target=f))
            if not is_code_file(f):
                continue
            full_path = ws / f
            if full_path.exists() and full_path.is_file():
                try:
                    text = full_path.read_text(encoding="utf-8", errors="ignore")
                    for sym in symbols:
                        if re.search(rf"\b{re.escape(sym)}\b", text):
                            guards.append(
                                GuardProbe(
                                    probe_type="symbol_present",
                                    target=f,
                                    expected=sym,
                                )
                            )
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
    ) -> Tuple[str, str]:
        """
        Synthesize both:
        1. `problem_intent` (Semantic Topic): High-signal task title combining the most informative
           user goal and/or Git commit subject rather than raw short conversational replies.
        2. `semantic_summary` (Semantic Resolution Summary): Concise explanation of what was solved,
           combining the assistant's final conclusion and key touched modules/symbols.
        """
        # Pick the most informative user intent from the session
        raw_user = ""
        if session.user_intents:
            last_u = session.user_intents[-1].strip()
            if len(last_u) >= 18:
                raw_user = last_u
            else:
                # Look back for a more descriptive task prompt if the last turn was short
                longer = [u.strip() for u in session.user_intents if len(u.strip()) >= 18]
                if longer:
                    raw_user = f"{longer[-1]} ({last_u})"
                else:
                    raw_user = last_u

        if commit_subject and raw_user:
            if len(raw_user) < 24 and commit_subject not in raw_user:
                problem_intent = f"{commit_subject} — {raw_user}"
            else:
                problem_intent = raw_user
        elif commit_subject:
            problem_intent = commit_subject
        elif raw_user:
            problem_intent = raw_user
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

        semantic_summary = " | ".join(summary_parts)
        return problem_intent, semantic_summary

    def _is_meaningful_command(self, cmd: str) -> bool:
        """
        Keep only repeatable verification, test, build, lint, or dependency installation commands;
        filter out exploratory shell noise, inline scratch scripts, and git commit/push/curl plumbing.
        """
        noisy_prefixes = (
            "ls",
            "pwd",
            "cd ",
            "cat ",
            "head ",
            "tail ",
            "echo ",
            "rm ",
            "mv ",
            "cp ",
            "mkdir ",
            "ln ",
            "chmod ",
            "curl ",
            "wget ",
            "TOKEN=",
            "git ",
            "find ",
            "grep ",
            "rg ",
            "sed ",
            "awk ",
            "python3 -c",
            "python -c",
            "node -e",
            "node --input-type=module -e",
        )
        stripped = cmd.strip()
        if any(stripped == p.strip() or stripped.startswith(p) for p in noisy_prefixes):
            return False
        # Also filter compound `cd ... && git commit/push/status` or `TOKEN=...` commands
        lower = stripped.lower()
        if any(
            g in lower
            for g in ("git push", "git commit", "git remote", "git status", "curl -s")
        ) and not any(
            v in lower for v in ("pytest", "unittest", "npm test", "cargo test", "go test")
        ):
            return False
        return True
