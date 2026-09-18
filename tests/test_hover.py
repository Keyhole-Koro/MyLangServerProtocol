#!/usr/bin/env python3
"""Hover resolution, rendering, and UTF-16 range tests."""

from feature_test_support import position, run_with_server, snapshot


SOURCE = """/**
 * Adds values.
 * @param a First value.
 * @param b Second value.
 * @return Their sum.
 */
i32 add(i32 a, i32 b) { return a + b; }
i32 main() { char* label = "😀"; return add(1, 2); }
"""


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

    unresolved_source = "i32 main() { return unknown(1); }\n"
    unresolved = server.language_features.hover(
        snapshot(unresolved_source, "unresolved.mln"),
        position(unresolved_source, "unknown", after=1),
    )
    assert unresolved is None

    library_source = """package math;
/// Imported add.
/// @param value Input.
export i32 inc(i32 value) { return value + 1; }
"""
    caller_source = """package app;
import { inc } from "math.mln";
i32 main() { return inc(1); }
"""
    server.language_features.unit(snapshot(library_source, "math.mln"))
    caller = snapshot(caller_source, "caller.mln")
    imported = server.language_features.hover(
        caller,
        position(caller_source, "inc(1", after=1),
    )
    assert imported is not None
    assert "Imported add." in imported["contents"]["value"]

    package_source = """package io;
/// Writes a value.
export void write(i32 value) {}
"""
    qualified_source = """package app;
import io;
i32 main() { io.write(1); return 0; }
"""
    server.language_features.unit(snapshot(package_source, "io.mln"))
    qualified_doc = snapshot(qualified_source, "qualified.mln")
    qualified = server.language_features.hover(
        qualified_doc,
        position(qualified_source, "write(1", after=1),
    )
    assert qualified is not None
    assert "Writes a value." in qualified["contents"]["value"]


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] hover")
