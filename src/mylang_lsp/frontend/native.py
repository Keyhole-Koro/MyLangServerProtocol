"""Lifecycle and caching for the native MyLang syntax-check service."""

import hashlib
import json
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Callable, List, Optional


MODEL_SUFFIX = ".model.mln"


def model_text(source: str, generated: str) -> str:
    """The text a model's analysis reads: the file as written, which keeps
    every position, then the code generated from it."""
    separator = "" if not source or source.endswith("\n") else "\n"
    return source + separator + generated


def merge_model_records(trace: dict, parsed: dict) -> dict:
    """Shape the two model records like an ordinary result, plus `model`.

    `parsed` covers the outline and the generated code; only the outline's
    tokens are on lines of the file. The trace adds the words a MyLang parser
    never sees, and says what a generated line came from.
    """
    first_generated = trace["generatedLine"]
    origins = trace["origins"]
    tokens = {(token[0], token[1]): token for token in parsed.get("tokens", [])}
    symbols = {(symbol[0], symbol[1]): symbol for symbol in parsed.get("symbols", [])}
    for line, column, length, kind, role, *declaration in trace["words"]:
        tokens[(line, column)] = [line, column, length, kind] + ([role] if role else [])
        if declaration == [line, column, length] and role in ("struct", "method"):
            symbols[(line, column)] = [line, column, length, role]

    def written(diagnostic: dict) -> dict:
        line = diagnostic.get("line", 0)
        if line < first_generated or line - first_generated >= len(origins):
            return diagnostic
        origin_line, column, length = origins[line - first_generated]
        return {**diagnostic, "line": origin_line, "character": column,
                "endCharacter": column + length}

    # A bad statement generates nothing, so the parse has nothing to add.
    diagnostics = [
        {"line": line, "character": column, "endCharacter": column + length, "message": message}
        for line, column, length, message in trace["errors"]
    ] or [written(diagnostic) for diagnostic in parsed.get("diagnostics", [])]
    return {
        "status": "error" if diagnostics else "ok",
        "diagnostics": diagnostics,
        "tokens": [tokens[key] for key in sorted(tokens)],
        "symbols": [symbols[key] for key in sorted(symbols)],
        "model": {
            "generated": trace["generated"],
            "generatedLine": first_generated,
            "origins": origins,
            "words": trace["words"],
        },
    }


class NativeFrontendClient:
    """Own the stateful syntax-check subprocess behind a small query API."""

    def __init__(self, repo_root: Path, cache_limit: int = 16) -> None:
        self.working_directory = repo_root / "toolchain" / "MyLangCompiler"
        self.binary = self.working_directory / "mylang-syntax-check"
        grammar_directory = (
            repo_root
            / "toolchain"
            / "MySyntaxEngine"
            / "tests"
            / "fixtures"
            / "grammars"
        )
        self.base_grammar = grammar_directory / "mylang_lsp.grammar"
        self.grammars = f"{self.base_grammar},{grammar_directory / 'mlx.grammar'}"
        self.table_cache = self.working_directory / "mylang-syntax-check-lsp.table"
        self.build_attempted = False
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.result_cache: OrderedDict[bytes, dict] = OrderedDict()
        self.cache_limit = cache_limit

    def query(self, text: str, uri: str = "") -> Optional[dict]:
        if uri.endswith(MODEL_SUFFIX):
            return self._exchange(
                "model", text, 2, lambda records: merge_model_records(*records)
            )
        return self._exchange("content", text, 1, lambda records: records[0])

    def _exchange(
        self,
        command: str,
        text: str,
        record_count: int,
        build: Callable[[List[dict]], dict],
    ) -> Optional[dict]:
        data = text.encode("utf-8")
        cache_key = hashlib.sha256(command.encode("ascii") + b"\0" + data).digest()
        cached = self.result_cache.get(cache_key)
        if cached is not None:
            self.result_cache.move_to_end(cache_key)
            return cached
        if not self.start() or self.process is None or self.process.stdin is None:
            return None

        try:
            self.process.stdin.write(f"{command} {len(data)}\n".encode("ascii"))
            self.process.stdin.write(data)
            self.process.stdin.write(b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.stop()
            return None

        try:
            records = []
            for _ in range(record_count):
                line = self._read_line()
                if line is None:
                    self.stop()
                    return None
                records.append(json.loads(line))
            result = build(records)
        except (json.JSONDecodeError, KeyError, OSError):
            # A KeyError is a binary too old to know the model command.
            self.stop()
            return None

        self.result_cache[cache_key] = result
        self.result_cache.move_to_end(cache_key)
        while len(self.result_cache) > self.cache_limit:
            self.result_cache.popitem(last=False)
        return result

    def start(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        if not self._ensure_binary() or not self.base_grammar.exists():
            return False

        try:
            self.process = subprocess.Popen(
                [
                    str(self.binary),
                    "--stdio",
                    self.grammars,
                    str(self.table_cache),
                ],
                cwd=str(self.working_directory),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.process = None
            return False

        line = self._read_line()
        if line and line.rstrip("\n") == "ready":
            return True
        self.stop()
        return False

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _read_line(self) -> Optional[str]:
        if self.process is None or self.process.stdout is None:
            return None
        line = self.process.stdout.readline()
        # The generator cuts a long column at a byte, not at a character.
        return line.decode("utf-8", errors="replace") if line else None

    def _ensure_binary(self) -> bool:
        if self.binary.exists():
            return True
        if self.build_attempted:
            return False
        self.build_attempted = True
        if not self.working_directory.exists():
            return False
        try:
            subprocess.run(
                ["make", "syntax-check"],
                cwd=str(self.working_directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return self.binary.exists()
