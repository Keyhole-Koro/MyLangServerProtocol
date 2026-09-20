"""Compatibility facade for the MyLang analysis API."""

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mylang_lsp.analysis import *  # noqa: F401,F403,E402
from mylang_lsp.analysis import __all__  # noqa: E402
