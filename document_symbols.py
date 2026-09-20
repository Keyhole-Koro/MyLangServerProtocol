"""Document-symbol projection from native frontend declaration spans."""

from typing import Callable, List, Optional

from document_model import LineMap


SYMBOL_KIND = {
    "file": 1, "module": 2, "namespace": 3, "package": 4, "class": 5,
    "method": 6, "property": 7, "field": 8, "constructor": 9, "enum": 10,
    "interface": 11, "function": 12, "variable": 13, "constant": 14,
    "string": 15, "number": 16, "boolean": 17, "array": 18, "object": 19,
    "key": 20, "null": 21, "enumMember": 22, "struct": 23, "event": 24,
    "operator": 25, "typeParameter": 26,
}

ENGINE_SYMBOL_KIND = {
    "function": "function",
    "method": "method",
    "struct": "struct",
    "enum": "enum",
    "type": "struct",
    "variable": "variable",
}


class DocumentSymbolService:
    def __init__(self, query_frontend: Callable[[str], Optional[dict]]) -> None:
        self.query_frontend = query_frontend

    def symbols(self, text: str) -> List[dict]:
        result = self.query_frontend(text)
        if not result:
            return []
        lines = text.splitlines()
        line_map = LineMap(text)
        symbols = []
        for entry in result.get("symbols", []):
            line_no, column, length, kind = entry[:4]
            if not 0 <= line_no < len(lines):
                continue
            start_offset = line_map.native_offset(line_no, column)
            end_offset = line_map.native_offset(line_no, column + length)
            name = line_map.data[start_offset:end_offset].decode(
                "utf-8", errors="replace"
            )
            start = line_map.offset_to_lsp(start_offset)["character"]
            end = line_map.offset_to_lsp(end_offset)["character"]
            symbol_kind = SYMBOL_KIND[ENGINE_SYMBOL_KIND.get(kind, "variable")]
            symbols.append(self.make(name, symbol_kind, line_no, start, end))
        return symbols

    def make(
        self, name: str, kind: int, line_no: int, start: int, end: int
    ) -> dict:
        span = {
            "start": {"line": line_no, "character": start},
            "end": {"line": line_no, "character": end},
        }
        return {
            "name": name,
            "kind": kind,
            "range": span,
            "selectionRange": span,
        }
