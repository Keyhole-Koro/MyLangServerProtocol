#!/usr/bin/env python3
"""Tests for LspServer.semantic_tokens.

Guards contracts rather than pinning the full token stream:
  1. classification  - identifiers get the right type (minimal, per-role)
  2. span fidelity   - a token's slice equals the source lexeme (hex/escape width)
  3. well-formedness - output is always a valid semantic-tokens stream
  4. robustness      - broken/empty/large input never crashes; lexical layer lives
  5. cross-component - the C<->Python seam (kinds and roles map to valid types)

Integration test: spawns the real mylang-syntax-check binary. Skips cleanly if
the toolchain cannot be built/started.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402

INV = {i: name for name, i in srv.TOKEN_TYPE_INDEX.items()}

# Roles the C engine (role_name in syntax_check.c) can attach to a token.
ENGINE_ROLES = {"function", "type", "struct", "namespace", "parameter", "property"}


def decode(server, text):
    """Decode semantic_tokens into [(line, col, length, type, slice)]."""
    enc = server.semantic_tokens(text)
    lines = text.splitlines()
    out = []
    line = col = 0
    for i in range(0, len(enc), 5):
        dl, dc, length, tidx, _mod = enc[i:i + 5]
        line += dl
        col = dc if dl else col + dc
        slice_ = lines[line][col:col + length] if 0 <= line < len(lines) else None
        out.append((line, col, length, INV.get(tidx), slice_))
    return out


def assert_wellformed(decoded, text, label):
    lines = text.splitlines()
    prev = (-1, -1)
    for line, col, length, ttype, _slice in decoded:
        assert ttype is not None, f"{label}: unknown token type index"
        assert length > 0, f"{label}: non-positive length at {(line, col)}"
        assert 0 <= line < len(lines), f"{label}: line {line} out of range"
        assert col + length <= len(lines[line]), \
            f"{label}: token at {(line, col)} len {length} crosses line end"
        assert (line, col) > prev, f"{label}: tokens not strictly ordered at {(line, col)}"
        prev = (line, col)


def roles_in(decoded):
    """Map text-slice -> set of types it was classified as."""
    out = {}
    for _l, _c, _len, ttype, slice_ in decoded:
        out.setdefault(slice_, set()).add(ttype)
    return out


def decode_uri(server, uri, text):
    enc = server.semantic_tokens_for_uri(uri, text)
    lines = text.splitlines()
    out = []
    line = col = 0
    for i in range(0, len(enc), 5):
        dl, dc, length, tidx, _mod = enc[i:i + 5]
        line += dl
        col = dc if dl else col + dc
        slice_ = lines[line][col:col + length] if 0 <= line < len(lines) else None
        out.append((line, col, length, INV.get(tidx), slice_))
    return out


# (source, {text: expected_type}) — assert each text is classified as expected.
CLASSIFY_CASES = [
    ("i32 add(i32 a, Point b) { return a + b; }\n",
     {"add": "function", "a": "parameter", "b": "parameter", "Point": "type"}),
    ("package gfx;\n", {"gfx": "namespace"}),
    ('import math from "m";\n', {"math": "namespace"}),
    ("struct Point { i32 x; };\n", {"Point": "struct"}),
    ("enum Color { RED, GREEN };\n", {"Color": "struct"}),
    ("typedef struct { i32 x; } Vec;\n", {"Vec": "struct"}),
    ("extern i32 puts(i32 c);\n", {"puts": "function", "c": "parameter"}),
    ("i32 f(mut i32 a) { return a; }\n", {"a": "parameter", "f": "function"}),
    ("i32 main() { return foo(b) + a.x; }\n",
     {"foo": "function", "x": "property", "main": "function"}),
    ("u8 a = 0; u16 b = 1; Point p = mk();\n",
     {"u8": "type", "u16": "type", "Point": "type", "mk": "function"}),
    # a method-style call stays property (no symbol resolution to call it a method).
    ("i32 m() { return obj.bar(); }\n", {"bar": "property"}),
]

# Lexical mapping sweep: representative tokens for the KIND_TO_TYPE seam.
# (source, {text: expected_type}). Guards against KIND_TO_TYPE drift.
LEXICAL_CASES = [
    ("i32 m() { i32 x = 0b1010; return x << 2 && 1; }\n",
     {"i32": "type", "return": "keyword", "0b1010": "number",
      "<<": "operator", "&&": "operator"}),
    ("char c = 'x'; char s = \"hi\";\n",
     {"char": "type", "'x'": "string", '"hi"': "string"}),
    ("ref i32 g(mut i32 a) { return a; }\n",
     {"ref": "ownershipRef", "mut": "ownershipMut"}),
    ("i32 m() { bool t = true; return t; }\n",
     {"bool": "type", "true": "variable"}),  # true/false/null lex as identifiers
    ("u8 b = 1; u16 w = 2;\n",
     {"u8": "type", "u16": "type"}),
]

# (source, [literal lexemes that must each appear as an exact token slice])
WIDTH_CASES = [
    ("i32 x = 0xFF;\n", ["0xFF"]),
    ('char s = "hi\\n";\n', ['"hi\\n"']),
]

# documentSymbol: (source, {name: SYMBOL_KIND key}, [names that must be absent])
DOCSYM_CASES = [
    ("i32 g = 0;\n"
     "struct Point { i32 fx; };\n"
     "enum Color { RED };\n"
     "typedef i32 MyInt;\n"
     "i32 add(i32 p) { i32 local = foo(p); return local; }\n",
     {"g": "variable", "Point": "struct", "Color": "enum",
      "MyInt": "struct", "add": "function"},
     ["fx", "local", "foo", "p", "RED"]),
]

ROBUSTNESS_INPUTS = [
    "",
    "\n\n\n",
    "i32 add(i32 a){ @@@ broken\nPoint p = mk(\n",   # syntax error mid-document
    '"unterminated string\n',
    "/* unclosed block comment\n",
    "i32 " + "x" * 4000 + ";\n",                       # long line
    "}{)(][;,.\n",                                     # punctuation soup
]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 5. cross-component seam (no binary needed)
    for kind, ttype in srv.KIND_TO_TYPE.items():
        check(ttype in srv.TOKEN_TYPE_INDEX, f"seam: KIND_TO_TYPE[{kind}]={ttype!r} not a token type")
    for role in ENGINE_ROLES:
        check(role in srv.TOKEN_TYPE_INDEX, f"seam: engine role {role!r} not a token type")

    server = srv.LspServer()
    if not server.start_syntax_checker():
        print("[SKIP] mylang-syntax-check unavailable; cannot run integration tests")
        # seam checks above still count
        if failures:
            for f in failures:
                print(f"[FAIL] {f}")
            return 1
        print("[PASS] cross-component seam")
        return 0

    try:
        # 1. classification + 5. lexical mapping seam
        for src, expected in CLASSIFY_CASES + LEXICAL_CASES:
            decoded = decode(server, src)
            rmap = roles_in(decoded)
            for text, want in expected.items():
                got = rmap.get(text, set())
                check(want in got, f"classify {src!r}: {text!r} -> {got!r}, expected {want!r}")
            # 3. well-formedness on the same inputs
            assert_wellformed(decoded, src, "classify")

        # 2. span fidelity
        for src, literals in WIDTH_CASES:
            decoded = decode(server, src)
            slices = [s for *_x, s in decoded]
            for lit in literals:
                check(lit in slices, f"width {src!r}: {lit!r} not an exact token slice; got {slices!r}")
            assert_wellformed(decoded, src, "width")

        # 4. robustness + well-formedness on hostile inputs
        for src in ROBUSTNESS_INPUTS:
            try:
                decoded = decode(server, src)
                assert_wellformed(decoded, src, "robust")
            except Exception as exc:  # noqa: BLE001
                check(False, f"robustness: crashed on {src!r}: {exc!r}")

        # documentSymbol: outline driven by the parser's symbol output.
        sym_name = {v: k for k, v in srv.SYMBOL_KIND.items()}
        for src, expected, absent in DOCSYM_CASES:
            syms = server.document_symbols(src)
            got = {}
            for s in syms:
                got.setdefault(s["name"], set()).add(sym_name.get(s["kind"]))
            for name, want in expected.items():
                check(want in got.get(name, set()),
                      f"docsym {src!r}: {name!r} -> {got.get(name)!r}, expected {want!r}")
            for name in absent:
                check(name not in got, f"docsym: {name!r} must not be an outline symbol")

        # graceful degradation: a syntax error mid-document must not wipe out the
        # lexical layer before it (B's accepted tradeoff).
        broken = "i32 add(i32 a){ return; @@@ broken\nPoint p = mk(\n"
        types = {t for *_x, t, _s in decode(server, broken)}
        check({"type", "operator", "keyword"} <= types,
              f"degradation: broken input lost its lexical layer; types={types!r}")

        # .mlx documents are validated through MyDOMTranspiler and get JSX-like token
        # highlighting without feeding raw <Window> syntax to the MyLang grammar.
        mlx_uri = "file:///tmp/screen.mlx"
        mlx_src = (
            "package main;\n"
            "DomNode* screen() {\n"
            "    return <Window title=\"Settings\" width={320}>\n"
            "        <Button text=\"OK\" onClick={handle_ok} />\n"
            "    </Window>;\n"
            "}\n"
        )
        mlx_decoded = decode_uri(server, mlx_uri, mlx_src)
        mlx_roles = roles_in(mlx_decoded)
        check("keyword" in mlx_roles.get("return", set()),
              f"mlx tokens: return roles={mlx_roles.get('return')!r}")
        check("function" in mlx_roles.get("screen", set()),
              f"mlx tokens: screen roles={mlx_roles.get('screen')!r}")
        check("struct" in mlx_roles.get("Window", set()),
              f"mlx tokens: Window roles={mlx_roles.get('Window')!r}")
        check("property" in mlx_roles.get("title", set()),
              f"mlx tokens: title roles={mlx_roles.get('title')!r}")
        check("number" in mlx_roles.get("320", set()),
              f"mlx tokens: 320 roles={mlx_roles.get('320')!r}")
        check(server.syntax_diagnostics_for_uri(mlx_uri, mlx_src) == [],
              "mlx diagnostics: valid .mlx produced diagnostics")
        bad_mlx = "DomNode* screen() { return <Window title={} />; }\n"
        bad_diags = server.syntax_diagnostics_for_uri(mlx_uri, bad_mlx)
        check(bad_diags and "Expected expression before '}'." in bad_diags[0]["message"],
              f"mlx diagnostics: invalid .mlx missing syntax diagnostic, got {bad_diags!r}")
    finally:
        server.stop_syntax_checker()

    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        return 1
    print(f"[PASS] classification ({len(CLASSIFY_CASES)})")
    print(f"[PASS] lexical mapping seam ({len(LEXICAL_CASES)})")
    print(f"[PASS] documentSymbol ({len(DOCSYM_CASES)})")
    print(f"[PASS] span fidelity ({len(WIDTH_CASES)})")
    print(f"[PASS] well-formedness + robustness ({len(ROBUSTNESS_INPUTS)} hostile inputs)")
    print("[PASS] cross-component seam")
    print("[PASS] native MLX syntax LSP integration")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
