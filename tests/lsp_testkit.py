"""Reusable fixtures and in-memory JSON-RPC harness for MyLang LSP tests."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lsp_analysis import DocumentSnapshot  # noqa: E402
from server import LspServer  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class Fixture:
    """Named MyLang source fixture with position and snapshot helpers."""

    name: str
    source: str

    def snapshot(self, version: int = 1) -> DocumentSnapshot:
        return snapshot(self.source, Path(self.name).name, version)

    def position(self, needle: str, occurrence: int = 0, after: int = 0) -> dict:
        return position(self.source, needle, occurrence, after)


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
