"""Versioned documents and UTF-8/UTF-16 position conversion."""

from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

@dataclass(frozen=True)
class SourceSpan:
    """Half-open UTF-8 byte offsets in one document snapshot."""

    start: int
    end: int


class LineMap:
    """Convert between native UTF-8 byte offsets and LSP UTF-16 positions."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.data = text.encode("utf-8")
        self.lines = text.split("\n")
        self.line_byte_starts: List[int] = []
        offset = 0
        for index, line in enumerate(self.lines):
            self.line_byte_starts.append(offset)
            offset += len(line.encode("utf-8"))
            if index + 1 < len(self.lines):
                offset += 1

    def native_offset(self, line: int, byte_column: int) -> int:
        if not self.line_byte_starts:
            return 0
        line = min(max(line, 0), len(self.line_byte_starts) - 1)
        line_size = len(self.lines[line].encode("utf-8"))
        return self.line_byte_starts[line] + min(max(byte_column, 0), line_size)

    def lsp_to_offset(self, position: dict) -> int:
        line = int(position.get("line", 0))
        character = int(position.get("character", 0))
        if not self.lines:
            return 0
        line = min(max(line, 0), len(self.lines) - 1)
        character = max(character, 0)
        byte_column = 0
        utf16_column = 0
        for char in self.lines[line]:
            width = len(char.encode("utf-16-le")) // 2
            if utf16_column + width > character:
                break
            utf16_column += width
            byte_column += len(char.encode("utf-8"))
        return self.line_byte_starts[line] + byte_column

    def offset_to_lsp(self, offset: int) -> dict:
        offset = min(max(offset, 0), len(self.data))
        line = max(0, bisect_right(self.line_byte_starts, offset) - 1)
        byte_column = offset - self.line_byte_starts[line]
        line_data = self.lines[line].encode("utf-8")
        prefix = line_data[:min(byte_column, len(line_data))]
        while prefix:
            try:
                text = prefix.decode("utf-8")
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
        else:
            text = ""
        character = len(text.encode("utf-16-le")) // 2
        return {"line": line, "character": character}

    def range(self, span: SourceSpan) -> dict:
        return {
            "start": self.offset_to_lsp(span.start),
            "end": self.offset_to_lsp(span.end),
        }

    def slice(self, span: SourceSpan) -> str:
        return self.data[span.start:span.end].decode("utf-8", errors="replace")


@dataclass(frozen=True)
class DocumentSnapshot:
    uri: str
    text: str
    version: int
    line_map: LineMap = field(compare=False)

    @classmethod
    def create(cls, uri: str, text: str, version: int) -> "DocumentSnapshot":
        return cls(uri=uri, text=text, version=version, line_map=LineMap(text))


class DocumentStore:
    def __init__(self) -> None:
        self._documents: Dict[str, DocumentSnapshot] = {}

    @property
    def documents(self) -> Dict[str, DocumentSnapshot]:
        return self._documents

    def update(self, uri: str, text: str, version: int) -> DocumentSnapshot:
        snapshot = DocumentSnapshot.create(uri, text, version)
        self._documents[uri] = snapshot
        return snapshot

    def get(self, uri: str) -> Optional[DocumentSnapshot]:
        return self._documents.get(uri)

    def close(self, uri: str) -> None:
        self._documents.pop(uri, None)

    def load(self, uri: str, path: str) -> Optional[DocumentSnapshot]:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return None
        return self.update(uri, text, 0)

