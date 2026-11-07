"""Compiler errors for a source on disk, as LSP diagnostics.

The server's own parser sees syntax only. `mlc --check` also runs the semantic
checks, on the saved file, so its report is shown once a source is opened or
saved. An edit makes the report stale, and it stays hidden until the next save.
"""

import re
import subprocess
from pathlib import Path
from typing import List, Optional

from ..analysis.documents import LineMap


TIMEOUT_SECONDS = 10.0
SEVERITIES = {"error": 1, "warning": 2}

REPORT_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<severity>error|warning)(?:\[(?P<code>\w+)\])?: (?P<message>.*?)"
    r"(?: \[range (?P<start_line>\d+):(?P<start_column>\d+)"
    r"-(?P<end_line>\d+):(?P<end_column>\d+)\])?$"
)


def position(line_map: LineMap, line: int, column: int) -> dict:
    """The LSP position of mlc's 1-based line and byte column."""
    return line_map.offset_to_lsp(line_map.native_offset(line - 1, column - 1))


def report_range(line_map: LineMap, match: re.Match) -> dict:
    if match["start_line"]:
        return {
            "start": position(line_map, int(match["start_line"]), int(match["start_column"])),
            "end": position(line_map, int(match["end_line"]), int(match["end_column"])),
        }
    start = position(line_map, int(match["line"]), int(match["column"]))
    return {"start": start, "end": {**start, "character": start["character"] + 1}}


def top_of_file() -> dict:
    return {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


def parse_report(output: str, path: Path, text: str) -> List[dict]:
    """The diagnostics in mlc's output. What it says about another file, such as
    a broken import, goes on the first line of this one."""
    line_map = LineMap(text)
    diagnostics = []
    for line in output.splitlines():
        match = REPORT_LINE.match(line)
        if not match:
            continue
        diagnostic = {
            "severity": SEVERITIES[match["severity"]],
            "source": "mlc",
        }
        if match["code"]:
            diagnostic["code"] = match["code"]
        if (path.parent / match["file"]).resolve() == path.resolve():
            diagnostic["range"] = report_range(line_map, match)
            diagnostic["message"] = match["message"]
        else:
            place = f"{match['file']}:{match['line']}:{match['column']}"
            diagnostic["range"] = top_of_file()
            diagnostic["message"] = f"{place}: {match['message']}"
        diagnostics.append(diagnostic)
    return diagnostics


class CompilerCheck:
    def __init__(self, compiler: Path) -> None:
        self.compiler = compiler

    def check(self, path: Path, text: str) -> Optional[List[dict]]:
        """What mlc reports for the file, or None if it could not run."""
        try:
            process = subprocess.run(
                [str(self.compiler), "--check", str(path)],
                cwd=str(path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        diagnostics = parse_report(process.stdout, path, text)
        if process.returncode != 0 and not any(d["severity"] == 1 for d in diagnostics):
            lines = process.stdout.strip().splitlines()
            diagnostics.append({
                "range": top_of_file(),
                "severity": 1,
                "source": "mlc",
                "message": lines[0] if lines else f"mlc --check exited with {process.returncode}",
            })
        return diagnostics
