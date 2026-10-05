#!/usr/bin/env python3
"""Protocol capability and interactive request integration tests."""

from tests.support.lsp import FixtureWorkspace, LspTestServer, fixture_text, position


SOURCE = fixture_text("protocol/basic.mln")


def run():
    server = LspTestServer()
    uri = "file:///tmp/protocol.mln"
    try:
        initialized = server.request(1, "initialize", {
            "initializationOptions": {"semanticTokens": False},
        })
        capabilities = initialized["result"]["capabilities"]
        assert capabilities["positionEncoding"] == "utf-16"
        assert capabilities["hoverProvider"] is True
        assert capabilities["definitionProvider"] is True
        assert capabilities["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]
        assert "semanticTokensProvider" not in capabilities
        assert server.message_priority("textDocument/hover") < server.message_priority(
            "textDocument/documentSymbol"
        )

        server.open_document(uri, SOURCE)
        assert server.get_doc(uri).version == 1
        # With semantic tokens disabled, diagnostics parse immediately, while
        # documentation/index construction remains lazy until hover.
        assert len(server.syntax_result_cache) == 1
        assert server.workspace_index.get(uri) is None

        hover = server.request(2, "textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": position(SOURCE, "add(1", after=1),
        })
        assert hover["result"]["contents"]["value"] == "Loading..."
        assert any(message.get("method") == "mylang/hoverReady" for message in server.messages)
        hover = server.request(20, "textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": position(SOURCE, "add(1", after=1),
        })
        assert "add(i32 a, i32 b) -> i32" in hover["result"]["contents"]["value"]

        signature = server.request(3, "textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": position(SOURCE, "add(1, 2)", after=len("add(1, ")),
        })
        assert signature["result"]["activeParameter"] == 1

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
        assert server.response(99)["error"]["code"] == -32800

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

        with FixtureWorkspace() as workspace:
            library = workspace.materialize("protocol/library.mln")
            unused = workspace.materialize("protocol/unused.mln")
            caller = workspace.materialize("protocol/caller.mln")
            caller_uri = caller.uri
            library_uri = library.uri
            unused_uri = unused.uri
            caller_source = caller.source
            server.open_document(caller_uri, caller_source)
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
            assert server.response(4)["result"]["contents"]["value"] == "Loading..."
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
            assert "Imported function." in server.response(5)["result"]["contents"]["value"]
            server.handle({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "inc(1", after=1),
                },
            })
            definition = server.response(6)["result"]
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
                imported_definition = server.response(request_id)["result"]
                assert imported_definition["uri"] == library_uri
                assert imported_definition["range"]["start"] == expected

            # A type Hover loads only the library that defines that type, just
            # like a function Hover. Removing the indexed library makes this
            # exercise the lazy path rather than the already-loaded result.
            server.workspace_index.remove(library_uri)
            server.handle({
                "jsonrpc": "2.0",
                "id": 21,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "Item item", after=1),
                },
            })
            assert server.response(21)["result"]["contents"]["value"] == "Loading..."
            assert server.workspace_index.get(library_uri) is not None
            server.handle({
                "jsonrpc": "2.0",
                "id": 22,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": caller_uri},
                    "position": position(caller_source, "Item item", after=1),
                },
            })
            item_hover = server.response(22)["result"]
            assert "struct Item {" in item_hover["contents"]["value"]
            assert "i32 value;" in item_hover["contents"]["value"]
    finally:
        server.stop_syntax_checker()

    color_server = LspTestServer()
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
