#!/usr/bin/env python3
"""NativeFrontendClient against a fake mylang-syntax-check, no toolchain needed."""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mylang_lsp.frontend.native import NativeFrontendClient  # noqa: E402


# Speaks the --stdio protocol: "ready", then one JSON line per
# "content <length>\n<text>\n" request. Each request is logged next to it.
FAKE_CHECKER = """\
import json
import sys
from pathlib import Path

log = Path(__file__).with_name("exchanges.log")
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
stdout.write(b"ready\\n")
stdout.flush()
while True:
    header = stdin.readline()
    if not header:
        break
    command, _, length = header.decode("ascii").strip().partition(" ")
    text = stdin.read(int(length)).decode("utf-8")
    stdin.read(1)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps([command, text]) + "\\n")
    if text == "garbage":
        stdout.write(b"this is not json\\n")
    else:
        record = {"status": "ok", "diagnostics": [], "tokens": [], "symbols": [], "text": text}
        stdout.write(json.dumps(record).encode("utf-8") + b"\\n")
    stdout.flush()
"""


def make_toolchain(root: Path) -> Path:
    compiler = root / "toolchain" / "MyLangCompiler"
    compiler.mkdir(parents=True)
    checker = compiler / "mylang-syntax-check"
    checker.write_text(f"#!{sys.executable}\n{FAKE_CHECKER}", encoding="utf-8")
    checker.chmod(0o755)
    grammars = root / "toolchain" / "MySyntaxEngine" / "tests" / "fixtures" / "grammars"
    grammars.mkdir(parents=True)
    (grammars / "mylang_lsp.grammar").write_text("", encoding="utf-8")
    (grammars / "mlx.grammar").write_text("", encoding="utf-8")
    return compiler / "exchanges.log"


def exchanges(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_same_text_is_answered_from_the_cache(root: Path):
    log = make_toolchain(root)
    client = NativeFrontendClient(root)
    try:
        first = client.query("i32 x;\n")
        second = client.query("i32 x;\n")
        assert first is not None and first["text"] == "i32 x;\n", first
        assert second is first
        assert exchanges(log) == [["content", "i32 x;\n"]], exchanges(log)
    finally:
        client.stop()


def test_cache_limit_evicts_the_oldest_text(root: Path):
    log = make_toolchain(root)
    client = NativeFrontendClient(root, cache_limit=2)
    try:
        for text in ("a", "b", "c"):
            assert client.query(text)["text"] == text
        assert len(client.result_cache) == 2
        client.query("c")
        client.query("b")
        assert [text for _command, text in exchanges(log)] == ["a", "b", "c"]
        client.query("a")
        assert [text for _command, text in exchanges(log)] == ["a", "b", "c", "a"]
        # The hit on "b" made "c" the least recently used, so "a" evicted it.
        client.query("b")
        assert [text for _command, text in exchanges(log)] == ["a", "b", "c", "a"]
        client.query("c")
        assert [text for _command, text in exchanges(log)] == ["a", "b", "c", "a", "c"]
    finally:
        client.stop()


def test_bad_reply_stops_the_checker_and_the_next_query_restarts_it(root: Path):
    log = make_toolchain(root)
    client = NativeFrontendClient(root)
    try:
        assert client.query("garbage") is None
        assert client.process is None
        assert client.query("i32 y;")["text"] == "i32 y;"
        assert client.process is not None
        # A failed reply is not cached, so asking again reaches the checker.
        assert client.query("garbage") is None
        assert [text for _command, text in exchanges(log)] == ["garbage", "i32 y;", "garbage"]
    finally:
        client.stop()


def test_missing_checker_returns_none_quickly(root: Path):
    # No toolchain/MyLangCompiler directory, so no `make` is attempted either.
    client = NativeFrontendClient(root)
    started = time.monotonic()
    assert client.query("i32 x;") is None
    assert client.query("i32 x;") is None
    assert time.monotonic() - started < 2.0
    assert client.process is None
    assert client.build_attempted is True


def run():
    tests = (
        test_same_text_is_answered_from_the_cache,
        test_cache_limit_evicts_the_oldest_text,
        test_bad_reply_stops_the_checker_and_the_next_query_restarts_it,
        test_missing_checker_returns_none_quickly,
    )
    for test in tests:
        with tempfile.TemporaryDirectory() as name:
            test(Path(name))


if __name__ == "__main__":
    run()
    print("[PASS] native frontend client: handshake, framing, cache, failures")
