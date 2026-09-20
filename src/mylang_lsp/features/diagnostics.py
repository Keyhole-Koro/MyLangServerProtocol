"""Syntax diagnostics built from source balancing and native parser output."""

from typing import Callable, List, Optional, Tuple

from ..analysis.documents import LineMap
from ..source_text import is_protected, protected_spans, utf16_column


DIAGNOSTIC_SEVERITY_ERROR = 1


class DiagnosticsService:
    def __init__(self, query_frontend: Callable[[str], Optional[dict]]) -> None:
        self.query_frontend = query_frontend

    def analyze(self, text: str) -> List[dict]:
        diagnostics = self.bracket_diagnostics(text)
        if diagnostics:
            return diagnostics
        return self.frontend_diagnostics(text)

    def bracket_diagnostics(self, text: str) -> List[dict]:
        lines = text.splitlines()
        protected = protected_spans(lines)
        stack: List[Tuple[str, int, int]] = []
        diagnostics: List[dict] = []
        pairs = {"(": ")", "{": "}", "[": "]"}
        closers = {")": "(", "}": "{", "]": "["}
        for line_no, line in enumerate(lines):
            for character, value in enumerate(line):
                if is_protected(line_no, character, character + 1, protected):
                    continue
                if value in pairs:
                    stack.append((value, line_no, character))
                elif value in closers:
                    if stack and stack[-1][0] == closers[value]:
                        stack.pop()
                    else:
                        diagnostics.append(self.make(
                            line_no,
                            utf16_column(line, character),
                            utf16_column(line, character + 1),
                            f"Unexpected '{value}'.",
                        ))
        for opener, line_no, character in stack:
            line = lines[line_no]
            diagnostics.append(self.make(
                line_no,
                utf16_column(line, character),
                utf16_column(line, character + 1),
                f"Expected '{pairs[opener]}' before end of file.",
            ))
        return diagnostics

    def frontend_diagnostics(self, text: str) -> List[dict]:
        result = self.query_frontend(text)
        if not result or result.get("status") == "ok":
            return []
        diagnostics = []
        line_map = LineMap(text)
        for diagnostic in result.get("diagnostics", []):
            line_no = int(diagnostic.get("line", 0))
            start_byte = int(diagnostic.get("character", 0))
            end_byte = int(diagnostic.get("endCharacter", start_byte + 1))
            start = line_map.offset_to_lsp(
                line_map.native_offset(line_no, start_byte)
            )["character"]
            end = line_map.offset_to_lsp(
                line_map.native_offset(line_no, end_byte)
            )["character"]
            diagnostics.append(self.make(
                line_no,
                start,
                end,
                str(diagnostic.get("message", "Syntax error.")),
            ))
        return diagnostics

    def make(self, line_no: int, start: int, end: int, message: str) -> dict:
        return {
            "range": {
                "start": {"line": line_no, "character": start},
                "end": {"line": line_no, "character": max(end, start + 1)},
            },
            "severity": DIAGNOSTIC_SEVERITY_ERROR,
            "source": "mylang",
            "message": message,
        }
