#!/usr/bin/env python3
"""Only \\r\\n, \\r and \\n end a line, as in LSP clients and the native checker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mylang_lsp.features.diagnostics import DiagnosticsService  # noqa: E402
from mylang_lsp.features.document_symbols import DocumentSymbolService  # noqa: E402
from mylang_lsp.features.semantic_tokens import (  # noqa: E402
    TOKEN_TYPE_INDEX,
    SemanticTokenService,
)
from mylang_lsp.source_text import split_lines  # noqa: E402


# Characters str.splitlines() treats as line breaks but LSP does not.
NOT_LINE_ENDINGS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ")


def no_frontend(text, uri=""):
    return None


def test_split_lines_matches_splitlines_on_lsp_endings():
    for text in (
        "",
        "a",
        "a\n",
        "a\nb",
        "a\n\nb\n\n",
        "\n",
        "a\r\nb\r\n",
        "a\rb\r",
        "a\r\n\rb\n\r\n",
        "\r\n",
        "\r",
    ):
        assert split_lines(text) == text.splitlines(), repr(text)


def test_split_lines_keeps_other_separators_on_the_line():
    for separator in NOT_LINE_ENDINGS:
        assert split_lines(f"a{separator}b\nc\n") == [f"a{separator}b", "c"], repr(separator)


def test_brackets_in_comments_and_strings_stay_protected():
    service = DiagnosticsService(no_frontend)
    for separator in NOT_LINE_ENDINGS:
        comment = f"// page{separator}(\ni32 x;\n"
        assert service.analyze(comment) == [], repr(separator)
        string = f'str s = "a{separator}(";\n'
        assert service.analyze(string) == [], repr(separator)


def test_real_bracket_after_separator_is_on_the_lsp_line():
    service = DiagnosticsService(no_frontend)
    diagnostics = service.analyze("i32 x;\ni32 y;\x0c(\n")
    assert len(diagnostics) == 1
    start = diagnostics[0]["range"]["start"]
    assert start == {"line": 1, "character": 7}, start


def test_comment_tokens_follow_lsp_lines():
    comment = TOKEN_TYPE_INDEX["comment"]
    for separator in NOT_LINE_ENDINGS:
        text = f"// a{separator}b\ni32 x; // c\n"
        encoded = SemanticTokenService(no_frontend).encode(text)
        assert encoded == [
            0, 0, 6, comment, 0,
            1, 7, 4, comment, 0,
        ], (repr(separator), encoded)


def test_document_symbols_past_the_last_lsp_line_are_dropped():
    # A model's frontend result runs on past the file into generated code.
    def frontend(text, uri=""):
        return {"symbols": [[0, 4, 4, "function"], [1, 0, 3, "variable"]]}

    symbols = DocumentSymbolService(frontend).symbols("i32 main();\x0c// x\n")
    assert [symbol["name"] for symbol in symbols] == ["main"], symbols
    assert symbols[0]["range"]["start"] == {"line": 0, "character": 4}


def run():
    test_split_lines_matches_splitlines_on_lsp_endings()
    test_split_lines_keeps_other_separators_on_the_line()
    test_brackets_in_comments_and_strings_stay_protected()
    test_real_bracket_after_separator_is_on_the_lsp_line()
    test_comment_tokens_follow_lsp_lines()
    test_document_symbols_past_the_last_lsp_line_are_dropped()


if __name__ == "__main__":
    run()
    print("[PASS] line endings follow LSP (\\r\\n, \\r, \\n)")
