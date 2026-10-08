#!/usr/bin/env python3
"""Generic impls, default items, and dyn values in navigation and hover."""

import shutil
import tempfile
from pathlib import Path

from tests.support.lsp import FIXTURE_ROOT, position, run_with_server


def test(server):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(FIXTURE_ROOT / "traits_generic", root, dirs_exist_ok=True)
        main = root / "generic.mln"
        source = main.read_text(encoding="utf-8")
        server.workspace_roots = [root]
        server.open_document(main.as_uri(), source)

        def request(method, needle, occurrence=0, after=1, request_id=[200]):
            request_id[0] += 1
            return server.request(request_id[0], method, {
                "textDocument": {"uri": main.as_uri()},
                "position": position(source, needle, occurrence, after),
            })["result"]

        # A method called on a dyn value is the trait's item.
        target = request("textDocument/definition", "area();\n    return")
        assert isinstance(target, dict), target
        assert target["range"]["start"] == position(source, "area();")

        # A default item is a definition like any other.
        target = request("textDocument/definition", "doubled();")
        assert isinstance(target, dict), target
        assert target["range"]["start"] == position(source, "doubled() {")

        # `impl<T: Eq> Eq for Box<T>` is an impl of Eq for Box<T>.
        hover = request("textDocument/hover", "eq(Box")["contents"]["value"]
        assert "`impl Eq for Box<T>`" in hover, hover
    print("[PASS] traits: generic impls, default items, dyn calls")


if __name__ == "__main__":
    run_with_server(test)
