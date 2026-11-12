"""Base protocol and utilities for multi-agent transcript & hook adapters."""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tmem.models import ActionNode, Requirement

ERROR_PATTERNS = [
    re.compile(r"([A-Za-z0-9_.]*(?:Error|Exception|Fault|Panic)[A-Za-z0-9_.:]*[^\n]{0,120})"),
    re.compile(r"(error\[E\d+\]:[^\n]{0,120})"),
    re.compile(r"(Cannot find module\s+['\"][^'\"]+['\"][^\n]{0,80})"),
    re.compile(r"(ModuleNotFoundError:[^\n]{0,100})"),
    re.compile(r"(ImportError:[^\n]{0,100})"),
    re.compile(r"(linker `[^`]+` not found)"),
]

GENERIC_FAILURE_SUMMARY_RE = re.compile(
    r"^(?:FAILED\s*\([^)]*\)|FAIL:\s*\S+|ERRORS?!?\s*\([^)]*\)|"
    r"Process exited with code \d+|Command failed \(exit \d+\)|Exit code \d+|"
    r".*tests? failed.*)$",
    re.IGNORECASE,
)

LOW_ENTROPY_SIGNATURES = {
    "error",
    "failed",
    "failure",
    "test failed",
    "tests failed",
    "command failed",
    "exit code 1",
    "exit code 2",
    "exit code 127",
}


def is_high_entropy_error_signature(sig: Optional[str]) -> bool:
    """
    Validate whether an extracted error signature has genuine diagnostic value.
    Rejects generic test runner summaries (e.g., 'FAILED (failures=1)'),
    generic exit code fallbacks (e.g., 'Command failed (exit 2): ...'),
    and overly short or low-entropy strings.
    """
    if not sig:
        return False
    s = sig.strip()
    if len(s) < 8:
        return False
    if s.lower() in LOW_ENTROPY_SIGNATURES:
        return False
    if GENERIC_FAILURE_SUMMARY_RE.match(s):
        return False
    if s.startswith("Command failed (exit ") or s.startswith("Process exited with code "):
        return False
    if re.match(r"^FAILED\b", s, re.IGNORECASE) or re.match(r"^FAIL:\s*", s, re.IGNORECASE):
        return False
    # Must contain a recognizable error archetype or specific diagnosis
    has_archetype = any(
        kw in s
        for kw in (
            "Error",
            "Exception",
            "Fault",
            "Panic",
            "error[E",
            "Cannot find module",
            "not found",
            "unresolved",
            "undefined",
            "Assertion",
            "syntax error",
        )
    )
    return has_archetype

SECRET_REDACT_RE = re.compile(
    r"\b(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{35})\b"
)


def scrub_secrets(text: str) -> str:
    """Redact API keys, PATs, and tokens before any trace memory is persisted."""
    if not text:
        return ""
    return SECRET_REDACT_RE.sub("[REDACTED_SECRET]", text)


def clean_rationale(text: str, max_chars: int = 160) -> Optional[str]:
    """Clean, scrub secrets, and compact a tool rationale/summary into a single line."""
    if not text:
        return None
    scrubbed = scrub_secrets(text).strip()
    # Normalize whitespace/newlines into single spaces
    compact = re.sub(r"\s+", " ", scrubbed).strip()
    # Strip common markdown heading/bullet markers
    compact = re.sub(r"^(?:#+\s*|[-*]\s*)", "", compact).strip()
    if not compact or len(compact) < 3:
        return None
    if len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip() + "..."
    return compact


@dataclass
class ParsedSession:
    """Normalized representation of an agent session across agy, codex, opencode, pi, dsh."""

    agent_name: str
    session_id: str
    workspace_root: str
    user_intents: List[str] = field(default_factory=list)
    assistant_conclusions: List[str] = field(default_factory=list)
    actions: List[ActionNode] = field(default_factory=list)
    active_errors: List[str] = field(default_factory=list)
    user_requirements: List[Requirement] = field(default_factory=list)


FILLER_INTENT_SET = {
    "done",
    "ok",
    "okay",
    "yes",
    "y",
    "go",
    "continue",
    "继续",
    "可以",
    "好的",
    "好",
    "行",
    "确认",
    "没问题",
}


def is_filler_user_intent(text: str) -> bool:
    """Return True if a user message is purely conversational confirmation without task semantics."""
    s = (text or "").strip().lower().rstrip(".!！。，,")
    if not s:
        return True
    return s in FILLER_INTENT_SET


def extract_assistant_conclusion(text: str) -> Optional[str]:
    """
    Extract a concise 1-2 line semantic summary from an assistant's response text
    (stripping markdown links, code blocks, and milestone prompt footers).
    """
    if not text:
        return None
    # Strip fenced code blocks and HTML comments
    cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    # Convert markdown links `[label](url)` to `label`
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)

    meaningful_lines: List[str] = []
    for raw_line in cleaned.splitlines():
        ln = raw_line.strip()
        if not ln or ln.startswith(("> 💡", "---", "```", "<tmem_")):
            continue
        if "是否需要将本次有效执行路径沉淀为记忆" in ln:
            continue
        # Strip leading markdown heading or bullet markers
        ln = re.sub(r"^(?:#+\s*|\d+\.\s*|[-*]\s*)", "", ln).strip()
        if len(ln) >= 12:
            meaningful_lines.append(ln)
            if len(meaningful_lines) >= 2:
                break

    if not meaningful_lines:
        return None
    summary = scrub_secrets("；".join(meaningful_lines))
    return summary[:240]


def normalize_rel_path(raw_path: Optional[str], workspace_root: str) -> Optional[str]:
    """Strip quotes and normalize path relative to workspace_root if inside it."""
    if not raw_path:
        return None
    cleaned = str(raw_path).strip().strip('"').strip("'")
    if not cleaned:
        return None
    try:
        ws = Path(workspace_root).resolve()
        target = Path(cleaned)
        if target.is_absolute():
            resolved = target.resolve()
            if resolved == ws or ws in resolved.parents:
                return str(resolved.relative_to(ws))
        return cleaned
    except Exception:
        return cleaned


def extract_error_signature(text: str) -> Optional[str]:
    """Deterministically extract high-entropy canonical error signature from tool stderr/stdout."""
    if not text:
        return None
    # Normalize and inspect from the end backwards (traceback bottom contains the exact exception)
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not raw_lines:
        return None

    # Clean lines: remove pytest 'E   ' prefix if present
    cleaned_lines = []
    for ln in raw_lines:
        if ln.startswith("E   ") or ln.startswith("E "):
            cleaned_lines.append(ln[2:].strip())
        else:
            cleaned_lines.append(ln)

    # Search window: check the last 40 lines in reverse order
    search_lines = list(reversed(cleaned_lines[-40:]))
    for ln in search_lines:
        for pat in ERROR_PATTERNS:
            m = pat.search(ln)
            if m:
                sig = m.group(1).strip()
                if "=>" not in sig and is_high_entropy_error_signature(sig):
                    return sig[:160]

    return None


NON_CODE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".lock",
    ".csv",
    ".svg",
    ".html",
    ".css",
}

SYMBOL_STOPWORDS = {
    "for",
    "if",
    "else",
    "elif",
    "while",
    "return",
    "in",
    "to",
    "of",
    "on",
    "from",
    "with",
    "by",
    "as",
    "is",
    "at",
    "be",
    "or",
    "and",
    "not",
    "the",
    "that",
    "this",
    "self",
    "super",
    "new",
    "var",
    "let",
    "const",
    "async",
    "await",
    "true",
    "false",
    "null",
    "none",
}


DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt", ".adoc", ".pdf", ".html", ".htm"}

TEST_FILE_PATTERNS = [
    re.compile(r"(?:^|[/\\])(?:tests?|testing|__tests__)(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])test_[^/\\]+\.[a-zA-Z0-9]+$", re.IGNORECASE),
    re.compile(r"_[a-zA-Z0-9]+_test\.[a-zA-Z0-9]+$", re.IGNORECASE),
    re.compile(r"[-_.](?:test|spec)\.[a-zA-Z0-9]+$", re.IGNORECASE),
]

INLINE_SCRIPT_PATTERNS = [
    re.compile(r"\b(?:python[0-9.]*|pypy[0-9.]*)\s+-c\b", re.IGNORECASE),
    re.compile(r"\b(?:node|deno|bun)\s+-e\b", re.IGNORECASE),
    re.compile(r"\b(?:bash|sh|zsh)\s+-c\b", re.IGNORECASE),
    re.compile(r"\b(?:ruby|perl)\s+-e\b", re.IGNORECASE),
    re.compile(r"<<-?\s*['\"]?(?:EOF|END|STOP)['\"]?", re.IGNORECASE),
]

META_NON_TASK_PATTERNS = [
    re.compile(r"(?:评审|评价|打分|评分|审查|审计|不留情面)", re.IGNORECASE),
    re.compile(r"\b(?:review|audit|critique|evaluate|scoring)\b", re.IGNORECASE),
    re.compile(r"(?:rate\s+(?:the\s+|this\s+)?(?:project|code|repo|codebase)|rating\s+(?:the\s+|this\s+)?project)", re.IGNORECASE),
    re.compile(r"(?:优化项目|根据反馈优化|优化工程|优化仓库|重构项目)", re.IGNORECASE),
    re.compile(r"^(?:explain|describe|what is|how does|介绍|总结|讲讲)\b", re.IGNORECASE),
]


def is_test_file(file_path: Optional[str]) -> bool:
    """Return True if path points to a test file or test directory."""
    if not file_path:
        return False
    p = str(file_path).strip()
    return any(pat.search(p) for pat in TEST_FILE_PATTERNS)


def is_doc_file(file_path: Optional[str]) -> bool:
    """Return True if path points to documentation or non-executable text."""
    if not file_path:
        return False
    p = Path(str(file_path).strip())
    if p.suffix.lower() in DOC_EXTENSIONS:
        return True
    return any(part.lower() in ("docs", "doc", "documentation") for part in p.parts)


def is_source_code_file(file_path: Optional[str]) -> bool:
    """Return True if path represents genuine application source code (not test, not doc)."""
    if not file_path:
        return False
    if is_doc_file(file_path) or is_test_file(file_path):
        return False
    return is_code_file(file_path)


def is_inline_script_command(cmd: Optional[str]) -> bool:
    """Return True if command is an inline script (-c, -e) or heredoc."""
    if not cmd:
        return False
    s = cmd.strip()
    if "\n" in s:
        return True
    return any(pat.search(s) for pat in INLINE_SCRIPT_PATTERNS)


def is_meta_non_task_intent(text: str) -> bool:
    """Return True if intent is a meta-request (evaluation, review, generic feedback) rather than a concrete task."""
    if not text:
        return False
    s = text.strip()
    return any(pat.search(s) for pat in META_NON_TASK_PATTERNS)


TRACEBACK_FILE_PATTERNS = [
    re.compile(r'File "([^"]+)", line \d+'),
    re.compile(r'^\s*([A-Za-z0-9_./\\-]+\.[a-zA-Z0-9_]+):(\d+):(?:\s*(?:in |error|fatal|warning))?', re.MULTILINE),
    re.compile(r'^\s*(?:FAIL|ERROR):\s+[A-Za-z0-9_.]+\s+\(([A-Za-z0-9_./\\-]+\.[a-zA-Z0-9_]+)\)', re.MULTILINE),
]


def extract_traceback_files(text: Optional[str], workspace_root: str = "") -> Set[str]:
    """Deterministically extract referenced source/test file paths from error output / traceback."""
    if not text:
        return set()
    found = set()
    for pat in TRACEBACK_FILE_PATTERNS:
        for m in pat.finditer(text):
            raw_f = m.group(1).strip()
            if any(pfx in raw_f for pfx in ("<stdin>", "<string>", "site-packages", "/usr/lib", ".local/lib")):
                continue
            norm = normalize_rel_path(raw_f, workspace_root)
            if norm and not norm.startswith("/"):
                found.add(norm)
    return found


def is_code_file(file_path: Optional[str]) -> bool:
    """Return True if file_path represents a source code file suitable for AST/symbol guards."""
    if not file_path:
        return True
    p = Path(file_path)
    if p.name.startswith(".") and p.suffix == "":
        return False
    return p.suffix.lower() not in NON_CODE_EXTENSIONS


def extract_symbols_from_text(text: str, file_path: Optional[str] = None) -> List[str]:
    """Extract defined/modified function or class names from code snippets via AST (Python) or regex."""
    if not text or not is_code_file(file_path):
        return []
    symbols = set()
    is_py = file_path and file_path.lower().endswith(".py")
    if is_py:
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name.lower() not in SYMBOL_STOPWORDS:
                        symbols.add(node.name)
            if symbols:
                return sorted(symbols)
        except Exception:
            pass

    # Regex fallback for non-Python files or partial diff snippets
    for m in re.finditer(
        r"(?:^|[\s;{}()])(?:def|class|fn|func|function|interface|struct)\s+([A-Za-z_][A-Za-z0-9_]{2,})\s*(?:[\(<:{]|$)",
        text,
        re.MULTILINE,
    ):
        cand = m.group(1)
        if cand.lower() not in SYMBOL_STOPWORDS:
            symbols.add(cand)
    return sorted(symbols)


class AgentAdapter:
    """Base interface for agent-specific transcript parsers and hook bridges."""

    name: str = "base"

    def parse_transcript(
        self, transcript_path: str, workspace_root: str, min_step_idx: int = 0
    ) -> ParsedSession:
        raise NotImplementedError

    def discover_latest_transcript(self, workspace_root: str) -> Optional[str]:
        raise NotImplementedError
