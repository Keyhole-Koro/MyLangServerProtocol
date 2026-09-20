"""Lifecycle and caching for the native MyLang syntax-check service."""

import hashlib
import json
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Optional


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

    def query(self, text: str) -> Optional[dict]:
        data = text.encode("utf-8")
        cache_key = hashlib.sha256(data).digest()
        cached = self.result_cache.get(cache_key)
        if cached is not None:
            self.result_cache.move_to_end(cache_key)
            return cached
        if not self.start() or self.process is None or self.process.stdin is None:
            return None

        try:
            self.process.stdin.write(f"content {len(data)}\n".encode("ascii"))
            self.process.stdin.write(data)
            self.process.stdin.write(b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.stop()
            return None

        try:
            line = self._read_line()
            if line is None:
                self.stop()
                return None
            result = json.loads(line)
        except (json.JSONDecodeError, OSError):
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
        return line.decode("utf-8") if line else None

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
