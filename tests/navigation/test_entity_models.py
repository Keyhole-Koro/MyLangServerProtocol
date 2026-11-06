#!/usr/bin/env python3
"""A `.model.mln` is usable in an editor, and so is what it generates."""

import subprocess
from pathlib import Path

from server import TOKEN_TYPES
from tests.support.lsp import (
    FixtureWorkspace,
    LspTestServer,
    fixture_text,
    position,
)


MODEL = "file:///tmp/world.model.mln"
REPO_ROOT = Path(__file__).resolve().parents[4]


def tokens_by_line(server, uri, text):
    """Decode semantic tokens into {line: [(text, type), ...]}."""
    encoded = server.semantic_tokens_for_uri(uri, text)
    lines = text.split("\n")
    decoded = {}
    line = column = 0
    for index in range(0, len(encoded), 5):
        delta_line, delta_column, length, token_type, _modifiers = encoded[index:index + 5]
        line += delta_line
        column = delta_column if delta_line else column + delta_column
        decoded.setdefault(line, []).append(
            (lines[line][column:column + length], TOKEN_TYPES[token_type])
        )
    return decoded


def line_of(source, needle):
    return position(source, needle)["line"]


def diagnostics_for(source):
    server = LspTestServer()
    try:
        return server.syntax_diagnostics_for_uri(MODEL, source)
    finally:
        server.stop_syntax_checker()


def test_fixtures_are_mylang():
    """What the compiler refuses proves nothing about what an editor shows."""
    toolchain = REPO_ROOT / "toolchain"
    compiler = toolchain / "MyLangCompiler" / "mlc"
    if not compiler.exists():
        return
    with FixtureWorkspace() as workspace:
        for name in ("world.model.mln", "caller.mln", "chained.mln"):
            workspace.materialize(f"entity/{name}")
        for name in ("caller", "chained"):
            completed = subprocess.run(
                [compiler, "--alias", f"@std={toolchain / 'MyStdLib'}",
                 f"{name}.mln", f"{name}.masm"],
                cwd=workspace.root, capture_output=True, text=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr


def test_no_false_diagnostics():
    assert diagnostics_for(fixture_text("entity/world.model.mln")) == []
    # `Text` in a column is why the imports stay part of what is parsed.
    assert diagnostics_for("package p;\nentity A max 4 { i32 x; }") == []
    assert diagnostics_for("") == []
    for path in [
        "system/MyOS/src/ui/dom/dom_model.model.mln",
        "toolchain/MyLangCompiler/tests/succeed/model/ui.model.mln",
        "toolchain/MyLangCompiler/tests/succeed/model/more.model.mln",
    ]:
        assert diagnostics_for((REPO_ROOT / path).read_text(encoding="utf-8")) == [], path


GOOD = "package p;\nentity A max 4 {\n    i32 x;\n    i32 y;\n}\nentity B max 4 { i32 z; }\n"


def test_real_errors():
    cases = [
        # The generator's own checks, each on the line that is wrong.
        (GOOD + "A has many C as cs;\n", "C as", "unknown entity 'C'"),
        (GOOD + "C has many A as cs;\n", "C has", "unknown entity 'C'"),
        (GOOD + "A has lots B as bs;\n", "lots", "expected `one` or `many`"),
        (GOOD + "A has many B to bs;\n", "to bs", "expected `as name`"),
        (GOOD + "A has many B as bs / many;\n", "many;", "many-to-many"),
        (GOOD + "A has many B as bs, sorted;\n", "sorted", "unknown relation option"),
        (GOOD + "A has many B as bs\n", "A has", "expected ';'"),
        (GOOD + "index A.w;\n", "w;", "has no column 'w'"),
        (GOOD + "index C.x;\n", "C.x", "unknown entity 'C'"),
        (GOOD + "index A;\n", "index", "expected Entity.column"),
        (GOOD.replace("i32 y", "bool y") + "unique A.y;\n", "A.y", "only an i32 column"),
        ("entity max 4 { i32 x; }\n", "max", "needs `max N`"),
        ("entity A max 9999 { i32 x; }\n", "9999", "max must be between"),
        ("entity A 4 { i32 x; }\n", "A", "needs `max N`"),
        ("entity A max 4 i32 x;\n", "i32", "expected '{'"),
        ("package p;\nentity A max 4 {\n    i32 x;\n    ;\n}\n", ";\n}", "malformed column"),
        # A line that is no declaration stays MyLang, and the parser finds it.
        (GOOD + "A hsa many B as bs;\n", "hsa", "Expected"),
        (GOOD + "garbage here there;\n", "here", "Expected"),
        (GOOD.replace("i32 y;", "i32 y"), "}", "Expected"),
        (GOOD + "i32 broken( { return 0; }\n", "{ return", "Expected"),
        # Only the generated code shows it, on the word that named it.
        (GOOD + "A has many B as for;\n", "for", "Expected"),
    ]
    for source, needle, message in cases:
        found = diagnostics_for(source)
        assert len(found) == 1, (source, found)
        assert found[0]["range"]["start"]["line"] == line_of(source, needle), (source, found)
        assert message in found[0]["message"], (source, found)


def test_error_marks_the_word():
    source = GOOD + "A has many Missing as ms;\n"
    assert diagnostics_for(source)[0]["range"] == {
        "start": position(source, "Missing"),
        "end": position(source, "Missing", after=len("Missing")),
    }
    # One report for each bad statement, and the good ones between are fine.
    source = GOOD + "A has many C as cs;\nA has many B as bs;\nindex B.w;\n"
    found = diagnostics_for(source)
    assert [item["range"]["start"]["line"] for item in found] == [
        line_of(source, "C as"), line_of(source, "index")
    ], found


def test_errors_keep_the_server_going():
    server = LspTestServer()
    try:
        broken = GOOD + "A has many C as cs;\n"
        assert server.syntax_diagnostics_for_uri(MODEL, broken)
        process = server.native_frontend.process
        assert server.syntax_diagnostics_for_uri(MODEL, GOOD) == []
        assert server.native_frontend.process is process and process.poll() is None
        # The words either side of a bad statement stay highlighted.
        source = "package p;\nentity A max 4 { i32 x; }\nA has many C as cs;\nA knows one A as next;\n"
        decoded = tokens_by_line(server, MODEL, source)
        assert ("entity", "keyword") in decoded[1]
        assert ("has", "keyword") in decoded[2]
        assert ("knows", "keyword") in decoded[3]
        assert ("next", "method") in decoded[3]

        # Source text reaches the JSON, even where it cannot be MyLang: a
        # control character, and a column the generator cuts inside a character.
        for awkward in ("i32\x01 x;", "i32 /* " + "\u65e5" * 40 + " */ x;"):
            server.syntax_diagnostics_for_uri(MODEL, f"entity A max 4 {{ {awkward} }}\n")
            assert server.native_frontend.process is process and process.poll() is None, awkward
    finally:
        server.stop_syntax_checker()


def test_highlighting():
    source = fixture_text("entity/world.model.mln")
    server = LspTestServer()
    try:
        decoded = tokens_by_line(server, MODEL, source)
        assert decoded[line_of(source, "entity Node")][:4] == [
            ("entity", "keyword"), ("Node", "struct"), ("max", "keyword"), ("64", "number"),
        ]
        assert decoded[line_of(source, "Node   has")] == [
            ("Node", "struct"), ("has", "keyword"), ("many", "keyword"), ("Node", "struct"),
            ("as", "keyword"), ("children", "method"), ("/", "operator"),
            ("parent", "method"), ("ordered", "keyword"),
        ]
        assert decoded[line_of(source, "Window knows one")] == [
            ("Window", "struct"), ("knows", "keyword"), ("one", "keyword"),
            ("Node", "struct"), ("as", "keyword"), ("focus", "method"),
        ]
        assert decoded[line_of(source, "index Node")] == [
            ("index", "keyword"), ("Node", "struct"), ("owner", "variable"),
        ]
        assert decoded[line_of(source, "unique Player")] == [
            ("unique", "keyword"), ("Player", "struct"), ("badge", "variable"),
        ]
        # Comments and ordinary code around the declarations are untouched.
        comment = decoded[line_of(source, "The max of")]
        assert len(comment) == 1 and comment[0][1] == "comment"
        assert ("count_nodes", "function") in decoded[line_of(source, "count_nodes")]
        # Nothing runs past the file into the generated code.
        assert max(decoded) < len(source.split("\n"))

        # A word of the syntax is an ordinary name elsewhere.
        names = "entity A max 4 { i32 max; i32 index; }\n"
        assert ("max", "keyword") in tokens_by_line(server, MODEL, names)[0]
        assert ("max", "variable") in tokens_by_line(server, MODEL, names)[0]
    finally:
        server.stop_syntax_checker()


def test_columns_look_like_struct_fields():
    body = """ {
    i32 kind;
    Text<16> label;
    i32 samples[8];
    Blob name;
    Item other;
    fn(i32 id) -> void on_click;
}"""
    prelude = 'import { Text } from "t.mln";\nimport { Blob } from "b.mln";\n'
    server = LspTestServer()
    try:
        entity = tokens_by_line(server, MODEL, f"{prelude}entity Item max 4{body}\n")
        struct = tokens_by_line(
            server, "file:///tmp/plain.mln", f"{prelude}struct Item{body};\n"
        )
        for line in range(3, 9):
            assert entity[line] == struct[line], (line, entity[line], struct[line])
    finally:
        server.stop_syntax_checker()


def open_cold(document):
    """A server that has only just heard of `document`, as a first request does."""
    server = LspTestServer()
    server.document_store.update(document.uri, document.source, 1)
    return server


def definition(document, needle, occurrence=0, after=1):
    server = open_cold(document)
    try:
        return server.request(1, "textDocument/definition", {
            "textDocument": {"uri": document.uri},
            "position": position(document.source, needle, occurrence, after),
        })["result"]
    finally:
        server.stop_syntax_checker()


def test_definition():
    with FixtureWorkspace() as workspace:
        model = workspace.materialize("entity/world.model.mln")
        caller = workspace.materialize("entity/caller.mln")
        chained = workspace.materialize("entity/chained.mln")
        entity = ("Node", "entity Node", len("entity "))
        window = ("Window", "entity Window", len("entity "))
        relation = ("children", "children /", 0)
        inverse = ("parent", "parent,", 0)
        focus = ("focus", "focus;", 0)
        owner = ("owner", "owner;", 0)
        badge = ("badge", "badge;", 0)
        kind = ("kind", "kind;", 0)
        cases = [
            (caller, "new_node", entity),
            (caller, "node_from_raw", entity),
            (caller, ".row(", entity),
            (caller, ".delete(", entity),
            (caller, ".is_live(", entity),
            (caller, "new_window", window),
            (caller, "Node must", entity),
            (caller, "NodeRow", entity),
            (caller, "Window window", window),
            (caller, ".children_add(", relation),
            (caller, ".children(", relation),
            (caller, ".parent(", inverse),
            (caller, ".set_focus(", focus),
            (caller, ".focus(", focus),
            (caller, ".set_owner(", owner),
            (caller, ".owner(", owner),
            (caller, ".node_by_owner(", owner),
            (caller, "player_by_badge", badge),
            (caller, ".kind", kind),
            # The receiver comes from a call, and NodeRow is not imported.
            (chained, "node_from_raw", entity),
            (chained, ".children_add(", relation),
            (chained, ".children_count(", relation),
            (chained, ".row(", entity),
            (chained, ".kind", kind),
        ]
        for document, needle, (word, declaration, offset) in cases:
            after = 1 if needle[0] != "." else 2
            result = definition(document, needle, after=after)
            assert isinstance(result, dict), (needle, result)
            assert result["uri"] == model.uri, (needle, result)
            start = position(model.source, declaration, after=offset)
            assert result["range"]["start"] == start, (needle, result)
            assert result["range"]["end"]["character"] == start["character"] + len(word)

        result = definition(caller, "count_nodes")
        assert result["range"]["start"] == position(model.source, "count_nodes")


def test_definition_inside_the_model():
    with FixtureWorkspace() as workspace:
        model = workspace.materialize("entity/world.model.mln")
        cases = [
            # A use of an entity or a column reaches its declaration.
            ("Node   has", "entity Node", len("entity ")),
            ("Node as children", "entity Node", len("entity ")),
            ("Node.owner", "entity Node", len("entity ")),
            ("owner;\nunique", "owner;", 0),
            # A declaration is its own definition.
            ("Node   max", "Node   max", 0),
            ("children /", "children /", 0),
            ("kind;", "kind;", 0),
            # Lines that pass through call what the model generated.
            ("node_live_id", "entity Node", len("entity ")),
        ]
        for needle, declaration, offset in cases:
            result = definition(model, needle)
            assert isinstance(result, dict), (needle, result)
            assert result["range"]["start"] == position(model.source, declaration, after=offset), (
                needle, result
            )
        for keyword in ("has", "many", "ordered", "max 16", "unique", "entity Node"):
            assert definition(model, keyword) is None, keyword


def hover(document, needle, after=1):
    server = open_cold(document)
    try:
        params = {
            "textDocument": {"uri": document.uri},
            "position": position(document.source, needle, after=after),
        }
        server.request(1, "textDocument/hover", params)
        return server.request(2, "textDocument/hover", params)["result"]
    finally:
        server.stop_syntax_checker()


def test_hover():
    with FixtureWorkspace() as workspace:
        model = workspace.materialize("entity/world.model.mln")
        caller = workspace.materialize("entity/caller.mln")
        chained = workspace.materialize("entity/chained.mln")
        cases = [
            (caller, "new_node", 1, "new_node() -> Option<Node>"),
            (caller, ".row(", 2, "row() -> borrow NodeRow"),
            (caller, ".children(", 2, "children() -> node_children_Iter"),
            (caller, ".children_add(", 2, "children_add(Node member) -> bool"),
            (caller, ".parent(", 2, "parent() -> Option<Node>"),
            (caller, ".set_focus(", 2, "set_focus(Node target) -> bool"),
            (caller, ".owner(", 2, "owner() -> i32"),
            (caller, ".set_owner(", 2, "set_owner(i32 value) -> void"),
            (caller, ".node_by_owner(", 2, "node_by_owner(i32 value) -> node_by_owner_Iter"),
            (caller, "node_by_owner_Iter", 1, "struct node_by_owner_Iter {\n    i32 next;\n    i32 value;\n}"),
            (caller, "player_by_badge", 1, "player_by_badge(i32 value) -> Option<Player>"),
            (chained, ".children_count(", 2, "children_count() -> i32"),
            # From the declaration, the function of the same name.
            (model, "children /", 1, "children() -> node_children_Iter"),
            (model, "parent,", 1, "parent() -> Option<Node>"),
            (model, "focus;", 1, "focus() -> Option<Node>"),
            (model, "owner;\nunique", 1, "owner() -> i32"),
            (model, "node_live_id", 1, "node_live_id(i32 id) -> bool"),
        ]
        for document, needle, after, signature in cases:
            result = hover(document, needle, after)
            assert result is not None, needle
            assert f"```mylang\n{signature}\n```" in result["contents"]["value"], (needle, result)

        # The generator's doc comments come along.
        assert "or None when the table is full" in hover(caller, "new_node")["contents"]["value"]
        assert "struct Node {\n    i32 id;\n}" in hover(caller, "Node must")["contents"]["value"]
        assert "struct Node {\n    i32 id;\n}" in hover(model, "Node as")["contents"]["value"]

        # A row shows its columns, except the ones row() does not expose.
        row = hover(caller, "NodeRow")["contents"]["value"]
        assert "    i32 kind;\n    Text<16> label;\n" in row, row
        assert "__" not in row and "owner" not in row, row

        # The range is the word under the pointer, in the file as written.
        result = hover(model, "children /")
        assert result["range"]["start"] == position(model.source, "children /")


def test_signature_help():
    with FixtureWorkspace() as workspace:
        workspace.materialize("entity/world.model.mln")
        caller = workspace.materialize("entity/caller.mln")
        server = open_cold(caller)
        try:
            result = server.request(1, "textDocument/signatureHelp", {
                "textDocument": {"uri": caller.uri},
                "position": position(caller.source, "children_add(leaf", after=len("children_add(")),
            })["result"]
            assert result["signatures"][0]["label"] == "children_add(Node member) -> bool", result
        finally:
            server.stop_syntax_checker()


def spelled(result, documents):
    """Reference locations as sorted (file name, line, text)."""
    by_uri = {document.uri: document.source.split("\n") for document in documents}
    found = []
    for location in result:
        start, end = location["range"]["start"], location["range"]["end"]
        line = by_uri[location["uri"]][start["line"]]
        found.append((
            location["uri"].rsplit("/", 1)[1],
            start["line"],
            line[start["character"]:end["character"]],
        ))
    return sorted(found)


def references(root, document, needle, documents, after=1, include=True):
    server = LspTestServer()
    server.workspace_roots = [root]
    server.open_document(document.uri, document.source)
    try:
        return spelled(server.request(1, "textDocument/references", {
            "textDocument": {"uri": document.uri},
            "position": position(document.source, needle, after=after),
            "context": {"includeDeclaration": include},
        })["result"], documents)
    finally:
        server.stop_syntax_checker()


def test_references():
    with FixtureWorkspace() as workspace:
        model = workspace.materialize("entity/world.model.mln")
        caller = workspace.materialize("entity/caller.mln")
        chained = workspace.materialize("entity/chained.mln")
        documents = [model, caller, chained]
        relation_line = line_of(model.source, "Node   has")
        declared = ("world.model.mln", relation_line, "children")
        add_line = line_of(caller.source, "root.children_add")
        loop_line = line_of(caller.source, "root.children()")

        # A relation answers for every function it generates, in any file.
        uses = [
            ("caller.mln", add_line, "children_add"),
            ("caller.mln", loop_line, "children"),
            ("chained.mln", line_of(chained.source, "children_add"), "children_add"),
            ("chained.mln", line_of(chained.source, "children_count"), "children_count"),
            ("caller.mln", line_of(caller.source, "node_children_Iter"), "node_children_Iter"),
        ]
        assert references(workspace.root, model, "children /", documents) == sorted(uses + [declared])
        assert references(workspace.root, model, "children /", documents, include=False) == sorted(uses)

        # The inverse is a name of its own, on the same relation.
        assert references(workspace.root, model, "parent,", documents) == sorted([
            ("caller.mln", line_of(caller.source, "leaf.parent"), "parent"),
            ("world.model.mln", relation_line, "parent"),
        ])

        # A use is that one function, and its declaration is the relation.
        assert references(workspace.root, caller, "children_add", documents, after=1) == sorted([
            uses[0], uses[2], declared,
        ])

        # An entity: its type, wherever it is written, and what it generates.
        found = references(workspace.root, model, "Node   max", documents)
        for expected in [
            ("world.model.mln", line_of(model.source, "entity Node"), "Node"),
            ("world.model.mln", relation_line, "Node"),
            ("world.model.mln", line_of(model.source, "index Node"), "Node"),
            ("world.model.mln", line_of(model.source, "node_live_id"), "node_live_id"),
            ("caller.mln", line_of(caller.source, "Node must"), "Node"),
            ("caller.mln", line_of(caller.source, "Node root"), "new_node"),
            ("caller.mln", line_of(caller.source, "root.row"), "row"),
            ("caller.mln", line_of(caller.source, "import { Node"), "NodeRow"),
            ("chained.mln", line_of(chained.source, ".row()"), "row"),
        ]:
            assert expected in found, (expected, found)
        # Other entities, and functions generated from relations, stay out.
        for word in ("Window", "window", "children", "children_add", "set_owner"):
            assert word not in {text for _file, _line, text in found}, (word, found)
        # The same answer from any use of the type, for that spelling.
        assert ("world.model.mln", relation_line, "Node") in references(
            workspace.root, caller, "Node must", documents
        )

        # Generated text is never a location.
        lines = len(model.source.split("\n"))
        assert all(line < lines for name, line, _text in found if name == "world.model.mln")


def test_document_symbols():
    source = fixture_text("entity/world.model.mln")
    server = LspTestServer()
    try:
        symbols = server.document_symbols_for_uri(MODEL, source)
        assert [(symbol["name"], symbol["kind"]) for symbol in symbols] == [
            ("Window", 23), ("Node", 23), ("Player", 23),
            ("children", 6), ("parent", 6), ("focus", 6), ("players", 6), ("window", 6),
            ("count_nodes", 12),
        ], symbols
        children = next(symbol for symbol in symbols if symbol["name"] == "children")
        assert children["range"]["start"] == position(source, "children /")

        # A broken statement does not hide the declarations around it.
        broken = "entity A max 4 { i32 x; }\nA has many C as cs;\nA knows one A as next;\n"
        assert {symbol["name"] for symbol in server.document_symbols_for_uri(MODEL, broken)} >= {
            "A", "next",
        }
    finally:
        server.stop_syntax_checker()


def test_edits_reach_importers():
    with FixtureWorkspace() as workspace:
        model = workspace.materialize("entity/world.model.mln")
        caller = workspace.materialize("entity/caller.mln")
        server = LspTestServer()
        try:
            server.open_document(model.uri, model.source)
            server.open_document(caller.uri, caller.source)
            params = {
                "textDocument": {"uri": caller.uri},
                "position": position(caller.source, ".children(", after=2),
            }
            first = server.request(1, "textDocument/definition", params)["result"]
            assert first["range"]["start"]["line"] == line_of(model.source, "Node   has")

            edited = "// a new first line\n" + model.source
            server.notify("textDocument/didChange", {
                "textDocument": {"uri": model.uri, "version": 2},
                "contentChanges": [{"text": edited}],
            })
            second = server.request(2, "textDocument/definition", params)["result"]
            assert second["range"]["start"]["line"] == first["range"]["start"]["line"] + 1

            renamed = edited.replace("children /", "kids /")
            server.notify("textDocument/didChange", {
                "textDocument": {"uri": model.uri, "version": 3},
                "contentChanges": [{"text": renamed}],
            })
            assert server.request(3, "textDocument/definition", params)["result"] is None
        finally:
            server.stop_syntax_checker()


def test_publishes_over_the_protocol():
    server = LspTestServer()
    try:
        broken = GOOD + "A has many C as cs;\n"
        server.open_document(MODEL, broken)
        server.request(1, "textDocument/semanticTokens/full", {"textDocument": {"uri": MODEL}})
        published = next(
            message for message in reversed(server.messages)
            if message.get("method") == "textDocument/publishDiagnostics"
        )
        assert published["params"]["uri"] == MODEL
        (diagnostic,) = published["params"]["diagnostics"]
        assert diagnostic["range"]["start"] == position(broken, "C as")
        assert diagnostic["message"] == "unknown entity 'C'"
    finally:
        server.stop_syntax_checker()


def test_truncation_never_breaks_the_frontend():
    source = fixture_text("entity/world.model.mln")
    server = LspTestServer()
    try:
        server.syntax_diagnostics_for_uri(MODEL, source)
        process = server.native_frontend.process
        for end in range(0, len(source), 11):
            text = source[:end]
            lines = text.split("\n")
            for diagnostic in server.syntax_diagnostics_for_uri(MODEL, text):
                assert diagnostic["range"]["start"]["line"] < len(lines), (end, diagnostic)
            assert max(tokens_by_line(server, MODEL, text), default=0) < len(lines), end
        assert server.native_frontend.process is process and process.poll() is None
    finally:
        server.stop_syntax_checker()


def run():
    test_fixtures_are_mylang()
    test_no_false_diagnostics()
    test_real_errors()
    test_error_marks_the_word()
    test_errors_keep_the_server_going()
    print("[PASS] model files: diagnostics")
    test_highlighting()
    test_columns_look_like_struct_fields()
    print("[PASS] model files: highlighting")
    test_definition()
    test_definition_inside_the_model()
    print("[PASS] model files: go to definition")
    test_hover()
    test_signature_help()
    print("[PASS] model files: hover and signature help")
    test_references()
    test_document_symbols()
    print("[PASS] model files: references and document symbols")
    test_edits_reach_importers()
    test_publishes_over_the_protocol()
    test_truncation_never_breaks_the_frontend()
    print("[PASS] model files: edits and robustness")


if __name__ == "__main__":
    run()
