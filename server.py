#!/usr/bin/env python3
"""Compatibility entry point for the MyLang language server."""

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mylang_lsp.features.diagnostics import DIAGNOSTIC_SEVERITY_ERROR
from mylang_lsp.features.document_symbols import ENGINE_SYMBOL_KIND, SYMBOL_KIND
from mylang_lsp.features.semantic_tokens import (
    KIND_TO_TYPE,
    TOKEN_MODIFIERS,
    TOKEN_TYPE_INDEX,
    TOKEN_TYPES,
)
from mylang_lsp.protocol.server import LspServer

__all__ = [
    "DIAGNOSTIC_SEVERITY_ERROR",
    "ENGINE_SYMBOL_KIND",
    "KIND_TO_TYPE",
    "LspServer",
    "SYMBOL_KIND",
    "TOKEN_MODIFIERS",
    "TOKEN_TYPE_INDEX",
    "TOKEN_TYPES",
]


if __name__ == "__main__":
    LspServer().run()
