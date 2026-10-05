#!/usr/bin/env python3
"""Definition and reference resolution for function-pointer uses."""

from tests.support.lsp import (
    FixtureWorkspace,
    LspTestServer,
    fixture_text,
    position,
    run_with_server,
    snapshot,
)


SOURCE = fixture_text("references/basic.mln")


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


def test_protocol():
    server = LspTestServer()
    try:
        with FixtureWorkspace() as workspace:
            library = workspace.materialize("references/library.mln")
            caller = workspace.materialize("references/caller.mln")
            initialized = server.request(1, "initialize", {
                "workspaceFolders": [{"uri": workspace.root.as_uri(), "name": "test"}],
                "initializationOptions": {"semanticTokens": False},
            })
            capabilities = initialized["result"]["capabilities"]
            assert capabilities["referencesProvider"] is True

            server.open_document(library.uri, library.source)
            result = server.request(2, "textDocument/references", {
                "textDocument": {"uri": library.uri},
                "position": position(library.source, "callback", after=1),
                "context": {"includeDeclaration": False},
            })["result"]
            assert len(result) == 1
            assert result[0]["uri"] == caller.uri
            assert result[0]["range"]["start"] == position(
                caller.source, "callback"
            )
    finally:
        server.stop_syntax_checker()


if __name__ == "__main__":
    run_with_server(test)
    test_protocol()
    print("[PASS] find references")
