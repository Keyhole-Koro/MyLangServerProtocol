#!/usr/bin/env python3
"""Versioned document and UTF-8/UTF-16 position model tests."""

from feature_test_support import LspServer, snapshot
from lsp_analysis import DocumentStore
from server import TOKEN_TYPE_INDEX


def test_line_map():
    doc = snapshot('i32 main() { print("日本😀"); foo(); }\n', "unicode.mln")
    source = doc.text
    byte_offset = source.encode("utf-8").index(b"foo")
    lsp = doc.line_map.offset_to_lsp(byte_offset)
    expected_prefix = source[:source.index("foo")]
    assert lsp == {
        "line": 0,
        "character": len(expected_prefix.encode("utf-16-le")) // 2,
    }
    assert doc.line_map.lsp_to_offset(lsp) == byte_offset


def test_document_store():
    store = DocumentStore()
    first = store.update("file:///tmp/a.mln", "i32 a;\n", 1)
    second = store.update("file:///tmp/a.mln", "i32 b;\n", 2)
    assert first.version == 1
    assert store.get(second.uri) is second
    store.close(second.uri)
    assert store.get(second.uri) is None


def test_semantic_token_utf16():
    server = LspServer()
    source = 'void main() { print("日本😀"); foo(1); }\n'
    try:
        encoded = server.semantic_tokens(source)
    finally:
        server.stop_syntax_checker()
    line = column = 0
    function_columns = []
    for index in range(0, len(encoded), 5):
        delta_line, delta_column, _length, token_type, _modifiers = encoded[index:index + 5]
        line += delta_line
        column = delta_column if delta_line else column + delta_column
        if token_type == TOKEN_TYPE_INDEX["function"]:
            function_columns.append(column)
    # TOKEN_TYPE_INDEX is module-level; avoid relying on a Python code-point slice.
    expected = len(source[:source.index("foo")].encode("utf-16-le")) // 2
    assert expected in function_columns


def test_versioned_analysis_cache():
    server = LspServer()
    try:
        uri = "file:///tmp/versioned.mln"
        first = snapshot("i32 first() { return 1; }\n", "versioned.mln", 1)
        second = snapshot("i32 second() { return 2; }\n", "versioned.mln", 2)
        first_unit = server.language_features.unit(first)
        second_unit = server.language_features.unit(second)
        assert [function.name for function in first_unit.functions] == ["first"]
        assert [function.name for function in second_unit.functions] == ["second"]
        assert list(server.frontend_analysis.cache) == [(uri, 2)]
    finally:
        server.stop_syntax_checker()


if __name__ == "__main__":
    test_line_map()
    test_document_store()
    test_semantic_token_utf16()
    test_versioned_analysis_cache()
    print("[PASS] LSP document and position model")
