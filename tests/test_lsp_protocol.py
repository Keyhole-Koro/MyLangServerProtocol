#!/usr/bin/env python3
"""Protocol capability and interactive request integration tests."""

import tempfile
from pathlib import Path

from feature_test_support import LspServer, position


class CaptureServer(LspServer):
    def __init__(self):
        super().__init__()
        self.messages = []

    def send(self, payload):
        self.messages.append(payload)


SOURCE = """/// Adds.
/// @param a First.
/// @param b Second.
i32 add(i32 a, i32 b) { return a + b; }
i32 main() { return add(1, 2); }
"""


def response(server, request_id):
    return next(message for message in reversed(server.messages) if message.get("id") == request_id)


def run():
    server = CaptureServer()
    uri = "file:///tmp/protocol.mln"
    try:
        server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"initializationOptions": {"semanticTokens": False}},
        })
        capabilities = response(server, 1)["result"]["capabilities"]
        assert capabilities["positionEncoding"] == "utf-16"
        assert capabilities["hoverProvider"] is True
        assert capabilities["definitionProvider"] is True
        assert capabilities["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]
        assert "semanticTokensProvider" not in capabilities
        assert server.message_priority("textDocument/hover") < server.message_priority(
            "textDocument/documentSymbol"
        )

        server.handle({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": "mylang",
                    "version": 1,
                    "text": SOURCE,
                }
            },
        })
        assert server.get_doc(uri).version == 1
        # With semantic tokens disabled, diagnostics parse immediately, while
        # documentation/index construction remains lazy until hover.
        assert len(server.syntax_result_cache) == 1
        assert server.workspace_index.get(uri) is None

        server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1", after=1),
            },
        })
        assert response(server, 2)["result"]["contents"]["value"] == "Loading..."
        assert any(message.get("method") == "mylang/hoverReady" for message in server.messages)
        server.handle({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1", after=1),
            },
        })
        assert "add(i32 a, i32 b) -> i32" in response(server, 20)["result"]["contents"]["value"]

        server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "textDocument/signatureHelp",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1, 2)", after=len("add(1, ")),
            },
        })
        assert response(server, 3)["result"]["activeParameter"] == 1

        with server.cancel_lock:
            server.cancelled_requests.add(99)
        server.handle_queued({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1", after=1),
            },
        })
        assert response(server, 99)["error"]["code"] == -32800

        changed = SOURCE.replace("add(1, 2)", "add(1, 3)")
        server.handle({
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": changed}],
            },
        })
        assert server.get_doc(uri).version == 2
        assert server.get_doc(uri).text == changed

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library_path = root / "library.mln"
            unused_path = root / "unused.mln"
            caller_path = root / "caller.mln"
            library_path.write_text(
                "package library;\n"
                "/// Imported function.\n"
                "export i32 inc(i32 value) { return value + 1; }\n"
                "export enum Status { READY };\n"
                "export struct Item { i32 value; };\n",
                encoding="utf-8",
            )
            unused_path.write_text(
                "package unused;\n"
                "/// Must stay lazy when hovering inc.\n"
                "export i32 untouched() { return 0; }\n",
                encoding="utf-8",
            )
            caller_source = (
                "package caller;\n"
                'import { inc, Status, Item } from "library.mln";\n'
                'import unused from "unused.mln";\n'
                "i32 main() { Item item; Status status = Status::READY; return inc(1); }\n"
            )
            caller_path.write_text(caller_source, encoding="utf-8")
            caller_uri = caller_path.as_uri()
            library_uri = library_path.as_uri()
            unused_uri = unused_path.as_uri()
            server.handle({
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": caller_uri,
                        "languageId": "mylang",
                        "version": 1,
                        "text": caller_source,
                    }
                },
            })
            # didOpen indexes only the current file. Dependencies are kept lazy
            # so unrelated import graphs cannot block interactive requests.
            assert server.workspace_index.get(library_uri) is None
            assert server.workspace_index.get(unused_uri) is None
            server.handle({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "inc(1", after=1),
                },
            })
            assert response(server, 4)["result"]["contents"]["value"] == "Loading..."
            assert server.workspace_index.get(library_uri) is not None
            assert server.workspace_index.get(unused_uri) is None
            server.handle({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "inc(1", after=1),
                },
            })
            assert "Imported function." in response(server, 5)["result"]["contents"]["value"]
            server.handle({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "inc(1", after=1),
                },
            })
            definition = response(server, 6)["result"]
            assert definition["uri"] == library_uri
            assert definition["range"]["start"] == {"line": 2, "character": 11}
            for request_id, needle, occurrence, expected in [
                (7, "Item", 1, {"line": 4, "character": 14}),
                (8, "Status", 1, {"line": 3, "character": 12}),
                (9, "READY", 0, {"line": 3, "character": 21}),
            ]:
                server.handle({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "textDocument/definition",
                    "params": {
                        "textDocument": {"uri": caller_uri},
                        "position": position(
                            caller_source, needle, occurrence=occurrence, after=1
                        ),
                    },
                })
                imported_definition = response(server, request_id)["result"]
                assert imported_definition["uri"] == library_uri
                assert imported_definition["range"]["start"] == expected
    finally:
        server.stop_syntax_checker()

    color_server = CaptureServer()
    color_uri = "file:///tmp/color-first.mln"
    try:
        color_server.handle({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {"initializationOptions": {"semanticTokens": True}},
        })
        color_server.handle({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": color_uri,
                    "languageId": "mylang",
                    "version": 1,
                    "text": SOURCE,
                }
            },
        })
        assert len(color_server.syntax_result_cache) == 0
        assert color_server.workspace_index.get(color_uri) is None
        color_server.handle({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "textDocument/semanticTokens/full",
            "params": {"textDocument": {"uri": color_uri}},
        })
        color_index = next(
            index for index, message in enumerate(color_server.messages)
            if message.get("id") == 11
        )
        diagnostics_index = next(
            index for index, message in enumerate(color_server.messages)
            if message.get("method") == "textDocument/publishDiagnostics"
        )
        assert color_index < diagnostics_index
        assert color_server.workspace_index.get(color_uri) is None
    finally:
        color_server.stop_syntax_checker()


if __name__ == "__main__":
    run()
    print("[PASS] LSP protocol integration")
