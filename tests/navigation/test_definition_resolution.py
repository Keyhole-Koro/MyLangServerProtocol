#!/usr/bin/env python3
"""Definition targets must follow scopes, receiver types and lazy imports."""

from tests.support.lsp import FixtureWorkspace, LspTestServer, fixture_text, position, snapshot


def target(server, source, use, declaration, name, occurrence=0, use_after=1):
    doc = snapshot(source, name)
    location = server.language_features.definition(doc, position(source, use, occurrence, use_after))
    assert isinstance(location, dict), (name, use, location)
    assert location["uri"] == doc.uri
    assert location["range"]["start"] == position(source, declaration), (name, use, location)


def test_local(server):
    source = fixture_text("definition-resolution/receivers.mln")
    # An unrelated declaration or an inner shadow must not change a's type.
    for occurrence in [2, 3, 5, 6, 7, 8]:
        target(server, source, "get", "get() { return a", "receivers.mln", occurrence)
    target(server, source, "get", "get() { return b", "receivers.mln", 4)

    generic = fixture_text("definition-resolution/generic-chain.mln")
    target(server, generic, "take", "take() { return box", "generic-chain.mln", 2)
    target(server, generic, "get", "get() { return a", "generic-chain.mln", 1)

    const_generic = fixture_text("definition-resolution/const-generic.mln")
    target(server, const_generic, "InlineString", "InlineString<const", "const-generic.mln", 2)
    target(server, const_generic, "len", "len() { return", "const-generic.mln", 1)
    target(server, const_generic, "N", "N> {", "const-generic.mln", 1, 0)

    variants = fixture_text("definition-resolution/variants.mln")
    target(server, variants, "Ok", "Ok(T)", "variants.mln", 2)
    target(server, variants, "Ok", "Ok(i32)", "variants.mln", 3)

    shadow = fixture_text("definition-resolution/shadow.mln")
    doc = snapshot(shadow, "shadow-resolution.mln")
    for occurrence in [1, 2, 3, 4]:
        assert server.language_features.definition(doc, position(shadow, "handle", occurrence, 1)) is None
    target(server, shadow, "handle", "handle(i32 x)", "shadow-resolution.mln", 5)
    refs = server.language_features.references(doc, position(shadow, "handle", after=1), False)
    assert len(refs) == 1, refs


def test_imports():
    with FixtureWorkspace() as workspace:
        library = workspace.materialize("definition-resolution/library.mln")
        unused = workspace.materialize("definition-resolution/unused.mln")
        caller = workspace.materialize("definition-resolution/caller.mln")
        # Each request starts cold; success must not depend on earlier hovers.
        cases = [("get", 1, "get()"), ("get", 2, "get()"),
                 ("get", 3, "get()"), ("Counter", 0, "Counter {"),
                 ("Counter", 1, "Counter {"), ("Result", 0, "Result<T"),
                 ("Ok", 0, "Ok(T)")]
        for needle, occurrence, declaration in cases:
            server = LspTestServer()
            try:
                server.document_store.update(caller.uri, caller.source, 1)
                result = server.request(1, "textDocument/definition", {
                    "textDocument": {"uri": caller.uri},
                    "position": position(caller.source, needle, occurrence, 1),
                })["result"]
                assert isinstance(result, dict), (needle, occurrence, result)
                assert result["uri"] == library.uri, (needle, occurrence, result)
                assert result["range"]["start"] == position(library.source, declaration)
                assert server.workspace_index.get(unused.uri) is None
            finally:
                server.stop_syntax_checker()


def test_builtin_string_methods():
    with FixtureWorkspace() as workspace:
        library = workspace.materialize("definition-resolution/str.mln")
        caller = workspace.materialize("definition-resolution/str-caller.mln", "caller.mln")
        cases = [
            ("len", 0, "len()"),
            ("len", 1, "len()"),
            ("as_str", 0, "as_str()"),
            ("len", 2, "len()"),
            ("InlineString", 0, "InlineString<const"),
            ("to_string", 0, "to_string()"),
            ("as_str", 1, "as_str() { return str { data: (char*)"),
            ("len", 3, "len()"),
        ]
        for needle, occurrence, declaration in cases:
            server = LspTestServer()
            try:
                server.document_store.update(caller.uri, caller.source, 1)
                result = server.request(1, "textDocument/definition", {
                    "textDocument": {"uri": caller.uri},
                    "position": position(caller.source, needle, occurrence, 1),
                })["result"]
                assert isinstance(result, dict), (needle, occurrence, result)
                assert result["uri"] == library.uri
                assert result["range"]["start"] == position(library.source, declaration)
            finally:
                server.stop_syntax_checker()


if __name__ == "__main__":
    server = LspTestServer()
    try:
        test_local(server)
    finally:
        server.stop_syntax_checker()
    test_imports()
    test_builtin_string_methods()
    print("[PASS] scoped definition resolution and cold imports")
