#!/usr/bin/env python3
"""Definition targets must follow scopes, receiver types and lazy imports."""

import tempfile
from pathlib import Path

from feature_test_support import position, snapshot
from test_references import CaptureServer


def target(server, source, use, declaration, name, occurrence=0):
    doc = snapshot(source, name)
    location = server.language_features.definition(doc, position(source, use, occurrence, 1))
    assert isinstance(location, dict), (name, use, location)
    assert location["uri"] == doc.uri
    assert location["range"]["start"] == position(source, declaration), (name, use, location)


def test_local(server):
    source = """struct A { i32 x; };
struct B { i32 x; };
struct Holder { A value; };
i32 (ref A a) get() { return a.x; }
i32 (ref B b) get() { return b.x; }
A make() { A a; return a; }
i32 main() {
    A a;
    a.get();
    i32 unrelated = 1;
    a.get();
    { B a; a.get(); }
    a.get();
    Holder holder;
    holder.value.get();
    A array[2];
    array[0].get();
    return make().get();
}
"""
    # An unrelated declaration or an inner shadow must not change a's type.
    for occurrence in [2, 3, 5, 6, 7, 8]:
        target(server, source, "get", "get() { return a", "receivers.mln", occurrence)
    target(server, source, "get", "get() { return b", "receivers.mln", 4)

    generic = """struct Box<T> { T value; };
struct Bag<T> { T value; };
struct A { i32 x; };
i32 (ref A a) get() { return a.x; }
U (ref Box<U> box) take() { return box.value; }
U (ref Bag<U> bag) take() { return bag.value; }
i32 main() { Box<A> box; return box.take().get(); }
"""
    target(server, generic, "take", "take() { return box", "generic-chain.mln", 2)
    target(server, generic, "get", "get() { return a", "generic-chain.mln", 1)

    variants = """enum Result<T,E> { Ok(T), Err(E) };
enum Other { Ok(i32), Err(i32) };
i32 main() { Result<i32,i32> r = Result<i32,i32>::Ok(1); Other o = Other::Ok(2); return 0; }
"""
    target(server, variants, "Ok", "Ok(T)", "variants.mln", 2)
    target(server, variants, "Ok", "Ok(i32)", "variants.mln", 3)

    shadow = """i32 handle(i32 x) { return x; }
i32 other(i32 handle) { return handle; }
i32 main() { { i32 handle = 7; handle; } return handle(1); }
"""
    doc = snapshot(shadow, "shadow-resolution.mln")
    for occurrence in [1, 2, 3, 4]:
        assert server.language_features.definition(doc, position(shadow, "handle", occurrence, 1)) is None
    target(server, shadow, "handle", "handle(i32 x)", "shadow-resolution.mln", 5)
    refs = server.language_features.references(doc, position(shadow, "handle", after=1), False)
    assert len(refs) == 1, refs


def test_imports():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        library = root / "library.mln"
        source = """package library;
export struct Counter { i32 x; };
export i32 (ref Counter c) get() { return c.x; }
export Counter *(Counter *c) me() { return c; }
export Counter make() { Counter c; return c; }
export enum Result<T,E> { Ok(T), Err(E) };
"""
        library.write_text(source)
        unused = root / "unused.mln"
        unused.write_text("export i32 unrelated() { return 0; }\n")
        caller = root / "caller.mln"
        text = """import lib from "library.mln";
import { Counter, Result } from "library.mln";
import unused from "unused.mln";
struct Other { i32 x; };
i32 (ref Other o) get() { return o.x; }
i32 main() {
    Counter c; Counter *p = &c;
    c.get();
    p->me()->get();
    Result<i32,i32> r = Result<i32,i32>::Ok(1);
    return lib.make().get();
}
"""
        caller.write_text(text)
        # Each request starts cold; success must not depend on earlier hovers.
        cases = [("get", 1, "get()"), ("get", 2, "get()"),
                 ("get", 3, "get()"), ("Counter", 0, "Counter {"),
                 ("Counter", 1, "Counter {"), ("Result", 0, "Result<T"),
                 ("Ok", 0, "Ok(T)")]
        for needle, occurrence, declaration in cases:
            server = CaptureServer()
            try:
                server.document_store.update(caller.as_uri(), text, 1)
                server.handle({"id": 1, "method": "textDocument/definition", "params": {
                    "textDocument": {"uri": caller.as_uri()},
                    "position": position(text, needle, occurrence, 1)}})
                result = server.messages[-1]["result"]
                assert isinstance(result, dict), (needle, occurrence, result)
                assert result["uri"] == library.as_uri(), (needle, occurrence, result)
                assert result["range"]["start"] == position(source, declaration)
                assert server.workspace_index.get(unused.as_uri()) is None
            finally:
                server.stop_syntax_checker()


def test_builtin_string_methods():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        library = root / "str.mln"
        source = """package str;
export i32 (str self) len() { return self.length; }
export str (char* self) as_str() { return str { data: self, length: 0 }; }
export str (i32 self) to_str(char* buffer) { return str { data: buffer, length: 0 }; }
"""
        library.write_text(source)
        caller = root / "caller.mln"
        text = """import str from "str.mln";
i32 main() {
    str value = "hello";
    char* legacy = "old";
    char digits[12];
    if ("x".len() != value.len()) { return 1; }
    if (legacy.as_str().len() != 3) { return 2; }
    return 42.to_str(&digits[0]).len();
}
"""
        caller.write_text(text)
        cases = [
            ("len", 0, "len()"),
            ("len", 1, "len()"),
            ("as_str", 0, "as_str()"),
            ("len", 2, "len()"),
            ("to_str", 0, "to_str("),
            ("len", 3, "len()"),
        ]
        for needle, occurrence, declaration in cases:
            server = CaptureServer()
            try:
                server.document_store.update(caller.as_uri(), text, 1)
                server.handle({"id": 1, "method": "textDocument/definition", "params": {
                    "textDocument": {"uri": caller.as_uri()},
                    "position": position(text, needle, occurrence, 1)}})
                result = server.messages[-1]["result"]
                assert isinstance(result, dict), (needle, occurrence, result)
                assert result["uri"] == library.as_uri()
                assert result["range"]["start"] == position(source, declaration)
            finally:
                server.stop_syntax_checker()


if __name__ == "__main__":
    server = CaptureServer()
    try:
        test_local(server)
    finally:
        server.stop_syntax_checker()
    test_imports()
    test_builtin_string_methods()
    print("[PASS] scoped definition resolution and cold imports")
