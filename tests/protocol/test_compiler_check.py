#!/usr/bin/env python3
"""What `mlc --check` reports for a saved source reaches the editor."""

import tempfile
from pathlib import Path

from tests.support.lsp import LspTestServer
from mylang_lsp.features.compiler_check import CompilerCheck, parse_report


CLEAN = "i32 main() { return 0; }\n"
SEMANTIC_ERROR = "i32 main() {\n    return nope;\n}\n"
SYNTAX_ERROR = "i32 main() { return 0 }\n"


def latest_diagnostics(server, uri):
    published = [message["params"]["diagnostics"] for message in server.messages
                 if message.get("method") == "textDocument/publishDiagnostics"
                 and message["params"]["uri"] == uri]
    return published[-1]


def write(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path, path.as_uri()


def edit(server, uri, text, version):
    server.notify("textDocument/didChange", {
        "textDocument": {"uri": uri, "version": version},
        "contentChanges": [{"text": text}],
    })


def started_server():
    server = LspTestServer()
    server.request(1, "initialize", {"initializationOptions": {"semanticTokens": False}})
    return server


def test_report_follows_the_file_on_disk(directory):
    server = started_server()
    try:
        capabilities = server.request(2, "initialize", {
            "initializationOptions": {"semanticTokens": False},
        })["result"]["capabilities"]
        assert capabilities["textDocumentSync"]["save"] == {"includeText": False}

        path, uri = write(directory, "report.mln", SEMANTIC_ERROR)
        server.open_document(uri, SEMANTIC_ERROR)
        [diagnostic] = latest_diagnostics(server, uri)
        assert diagnostic["source"] == "mlc"
        assert diagnostic["code"] == "E0001"
        assert diagnostic["severity"] == 1
        assert "undefined identifier 'nope'" in diagnostic["message"]
        assert diagnostic["range"] == {
            "start": {"line": 1, "character": 11},
            "end": {"line": 1, "character": 15},
        }

        # An edit makes the report stale, and the file on disk is the old text.
        edit(server, uri, CLEAN, 2)
        assert latest_diagnostics(server, uri) == []
        server.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        assert latest_diagnostics(server, uri) == []

        path.write_text(CLEAN, encoding="utf-8")
        server.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        assert latest_diagnostics(server, uri) == []

        edit(server, uri, SEMANTIC_ERROR, 3)
        assert latest_diagnostics(server, uri) == []
        path.write_text(SEMANTIC_ERROR, encoding="utf-8")
        server.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        assert [d["source"] for d in latest_diagnostics(server, uri)] == ["mlc"]
    finally:
        server.stop_syntax_checker()


def test_syntax_error_is_shown_once(directory):
    server = started_server()
    try:
        _path, uri = write(directory, "syntax.mln", SYNTAX_ERROR)
        server.open_document(uri, SYNTAX_ERROR)
        [diagnostic] = latest_diagnostics(server, uri)
        assert diagnostic["source"] == "mylang"
    finally:
        server.stop_syntax_checker()


def test_unsaved_or_missing_source_is_not_checked(directory):
    server = started_server()
    try:
        _path, uri = write(directory, "unsaved.mln", CLEAN)
        server.open_document(uri, SEMANTIC_ERROR)
        assert latest_diagnostics(server, uri) == []

        missing = (directory / "missing.mln").as_uri()
        server.open_document(missing, SEMANTIC_ERROR)
        assert latest_diagnostics(server, missing) == []
    finally:
        server.stop_syntax_checker()


def test_compiler_that_cannot_run_is_reported_once(directory):
    server = started_server()
    try:
        server.compiler_check = CompilerCheck(directory / "no-such-compiler")
        for name in ("first.mln", "second.mln"):
            _path, uri = write(directory, name, SEMANTIC_ERROR)
            server.open_document(uri, SEMANTIC_ERROR)
            assert latest_diagnostics(server, uri) == []
        warnings = [message for message in server.messages
                    if message.get("method") == "window/showMessage"]
        assert len(warnings) == 1
        assert "no-such-compiler" in warnings[0]["params"]["message"]
    finally:
        server.stop_syntax_checker()


def test_report_lines(directory):
    path = directory / "main.mln"
    text = "// é\ni32 x;\n"
    output = "\n".join([
        f"{path}:2:5: error[E0301]: wrong [range 2:5-2:6]",
        "main.mln:1:1: warning[W0001]: unreachable statement",
        "lib.mln:3:4: error: expected ';'",
        "  i32 x",
        "main.mln:1:1: note: declared here",
    ])
    first, second, third = parse_report(output, path, text)
    assert first["code"] == "E0301" and first["severity"] == 1
    assert first["range"]["end"] == {"line": 1, "character": 5}
    assert second["severity"] == 2 and second["message"] == "unreachable statement"
    assert third["range"]["start"] == {"line": 0, "character": 0}
    assert third["message"] == "lib.mln:3:4: expected ';'"


def run():
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        test_report_follows_the_file_on_disk(directory)
        test_syntax_error_is_shown_once(directory)
        test_unsaved_or_missing_source_is_not_checked(directory)
        test_compiler_that_cannot_run_is_reported_once(directory)
        test_report_lines(directory)
    print("[PASS] compiler check")


if __name__ == "__main__":
    run()
