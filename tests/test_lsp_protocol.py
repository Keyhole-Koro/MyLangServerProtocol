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
        assert capabilities["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]
        assert "semanticTokensProvider" not in capabilities

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

        server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1", after=1),
            },
        })
        assert "add(i32 a, i32 b) -> i32" in response(server, 2)["result"]["contents"]["value"]

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
            caller_path = root / "caller.mln"
            library_path.write_text(
                "package library;\n"
                "/// Imported function.\n"
                "export i32 inc(i32 value) { return value + 1; }\n",
                encoding="utf-8",
            )
            caller_source = (
                "package caller;\n"
                'import { inc } from "library.mln";\n'
                "i32 main() { return inc(1); }\n"
            )
            caller_path.write_text(caller_source, encoding="utf-8")
            caller_uri = caller_path.as_uri()
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
            server.handle({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "inc(1", after=1),
                },
            })
            assert "Imported function." in response(server, 4)["result"]["contents"]["value"]
    finally:
        server.stop_syntax_checker()


if __name__ == "__main__":
    run()
    print("[PASS] LSP protocol integration")
