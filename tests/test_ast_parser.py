"""Unit tests for optional multi-language tree-sitter AST support and graceful fallback."""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tmem import ast_parser
from tmem.adapters.base import (
    extract_local_imports,
    extract_symbols_for_line_ranges,
    extract_symbols_from_text,
)
from tmem.guards import check_symbol_in_ast


@dataclass
class _FakeNode:
    type: str
    start_point: Tuple[int, int]
    end_point: Tuple[int, int]
    text: bytes = b""
    children: List["_FakeNode"] = field(default_factory=list)
    fields: Dict[str, "_FakeNode"] = field(default_factory=dict)

    def child_by_field_name(self, name: str) -> Optional["_FakeNode"]:
        return self.fields.get(name)


@dataclass
class _FakeTree:
    root_node: _FakeNode


class _FakeParser:
    def __init__(self, root: _FakeNode) -> None:
        self._root = root

    def parse(self, _source_bytes: bytes) -> _FakeTree:
        return _FakeTree(root_node=self._root)


class TestAstParser(unittest.TestCase):
    def tearDown(self) -> None:
        ast_parser.clear_parser_cache()

    def test_detect_language(self) -> None:
        self.assertEqual(ast_parser.detect_language("src/app.py"), "python")
        self.assertEqual(ast_parser.detect_language("web/index.ts"), "typescript")
        self.assertEqual(ast_parser.detect_language("web/App.tsx"), "tsx")
        self.assertEqual(ast_parser.detect_language("pkg/main.go"), "go")
        self.assertEqual(ast_parser.detect_language("src/lib.rs"), "rust")
        self.assertIsNone(ast_parser.detect_language("README.md"))

    def test_graceful_fallback_without_tree_sitter(self) -> None:
        ast_parser.clear_parser_cache()
        ast_parser._PARSER_CACHE["typescript"] = None

        ts_code = "export class AuthService {\n  login() {}\n}\n"
        self.assertEqual(
            extract_symbols_for_line_ranges(ts_code, "src/auth.ts", [(1, 3)]),
            [],
        )
        self.assertEqual(
            extract_symbols_from_text(ts_code, "src/auth.ts"),
            ["AuthService"],
        )
        self.assertTrue(check_symbol_in_ast(ts_code, "src/auth.ts", "AuthService"))
        self.assertFalse(check_symbol_in_ast(ts_code, "src/auth.ts", "MissingSvc"))
        self.assertEqual(
            extract_local_imports("import { x } from './helper';", "src/auth.ts", {"src/helper.ts"}),
            set(),
        )

    def test_tree_sitter_line_range_and_guard_and_imports(self) -> None:
        # Simulated TS AST:
        # 1: import { verify } from "../utils/auth";
        # 2-8: class UserService { login() { ... } logout() { ... } }
        # 9-11: const handleSubmit = () => {}
        imp_str = _FakeNode(type="string", start_point=(0, 24), end_point=(0, 39), text=b'"../utils/auth"')
        imp_stmt = _FakeNode(type="import_statement", start_point=(0, 0), end_point=(0, 40), children=[imp_str])

        cls_name = _FakeNode(type="type_identifier", start_point=(1, 6), end_point=(1, 17), text=b"UserService")
        login_name = _FakeNode(type="property_identifier", start_point=(2, 2), end_point=(2, 7), text=b"login")
        login_fn = _FakeNode(
            type="method_definition",
            start_point=(2, 2),
            end_point=(4, 3),
            fields={"name": login_name},
        )
        logout_name = _FakeNode(type="property_identifier", start_point=(5, 2), end_point=(5, 8), text=b"logout")
        logout_fn = _FakeNode(
            type="method_definition",
            start_point=(5, 2),
            end_point=(7, 3),
            fields={"name": logout_name},
        )
        cls_node = _FakeNode(
            type="class_declaration",
            start_point=(1, 0),
            end_point=(7, 1),
            children=[login_fn, logout_fn],
            fields={"name": cls_name},
        )

        arrow_fn = _FakeNode(type="arrow_function", start_point=(8, 21), end_point=(10, 1))
        var_name = _FakeNode(type="identifier", start_point=(8, 6), end_point=(8, 18), text=b"handleSubmit")
        var_decl = _FakeNode(
            type="variable_declarator",
            start_point=(8, 6),
            end_point=(10, 1),
            children=[arrow_fn],
            fields={"name": var_name, "value": arrow_fn},
        )

        root = _FakeNode(
            type="program",
            start_point=(0, 0),
            end_point=(10, 1),
            children=[imp_stmt, cls_node, var_decl],
        )
        ast_parser._PARSER_CACHE["typescript"] = _FakeParser(root)

        ts_text = "// comment mentioning ghost_fn\nplaceholder"
        # Line 4 (1-indexed) is inside login() (start_point 2 -> line 3, end_point 4 -> line 5)
        self.assertEqual(
            extract_symbols_for_line_ranges(ts_text, "src/services/user.ts", [(4, 4)]),
            ["UserService.login"],
        )
        self.assertEqual(
            extract_symbols_from_text(ts_text, "src/services/user.ts"),
            ["UserService", "handleSubmit", "login", "logout"],
        )
        self.assertTrue(check_symbol_in_ast(ts_text, "src/services/user.ts", "UserService.login"))
        # Comment-only mention must NOT fool AST guard
        self.assertFalse(check_symbol_in_ast(ts_text, "src/services/user.ts", "ghost_fn"))

        imports = extract_local_imports(
            ts_text,
            "src/services/user.ts",
            {"src/utils/auth.ts", "src/utils/other.ts"},
        )
        self.assertEqual(imports, {"src/utils/auth.ts"})


if __name__ == "__main__":
    unittest.main()
