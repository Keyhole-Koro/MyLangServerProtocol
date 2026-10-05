#!/usr/bin/env python3
"""Signature Help call-context and active-parameter tests."""

from feature_test_support import fixture_text, position, run_with_server, snapshot


SOURCE = fixture_text("signature/basic.mln")


def signature(server, doc, marker, after):
    result = server.language_features.signature_help(doc, position(SOURCE, marker, after=after))
    assert result is not None
    return result


def test(server):
    doc = snapshot(SOURCE, "signature.mln")

    nested = signature(server, doc, "inner(1, 2)", len("inner(1, "))
    assert nested["signatures"][0]["label"] == "inner(i32 a, i32 b) -> i32"
    assert nested["activeParameter"] == 1

    outer = signature(server, doc, "outer(inner(1, 2), 3)", len("outer(inner(1, 2), "))
    assert outer["signatures"][0]["label"] == "outer(i32 x, i32 y) -> i32"
    assert outer["activeParameter"] == 1

    rest = signature(server, doc, "log(1, 2, 3)", len("log(1, 2, "))
    assert rest["signatures"][0]["label"] == "log(rest values) -> void"
    assert rest["activeParameter"] == 0

    method = signature(server, doc, "user.show(1)", len("user.show("))
    assert method["signatures"][0]["label"] == "show(i32 value) -> void"
    assert method["activeParameter"] == 0
    assert len(method["signatures"][0]["parameters"]) == 1

    admin_method = signature(server, doc, 'admin.show("ok")', len("admin.show("))
    assert admin_method["signatures"][0]["label"] == "show(char* text) -> void"
    assert admin_method["activeParameter"] == 0

    incomplete_source = fixture_text("signature/incomplete.mln")
    incomplete = server.language_features.signature_help(
        snapshot(incomplete_source, "incomplete.mln"),
        position(incomplete_source, "add(1, ", after=len("add(1, ")),
    )
    assert incomplete is not None
    assert incomplete["activeParameter"] == 1

    protected_source = fixture_text("signature/protected.mln")
    protected = server.language_features.signature_help(
        snapshot(protected_source, "protected.mln"),
        position(protected_source, '"a,b"', after=2),
    )
    assert protected is not None
    assert protected["activeParameter"] == 1


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] signature help")
