"""Shared helpers for interactive MyLang LSP feature tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lsp_analysis import DocumentSnapshot  # noqa: E402
from server import LspServer  # noqa: E402


def snapshot(source: str, name: str = "feature.mln", version: int = 1) -> DocumentSnapshot:
    return DocumentSnapshot.create(f"file:///tmp/{name}", source, version)


def position(source: str, needle: str, occurrence: int = 0, after: int = 0) -> dict:
    start = -1
    cursor = 0
    for _ in range(occurrence + 1):
        start = source.index(needle, cursor)
        cursor = start + len(needle)
    offset = start + after
    prefix = source[:offset]
    line = prefix.count("\n")
    line_prefix = prefix.rsplit("\n", 1)[-1]
    character = len(line_prefix.encode("utf-16-le")) // 2
    return {"line": line, "character": character}


def run_with_server(test):
    server = LspServer()
    try:
        test(server)
    finally:
        server.stop_syntax_checker()

