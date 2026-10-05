#!/usr/bin/env python3
"""Hover resolution, rendering, and UTF-16 range tests."""

from lsp_testkit import fixture_text, position, run_with_server, snapshot


SOURCE = fixture_text("hover/basic.mln")


def test(server):
    doc = snapshot(SOURCE, "hover.mln")

    callee_position = position(SOURCE, "add(1", after=1)
    hover = server.language_features.hover(doc, callee_position)
    assert hover is not None
    markdown = hover["contents"]["value"]
    assert "add(i32 a, i32 b) -> i32" in markdown
    assert "Adds values." in markdown
    assert "`a` — First value." in markdown
    assert "Their sum." in markdown
    assert hover["range"]["start"] == position(SOURCE, "add(1")
    assert hover["range"]["end"]["character"] - hover["range"]["start"]["character"] == 3

    argument_position = position(SOURCE, "2);", after=0)
    argument_hover = server.language_features.hover(doc, argument_position)
    assert argument_hover is not None
    assert "i32 b" in argument_hover["contents"]["value"]
    assert "Second value." in argument_hover["contents"]["value"]

    unresolved_source = fixture_text("hover/unresolved.mln")
    unresolved = server.language_features.hover(
        snapshot(unresolved_source, "unresolved.mln"),
        position(unresolved_source, "unknown", after=1),
    )
    assert unresolved is None

    library_source = fixture_text("hover/math.mln")
    caller_source = fixture_text("hover/math-caller.mln")
    server.language_features.unit(snapshot(library_source, "math.mln"))
    caller = snapshot(caller_source, "caller.mln")
    imported = server.language_features.hover(
        caller,
        position(caller_source, "inc(1", after=1),
    )
    assert imported is not None
    assert "Imported add." in imported["contents"]["value"]

    package_source = fixture_text("hover/io.mln")
    qualified_source = fixture_text("hover/io-caller.mln")
    server.language_features.unit(snapshot(package_source, "io.mln"))
    qualified_doc = snapshot(qualified_source, "qualified.mln")
    qualified = server.language_features.hover(
        qualified_doc,
        position(qualified_source, "write(1", after=1),
    )
    assert qualified is not None
    assert "Writes a value." in qualified["contents"]["value"]

    structure_source = fixture_text("hover/structures.mln")
    structure_doc = snapshot(structure_source, "structures.mln")
    declared_struct = server.language_features.hover(
        structure_doc,
        position(structure_source, "Point {", after=1),
    )
    assert declared_struct is not None
    assert declared_struct["contents"]["value"] == """```mylang
struct Point {
    i32 x;
    i32 y;
}
```"""

    referenced_struct = server.language_features.hover(
        structure_doc,
        position(structure_source, "Point origin", after=1),
    )
    assert referenced_struct is not None
    assert referenced_struct["contents"]["value"] == declared_struct["contents"]["value"]

    generic_struct = server.language_features.hover(
        structure_doc,
        position(structure_source, "Box<Point>", after=1),
    )
    assert generic_struct is not None
    assert generic_struct["contents"]["value"] == """```mylang
struct Box<T> {
    T value;
    i32 count;
}
```"""

    structure_library = fixture_text("hover/shapes.mln")
    structure_caller = fixture_text("hover/shapes-caller.mln")
    server.language_features.unit(snapshot(structure_library, "shapes.mln"))
    imported_struct = server.language_features.hover(
        snapshot(structure_caller, "shapes-caller.mln"),
        position(structure_caller, "Rect bounds", after=1),
    )
    assert imported_struct is not None
    assert "struct Rect {" in imported_struct["contents"]["value"]
    assert "i32 width;" in imported_struct["contents"]["value"]


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] hover")
