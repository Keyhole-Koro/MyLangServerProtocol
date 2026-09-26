#!/usr/bin/env python3
"""Definition and reference resolution for function-pointer uses."""

import tempfile
from pathlib import Path

from feature_test_support import position, run_with_server, snapshot
from server import LspServer


SOURCE = """package callbacks;
i32 handle(i32 value) { return value; }
void init() {
    install(handle);
}
"""


def test(server):
    doc = snapshot(SOURCE, "references.mln")
    server.language_features.unit(doc)

    callback_definition = server.language_features.definition(
        doc, position(SOURCE, "handle", occurrence=1, after=1)
    )
    assert callback_definition is not None
    assert callback_definition["range"]["start"] == position(SOURCE, "handle")

    without_declaration = server.language_features.references(
        doc,
        position(SOURCE, "handle", after=1),
        include_declaration=False,
    )
    assert len(without_declaration) == 1
    assert without_declaration[0]["range"]["start"] == position(
        SOURCE, "handle", occurrence=1
    )

    with_declaration = server.language_features.references(
        doc,
        position(SOURCE, "handle", occurrence=1, after=1),
        include_declaration=True,
    )
    assert len(with_declaration) == 2


class CaptureServer(LspServer):
    def __init__(self):
        super().__init__()
        self.messages = []

    def send(self, payload):
        self.messages.append(payload)


def test_protocol():
    server = CaptureServer()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library.mln"
            caller = root / "caller.mln"
            library_source = (
                "package library;\n"
                "export i32 callback(i32 value) { return value; }\n"
            )
            library.write_text(library_source, encoding="utf-8")
            caller_source = (
                "package caller;\n"
                'import library from "library.mln";\n'
                "void init() { install(library.callback); }\n"
            )
            caller.write_text(caller_source, encoding="utf-8")
            server.handle({
                "id": 1,
                "method": "initialize",
                "params": {
                    "workspaceFolders": [{"uri": root.as_uri(), "name": "test"}],
                    "initializationOptions": {"semanticTokens": False},
                },
            })
            capabilities = server.messages[-1]["result"]["capabilities"]
            assert capabilities["referencesProvider"] is True

            server.handle({
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": library.as_uri(),
                        "version": 1,
                        "text": library_source,
                    }
                },
            })
            server.handle({
                "id": 2,
                "method": "textDocument/references",
                "params": {
                    "textDocument": {"uri": library.as_uri()},
                    "position": position(library_source, "callback", after=1),
                    "context": {"includeDeclaration": False},
                },
            })
            result = next(
                message["result"]
                for message in reversed(server.messages)
                if message.get("id") == 2
            )
            assert len(result) == 1
            assert result[0]["uri"] == caller.as_uri()
            assert result[0]["range"]["start"] == position(
                caller_source, "callback"
            )
    finally:
        server.stop_syntax_checker()


if __name__ == "__main__":
    run_with_server(test)
    test_protocol()
    print("[PASS] find references")
