#!/usr/bin/env python3
"""Go-to-definition resolution for functions and named declarations."""

from feature_test_support import position, run_with_server, snapshot


SOURCE = """package demo;
enum Color { RED, GREEN };
struct Point { i32 x; };
typedef i32 Count;
i32 add(i32 a, i32 b) { return a + b; }
i32 main() {
    char* marker = "😀";
    Point point;
    Color color = Color::RED;
    Count count = add(1, 2);
    return count;
}
"""


def test(server):
    doc = snapshot(SOURCE, "definition.mln")
    server.language_features.unit(doc)

    cases = [
        ("add", 1, "add", 0),
        ("Point", 1, "Point", 0),
        ("Color", 1, "Color", 0),
        ("Count", 1, "Count", 0),
        ("RED", 1, "RED", 0),
    ]
    for use, use_occurrence, declaration, declaration_occurrence in cases:
        location = server.language_features.definition(
            doc,
            position(SOURCE, use, occurrence=use_occurrence, after=1),
        )
        assert location is not None, (use, use_occurrence)
        assert location["uri"] == doc.uri
        assert location["range"]["start"] == position(
            SOURCE, declaration, occurrence=declaration_occurrence
        )

    unresolved_source = "i32 main() { return missing(); }\n"
    unresolved = snapshot(unresolved_source, "unresolved-definition.mln")
    assert server.language_features.definition(
        unresolved,
        position(unresolved_source, "missing", after=1),
    ) is None


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] go to definition")
