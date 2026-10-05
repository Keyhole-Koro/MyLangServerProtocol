#!/usr/bin/env python3
"""Documentation extraction and frontend metadata tests."""

from lsp_testkit import fixture_text, run_with_server, snapshot


SOURCE = fixture_text("doc-comments.mln")


def test(server):
    unit = server.language_features.unit(snapshot(SOURCE, "docs.mln"))
    functions = {function.name: function for function in unit.functions}
    assert set(functions) == {"add", "plain", "log", "take", "one"}

    add = functions["add"]
    assert add.package == "math"
    assert add.is_exported
    assert add.signature == "add<T>(mut i32 a, i32 b) -> i32"
    assert [parameter.name for parameter in add.parameters] == ["a", "b"]
    assert add.doc.summary == "Adds values."
    assert add.doc.body == "Signed arithmetic."
    assert add.doc.param_docs["a"].text == "First value."
    assert add.doc.param_docs["b"].text == "Second value."
    assert add.doc.param_docs["missing"].text == "Ignored."
    assert add.doc.return_doc == "Their sum."
    assert add.doc.comment_span is not None
    assert SOURCE.encode("utf-8")[add.doc.comment_span.start:add.doc.comment_span.end].startswith(b"/**")

    assert functions["plain"].doc.summary == ""
    log = functions["log"]
    assert log.signature == "log(rest values) -> void"
    assert log.parameters[0].is_rest
    assert log.doc.param_docs["values"].text == "Values to log."

    take = functions["take"]
    assert [parameter.name for parameter in take.parameters] == ["value", "next"]
    assert take.parameters[0].label == "Pair<i32, i32> value"
    assert functions["one"].doc.summary == "One-line docs."


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] documentation extraction")
