#!/usr/bin/env python3
"""Traits: impl items as call targets, trait hover, and go-to-implementation."""

import shutil
import tempfile
from pathlib import Path

from tests.support.lsp import FIXTURE_ROOT, position, run_with_server


def location_start(location):
    assert isinstance(location, dict), location
    return location["uri"], location["range"]["start"]


def test(server):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(FIXTURE_ROOT / "traits", root, dirs_exist_ok=True)
        main = root / "main.mln"
        library = root / "lib" / "cmp.mln"
        source = main.read_text(encoding="utf-8")
        library_source = library.read_text(encoding="utf-8")
        server.workspace_roots = [root]
        server.open_document(main.as_uri(), source)

        def request(method, needle, occurrence=0, after=1, request_id=[0]):
            request_id[0] += 1
            return server.request(request_id[0], method, {
                "textDocument": {"uri": main.as_uri()},
                "position": position(source, needle, occurrence, after),
            })["result"]

        # A method written with Self receives the impl's type, so a call on a
        # Point reaches the impl item.
        uri, start = location_start(request("textDocument/definition", "eq(&b)"))
        assert uri == main.as_uri()
        assert start == position(source, "eq(Self")

        # Type::from reaches the associated function of that type's impl.
        targets = request("textDocument/definition", "from(3)")
        assert isinstance(targets, dict), targets
        assert targets["range"]["start"] == position(source, "from(i32")

        # An `impl ... = value;` has no item to point at; its header is the
        # definition.
        uri, start = location_start(request("textDocument/definition", "from(Code::A)"))
        assert start == position(source, "From<Code> for Mode")

        # A trait name in an impl header jumps through the @lib alias.
        uri, start = location_start(request("textDocument/definition", "Eq for"))
        assert uri == library.as_uri()
        assert start == position(library_source, "Eq {")

        # Hovering the trait shows its contract, doc, and known impls.
        hover = request("textDocument/hover", "Eq for")["contents"]["value"]
        assert "trait Eq {" in hover, hover
        assert "bool (Self *self) eq(Self *other);" in hover, hover
        assert "Equality between two values" in hover, hover
        assert "`impl Eq for Point`" in hover, hover

        # Hovering a call names the impl and borrows the trait item's doc.
        hover = request("textDocument/hover", "eq(&b)")["contents"]["value"]
        assert "`impl Eq for Point`" in hover, hover
        assert "True when both values are equal." in hover, hover

        # Implementations of a trait from its declaration.
        server.open_document(library.as_uri(), library_source)
        result = server.request(100, "textDocument/implementation", {
            "textDocument": {"uri": library.as_uri()},
            "position": position(library_source, "Eq {", after=1),
        })["result"]
        uri, start = location_start(result)
        assert uri == main.as_uri()
        assert start == position(source, "Eq for")

        # Implementations of a trait item.
        result = server.request(101, "textDocument/implementation", {
            "textDocument": {"uri": library.as_uri()},
            "position": position(library_source, "eq(Self", after=1),
        })["result"]
        uri, start = location_start(result)
        assert start == position(source, "eq(Self")

        result = server.request(102, "textDocument/implementation", {
            "textDocument": {"uri": library.as_uri()},
            "position": position(library_source, "from(S", after=1),
        })["result"]
        assert isinstance(result, dict) and result["range"]["start"] == position(source, "from(i32")
    print("[PASS] traits: impl calls, trait hover, implementations")


if __name__ == "__main__":
    run_with_server(test)
