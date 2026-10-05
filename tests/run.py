"""Run the MyLang LSP test modules in a stable feature-oriented order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


MODULES = (
    "tests.frontend.test_lsp_model",
    "tests.frontend.test_doc_comments",
    "tests.frontend.test_semantic_tokens",
    "tests.interaction.test_hover",
    "tests.interaction.test_signature_help",
    "tests.navigation.test_definition",
    "tests.navigation.test_definition_resolution",
    "tests.navigation.test_references",
    "tests.protocol.test_lsp_protocol",
    "tests.protocol.test_stdio_protocol",
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    for module in MODULES:
        completed = subprocess.run([sys.executable, "-m", module], cwd=root)
        if completed.returncode:
            raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
