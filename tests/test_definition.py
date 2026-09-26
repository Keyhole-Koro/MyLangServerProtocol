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

    ambiguous_source = """enum First { ITEM };
enum Second { ITEM };
enum Duplicate { VALUE };
enum Duplicate { VALUE };
i32 main() {
    First first = First::ITEM;
    Second second = Second::ITEM;
    Duplicate duplicate = Duplicate::VALUE;
    return 0;
}
"""
    ambiguous = snapshot(ambiguous_source, "ambiguous-definition.mln")

    # The enum qualifier disambiguates identical member names.
    first_item = server.language_features.definition(
        ambiguous,
        position(ambiguous_source, "ITEM", occurrence=2, after=1),
    )
    second_item = server.language_features.definition(
        ambiguous,
        position(ambiguous_source, "ITEM", occurrence=3, after=1),
    )
    assert isinstance(first_item, dict)
    assert isinstance(second_item, dict)
    assert first_item["range"]["start"] == position(
        ambiguous_source, "ITEM", occurrence=0
    )
    assert second_item["range"]["start"] == position(
        ambiguous_source, "ITEM", occurrence=1
    )

    # LSP accepts multiple Locations; VS Code presents these as selectable
    # definition targets instead of silently refusing to navigate.
    duplicate_value = server.language_features.definition(
        ambiguous,
        position(ambiguous_source, "VALUE", occurrence=2, after=1),
    )
    assert isinstance(duplicate_value, list)
    assert len(duplicate_value) == 2


if __name__ == "__main__":
    run_with_server(test)
    print("[PASS] go to definition")
