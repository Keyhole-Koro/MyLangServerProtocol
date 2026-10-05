"""Reusable fixtures and in-memory JSON-RPC harness for MyLang LSP tests."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lsp_analysis import DocumentSnapshot  # noqa: E402
from server import LspServer  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


@dataclass(frozen=True)
class Fixture:
    """Named MyLang source fixture with position and snapshot helpers."""

    name: str
    source: str

    def snapshot(self, version: int = 1) -> DocumentSnapshot:
        return snapshot(self.source, Path(self.name).name, version)

    def position(self, needle: str, occurrence: int = 0, after: int = 0) -> dict:
        return position(self.source, needle, occurrence, after)


@dataclass(frozen=True)
class WorkspaceDocument:
    """A fixture materialized at a real path for import-resolution tests."""

    path: Path
    source: str

    @property
    def uri(self) -> str:
        return self.path.as_uri()


class FixtureWorkspace:
    """Temporary workspace populated from named source fixtures."""

    def __init__(self) -> None:
        self._temporary: Optional[tempfile.TemporaryDirectory] = None
        self.root: Optional[Path] = None

    def __enter__(self) -> "FixtureWorkspace":
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        return self

    def __exit__(self, *_unused) -> None:
        assert self._temporary is not None
        self._temporary.cleanup()
        self._temporary = None
        self.root = None

    def materialize(self, fixture_name: str, relative_path: Optional[str] = None) -> WorkspaceDocument:
        assert self.root is not None, "FixtureWorkspace must be used in a with block"
        path = self.root / (relative_path or Path(fixture_name).name)
        path.parent.mkdir(parents=True, exist_ok=True)
        source = fixture_text(fixture_name)
        path.write_text(source, encoding="utf-8")
        return WorkspaceDocument(path, source)


def fixture(name: str) -> Fixture:
    return Fixture(name, fixture_text(name))


def fixture_text(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def fixture_path(name: str) -> Path:
    return FIXTURE_ROOT / name


def fixture_snapshot(name: str, version: int = 1) -> tuple[str, DocumentSnapshot]:
    source = fixture_text(name)
    return source, snapshot(source, Path(name).name, version)


def snapshot(source: str, name: str = "feature.mln", version: int = 1) -> DocumentSnapshot:
    return DocumentSnapshot.create(f"file:///tmp/{name}", source, version)


def position(source: str, needle: str, occurrence: int = 0, after: int = 0) -> dict:
    start = -1
    cursor = 0
    for _ in range(occurrence + 1):
        start = source.index(needle, cursor)
        cursor = start + len(needle)
    prefix = source[:start + after]
    line = prefix.count("\n")
    character = len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2
    return {"line": line, "character": character}


class LspTestServer(LspServer):
    """LSP server that retains JSON-RPC output and exposes test operations."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def send(self, payload):
        self.messages.append(payload)

    def notify(self, method: str, params: dict) -> None:
        self.handle({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, request_id: int, method: str, params: dict) -> dict:
        self.handle({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        })
        return self.response(request_id)

    def response(self, request_id: int) -> dict:
        return next(message for message in reversed(self.messages)
                    if message.get("id") == request_id)

    def open_document(self, uri: str, text: str, version: int = 1) -> None:
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "mylang", "version": version, "text": text,
        }})


def run_with_server(test: Callable[[LspTestServer], None]) -> None:
    server = LspTestServer()
    try:
        test(server)
    finally:
        server.stop_syntax_checker()
