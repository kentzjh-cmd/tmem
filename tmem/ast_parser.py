"""
Optional Multi-Language Tree-Sitter AST Parser for tmem.

Preserves tmem's zero-required-dependency guarantee:
- Python (.py) always has built-in stdlib `ast` support.
- Other languages (JS/TS/TSX, Go, Rust, Java, C/C++, Ruby, PHP, C#) use `tree-sitter`
  when installed via `pip install -e ".[ast]"` (`tree-sitter` + `tree-sitter-language-pack`
  or `tree-sitter-languages` / individual `tree_sitter_<lang>` packages).
- When `tree-sitter` is unavailable, all entry points degrade gracefully without raising.
"""

from __future__ import annotations

import importlib
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from tmem import logger

EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
}

# Tree-sitter node types representing container definitions (classes, structs, interfaces, impls)
CONTAINER_NODE_TYPES: Set[str] = {
    "class_definition",
    "class_declaration",
    "class_specifier",
    "class",
    "struct_item",
    "struct_specifier",
    "type_declaration",
    "interface_declaration",
    "trait_item",
    "enum_item",
    "enum_declaration",
    "impl_item",
    "module",
}

# Tree-sitter node types representing callable / function / method definitions
FUNCTION_NODE_TYPES: Set[str] = {
    "function_definition",
    "function_declaration",
    "function_item",
    "method_definition",
    "method_declaration",
    "method",
    "singleton_method",
    "constructor_declaration",
    "arrow_function",
    "function_expression",
}

# Child node types that commonly hold the identifier name of a definition
NAME_CHILD_TYPES: Set[str] = {
    "identifier",
    "type_identifier",
    "property_identifier",
    "field_identifier",
    "constant",
    "name",
}

_PARSER_CACHE: Dict[str, Optional[Any]] = {}


def detect_language(file_path: Optional[str]) -> Optional[str]:
    """Map a file path extension to a canonical tree-sitter language name."""
    if not file_path:
        return None
    return EXT_TO_LANG.get(Path(file_path).suffix.lower())


def clear_parser_cache() -> None:
    """Clear cached tree-sitter parsers (primarily for testing)."""
    _PARSER_CACHE.clear()


def _load_parser_from_backends(lang: str) -> Optional[Any]:
    """Attempt to construct a tree-sitter parser from available installed packages."""
    for pack_mod_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            pack_mod = importlib.import_module(pack_mod_name)
            get_parser = getattr(pack_mod, "get_parser", None)
            if callable(get_parser):
                parser = get_parser(lang)
                if parser is not None:
                    return parser
        except Exception:
            continue

    # Fallback: individual `tree_sitter_<lang>` package + `tree_sitter.Parser`
    try:
        ts_mod = importlib.import_module("tree_sitter")
        lang_pkg_name = f"tree_sitter_{lang}"
        lang_mod = importlib.import_module(lang_pkg_name)
        lang_fn = getattr(lang_mod, "language", None)
        if callable(lang_fn):
            language_obj = ts_mod.Language(lang_fn())
            try:
                return ts_mod.Parser(language_obj)
            except TypeError:
                parser = ts_mod.Parser()
                parser.set_language(language_obj)
                return parser
    except Exception:
        pass

    return None


def get_tree_sitter_parser(lang: Optional[str]) -> Optional[Any]:
    """Return a cached tree-sitter parser for `lang`, or None if unavailable."""
    if not lang:
        return None
    if lang in _PARSER_CACHE:
        return _PARSER_CACHE[lang]
    parser = _load_parser_from_backends(lang)
    _PARSER_CACHE[lang] = parser
    return parser


def is_tree_sitter_available(file_path: Optional[str] = None) -> bool:
    """Return True if tree-sitter can parse `file_path` (or any common language if omitted)."""
    lang = detect_language(file_path) if file_path else "javascript"
    return get_tree_sitter_parser(lang) is not None


def _node_text(node: Any, source_bytes: bytes) -> str:
    raw = getattr(node, "text", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        return raw
    start_b = getattr(node, "start_byte", None)
    end_b = getattr(node, "end_byte", None)
    if isinstance(start_b, int) and isinstance(end_b, int):
        return source_bytes[start_b:end_b].decode("utf-8", errors="ignore")
    return ""


def _extract_node_name(node: Any, source_bytes: bytes) -> str:
    """Extract the symbol identifier name from a tree-sitter definition node."""
    child_by_field = getattr(node, "child_by_field_name", None)
    if callable(child_by_field):
        for field_name in ("name", "type", "declarator"):
            target = child_by_field(field_name)
            if target is None:
                continue
            if getattr(target, "type", "") in NAME_CHILD_TYPES:
                txt = _node_text(target, source_bytes).strip()
                if txt:
                    return txt
            # C/C++ function_declarator -> declarator identifier
            sub = _extract_node_name(target, source_bytes)
            if sub:
                return sub

    for child in getattr(node, "children", ()) or ():
        ctype = getattr(child, "type", "")
        if ctype in NAME_CHILD_TYPES:
            txt = _node_text(child, source_bytes).strip()
            if txt:
                return txt
        # Go type_declaration -> type_spec -> type_identifier
        if ctype == "type_spec":
            sub = _extract_node_name(child, source_bytes)
            if sub:
                return sub

    return ""


def _iter_tree_definitions(
    node: Any,
    source_bytes: bytes,
    container_stack: Tuple[str, ...] = (),
) -> Iterable[Tuple[str, int, int]]:
    """
    Yield `(qualified_symbol_name, start_line_1idx, end_line_1idx)` for all
    container and function/method definitions in a tree-sitter syntax tree.
    """
    ntype = getattr(node, "type", "")
    start_pt = getattr(node, "start_point", (0, 0))
    end_pt = getattr(node, "end_point", start_pt)
    start_ln = int(start_pt[0]) + 1
    end_ln = max(start_ln, int(end_pt[0]) + 1)

    next_stack = container_stack
    if ntype in CONTAINER_NODE_TYPES:
        cname = _extract_node_name(node, source_bytes)
        if cname:
            qualified = ".".join((*container_stack, cname))
            yield qualified, start_ln, end_ln
            next_stack = (*container_stack, cname)
    elif ntype in FUNCTION_NODE_TYPES:
        fname = _extract_node_name(node, source_bytes)
        if not fname and ntype in ("arrow_function", "function_expression"):
            # Check parent variable_declarator / lexical_declaration if handled at parent
            fname = ""
        if fname:
            qualified = ".".join((*container_stack, fname))
            yield qualified, start_ln, end_ln
    elif ntype == "variable_declarator":
        # JS/TS `const handleSubmit = (...) => { ... }`
        child_by_field = getattr(node, "child_by_field_name", None)
        val_node = child_by_field("value") if callable(child_by_field) else None
        if val_node is not None and getattr(val_node, "type", "") in (
            "arrow_function",
            "function_expression",
        ):
            vname = _extract_node_name(node, source_bytes)
            if vname:
                qualified = ".".join((*container_stack, vname))
                yield qualified, start_ln, end_ln

    for child in getattr(node, "children", ()) or ():
        yield from _iter_tree_definitions(child, source_bytes, next_stack)


def _parse_tree(content: str, file_path: Optional[str]) -> Optional[Tuple[Any, bytes]]:
    lang = detect_language(file_path)
    parser = get_tree_sitter_parser(lang)
    if parser is None or not content:
        return None
    source_bytes = content.encode("utf-8", errors="ignore")
    try:
        tree = parser.parse(source_bytes)
        root = getattr(tree, "root_node", None)
        if root is None:
            return None
        return root, source_bytes
    except Exception as e:
        logger.debug("tree-sitter parse failed for %s: %s", file_path, e)
        return None


def _ranges_overlap(a_start: int, a_end: int, ranges: List[Tuple[int, int]]) -> bool:
    for r_start, r_end in ranges:
        if a_start <= r_end and r_start <= a_end:
            return True
    return False


def extract_ts_symbols_from_text(
    text: str, file_path: Optional[str], stopwords: Optional[Set[str]] = None
) -> List[str]:
    """Extract defined symbol names from code text using tree-sitter."""
    parsed = _parse_tree(text, file_path)
    if parsed is None:
        return []
    root, source_bytes = parsed
    stop = stopwords or set()
    found: Set[str] = set()
    for qualified, _, _ in _iter_tree_definitions(root, source_bytes):
        leaf = qualified.split(".")[-1]
        if leaf and leaf.lower() not in stop and len(leaf) >= 2:
            found.add(leaf)
    return sorted(found)


def extract_ts_symbols_for_line_ranges(
    content: str, file_path: Optional[str], line_ranges: List[Tuple[int, int]]
) -> List[str]:
    """
    Parse `content` via tree-sitter and return symbol names (`func`, `Class`, `Class.method`)
    whose line intervals overlap any range in `line_ranges` (1-indexed inclusive).
    """
    if not line_ranges:
        return []
    parsed = _parse_tree(content, file_path)
    if parsed is None:
        return []
    root, source_bytes = parsed

    matched: List[str] = []
    containers_with_matched_children: Set[str] = set()
    all_hits: List[Tuple[str, bool]] = []

    for qualified, start_ln, end_ln in _iter_tree_definitions(root, source_bytes):
        if _ranges_overlap(start_ln, end_ln, line_ranges):
            is_nested = "." in qualified
            if is_nested:
                parent = qualified.rsplit(".", 1)[0]
                containers_with_matched_children.add(parent)
            all_hits.append((qualified, is_nested))

    for qualified, is_nested in all_hits:
        if not is_nested and qualified in containers_with_matched_children:
            continue
        if qualified not in matched:
            matched.append(qualified)

    return sorted(matched)


def check_ts_symbol_present(
    content: str, file_path: Optional[str], expected_symbol: str
) -> Optional[bool]:
    """
    Verify presence of `expected_symbol` in `content` via tree-sitter AST.
    Returns None if tree-sitter is unavailable for `file_path` so caller can fall back.
    """
    parsed = _parse_tree(content, file_path)
    if parsed is None:
        return None
    root, source_bytes = parsed
    target = expected_symbol.strip()
    if not target:
        return False

    for qualified, _, _ in _iter_tree_definitions(root, source_bytes):
        if qualified == target:
            return True
        if "." not in target and qualified.split(".")[-1] == target:
            return True
    return False


_JS_TS_RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def extract_ts_local_imports(
    content: str, file_path: str, candidate_files: Set[str]
) -> Optional[Set[str]]:
    """
    Extract statically imported local files for JS/TS/Go/Rust via tree-sitter.
    Returns None when tree-sitter is unavailable for `file_path`.
    """
    parsed = _parse_tree(content, file_path)
    if parsed is None:
        return None
    root, source_bytes = parsed
    lang = detect_language(file_path)
    if not lang:
        return set()

    raw_specs: Set[str] = set()

    def _walk(node: Any) -> None:
        ntype = getattr(node, "type", "")
        if lang in ("javascript", "typescript", "tsx"):
            if ntype in ("import_statement", "export_statement"):
                for child in getattr(node, "children", ()) or ():
                    if getattr(child, "type", "") == "string":
                        raw = _node_text(child, source_bytes).strip().strip("\"'`")
                        if raw:
                            raw_specs.add(raw)
            elif ntype == "call_expression":
                fn_node = (
                    node.child_by_field_name("function")
                    if hasattr(node, "child_by_field_name")
                    else None
                )
                fn_name = _node_text(fn_node, source_bytes).strip() if fn_node else ""
                if fn_name in ("require", "import"):
                    args_node = node.child_by_field_name("arguments")
                    for child in getattr(args_node, "children", ()) or ():
                        if getattr(child, "type", "") == "string":
                            raw = _node_text(child, source_bytes).strip().strip("\"'`")
                            if raw:
                                raw_specs.add(raw)
        elif lang == "go" and ntype == "import_spec":
            raw = _node_text(node, source_bytes).strip().strip('"')
            if raw:
                raw_specs.add(raw)
        elif lang == "rust" and ntype == "use_declaration":
            raw = _node_text(node, source_bytes)
            for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", raw):
                if part not in ("use", "crate", "self", "super", "as", "pub"):
                    raw_specs.add(part)

        for child in getattr(node, "children", ()) or ():
            _walk(child)

    _walk(root)

    if not raw_specs:
        return set()

    matched: Set[str] = set()
    cur_dir = Path(file_path).parent

    if lang in ("javascript", "typescript", "tsx"):
        for spec in raw_specs:
            if not spec.startswith("."):
                continue
            resolved_base = posixpath.normpath(
                posixpath.join(cur_dir.as_posix(), spec)
            )
            candidates_to_try = [resolved_base]
            for ext in _JS_TS_RESOLVE_EXTS:
                candidates_to_try.append(f"{resolved_base}{ext}")
                candidates_to_try.append(posixpath.join(resolved_base, f"index{ext}"))
            clean_cands = {c.lstrip("./") for c in candidates_to_try}
            for cand in candidate_files:
                if cand.lstrip("./") in clean_cands:
                    matched.add(cand)
    else:
        for cand in candidate_files:
            stem = Path(cand).stem
            parent_and_stem = Path(cand).with_suffix("").as_posix()
            if any(
                spec == stem or parent_and_stem.endswith(spec)
                for spec in raw_specs
            ):
                matched.add(cand)

    return matched
