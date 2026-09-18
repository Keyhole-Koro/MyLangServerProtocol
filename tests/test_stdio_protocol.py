#!/usr/bin/env python3
"""End-to-end JSON-RPC framing test against the language-server process."""

import json
import select
import subprocess
import sys
from pathlib import Path

from feature_test_support import position


SERVER_DIR = Path(__file__).resolve().parents[1]
SOURCE = """/// Adds.
/// @param a First.
/// @param b Second.
i32 add(i32 a, i32 b) { return a + b; }
i32 main() { return add(1, 2); }
"""


def send(process, message):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    process.stdin.write(body)
    process.stdin.flush()


def receive(process, timeout=10):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("timed out waiting for LSP response")
    headers = {}
    while True:
        line = process.stdout.readline()
        if line in (b"\r\n", b"\n"):
            break
        if not line:
            raise AssertionError("language server closed stdout")
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(process.stdout.read(length).decode("utf-8"))


def receive_response(process, request_id):
    while True:
        message = receive(process)
        if message.get("id") == request_id:
            return message


def run():
    process = subprocess.Popen(
        [sys.executable, str(SERVER_DIR / "server.py")],
        cwd=SERVER_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        send(process, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"initializationOptions": {"semanticTokens": True}},
        })
        initialized = receive_response(process, 1)
        assert initialized["result"]["capabilities"]["hoverProvider"] is True

        uri = "file:///tmp/stdio-feature.mln"
        send(process, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": "mylang",
                    "version": 1,
                    "text": SOURCE,
                }
            },
        })
        send(process, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": position(SOURCE, "add(1", after=1),
            },
        })
        hover = receive_response(process, 2)
        assert "add(i32 a, i32 b) -> i32" in hover["result"]["contents"]["value"]

        send(process, {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}})
        assert receive_response(process, 3)["result"] is None
        send(process, {"jsonrpc": "2.0", "method": "exit", "params": {}})
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    run()
    print("[PASS] stdio LSP integration")
