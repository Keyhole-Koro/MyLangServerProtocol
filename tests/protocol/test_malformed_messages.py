#!/usr/bin/env python3
"""The server process must survive well-framed but malformed JSON-RPC bodies."""

import json
import select
import subprocess
import sys
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[2]

MALFORMED = {
    "cancel with null params": {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": None},
    "cancel with list params": {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": [1]},
    "cancel without params": {"jsonrpc": "2.0", "method": "$/cancelRequest"},
    "batch array": [{"jsonrpc": "2.0", "id": 7, "method": "shutdown", "params": {}}],
    "bare number": 42,
    "bare string": "initialize",
}


def frame(message):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def send(process, message):
    process.stdin.write(frame(message))
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


def survives(name, malformed):
    process = subprocess.Popen(
        [sys.executable, str(SERVER_DIR / "server.py")],
        cwd=SERVER_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        send(process, malformed)
        send(process, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"initializationOptions": {"semanticTokens": False}},
        })
        initialized = receive_response(process, 1)
        assert initialized["result"]["capabilities"]["hoverProvider"] is True, name

        send(process, {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}})
        assert receive_response(process, 2)["result"] is None, name
        send(process, {"jsonrpc": "2.0", "method": "exit", "params": {}})
        process.stdin.close()
        assert process.wait(timeout=10) == 0, name
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def run():
    for name, malformed in MALFORMED.items():
        survives(name, malformed)


if __name__ == "__main__":
    run()
    print("[PASS] malformed JSON-RPC messages do not stop the server")
