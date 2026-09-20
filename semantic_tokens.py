"""Semantic-token classification and LSP delta encoding."""

import re
from typing import Callable, Dict, List, Optional, Tuple

from document_model import LineMap
from source_text import ProtectedSpans, protected_spans, utf16_column


TOKEN_TYPES = [
    "comment", "docTag", "string", "keyword", "operator", "ownershipRef",
    "ownershipMut", "namespace", "number", "type", "struct", "function",
    "method", "parameter", "variable", "property", "enum", "enumMember",
    "result", "resultVariant",
]
TOKEN_MODIFIERS: List[str] = []
TOKEN_TYPE_INDEX = {name: index for index, name in enumerate(TOKEN_TYPES)}


# Lexical token kind (as emitted by tokenkind2str in the C lexer) -> LSP type.
KIND_TO_TYPE = {
    "NUMBER": "number",
    "STRING_LITERAL": "string", "CHAR_LITERAL": "string",
    "TRUE_LITERAL": "variable", "FALSE_LITERAL": "variable",
    "BOOL": "type", "U8": "type", "U16": "type", "I32": "type",
    "U32": "type", "CHAR": "type", "FLOAT": "type", "DOUBLE": "type",
    "VOID": "type", "LONG": "type", "SHORT": "type",
    "REF": "ownershipRef", "MUT": "ownershipMut", "AMPERSAND": "ownershipRef",
    "EQ": "operator", "NEQ": "operator", "LTE": "operator", "GTE": "operator",
    "AND": "operator", "OR": "operator", "LSH": "operator", "RSH": "operator",
    "INC": "operator", "DEC": "operator", "ASTARISK": "operator",
    "MEMBER": "operator", "ADD": "operator", "SUB": "operator",
    "DIV": "operator", "MOD": "operator", "ASSIGN": "operator",
    "LT": "operator", "GT": "operator", "NOT": "operator",
    "QUESTION": "operator", "COLON": "operator", "BITOR": "operator",
    "BITXOR": "operator", "BITNOT": "operator",
    "CONST": "keyword", "STATIC": "keyword", "EXTERN": "keyword",
    "AUTO": "keyword", "REGISTER": "keyword", "SIZEOF": "keyword",
    "IF": "keyword", "ELSE": "keyword", "WHILE": "keyword", "DO": "keyword",
    "FOR": "keyword", "SWITCH": "keyword", "CASE": "keyword",
    "DEFAULT": "keyword", "BREAK": "keyword", "CONTINUE": "keyword",
    "RETURN": "keyword", "YIELD": "keyword", "UNCHECKED": "keyword",
    "OF": "keyword", "TYPEDEF": "keyword", "STRUCT": "keyword",
    "UNION": "keyword", "ENUM": "keyword", "IMPORT": "keyword",
    "FROM": "keyword", "REST": "keyword", "EXPORT": "keyword",
    "PACKAGE": "keyword", "TEST": "keyword", "IDENTIFIER": "variable",
}


class SemanticTokenService:
    def __init__(self, query_frontend: Callable[[str], Optional[dict]]) -> None:
        self.query_frontend = query_frontend

    def encode(self, text: str) -> List[int]:
        lines = text.splitlines()
        line_map = LineMap(text)
        protected = protected_spans(lines)
        base: Dict[Tuple[int, int], Tuple[int, str]] = {}
        engine: Dict[Tuple[int, int], Tuple[int, str]] = {}
        result = self.query_frontend(text)
        if result:
            for entry in result.get("tokens", []):
                line_no, column, length, kind = entry[0], entry[1], entry[2], entry[3]
                if 0 <= line_no < len(lines):
                    start = line_map.offset_to_lsp(line_map.native_offset(line_no, column))
                    end = line_map.offset_to_lsp(line_map.native_offset(line_no, column + length))
                    column = start["character"]
                    length = max(0, end["character"] - column)
                token_type = KIND_TO_TYPE.get(kind)
                if token_type and length:
                    base[(line_no, column)] = (length, token_type)
                if length and len(entry) > 4 and entry[4] in TOKEN_TYPE_INDEX:
                    engine[(line_no, column)] = (length, entry[4])

            for line_no, column, length in self.generic_type_argument_spans(
                lines, result.get("tokens", [])
            ):
                start = line_map.offset_to_lsp(line_map.native_offset(line_no, column))
                end = line_map.offset_to_lsp(line_map.native_offset(line_no, column + length))
                utf16_column_value = start["character"]
                utf16_length = max(0, end["character"] - utf16_column_value)
                if utf16_length:
                    engine[(line_no, utf16_column_value)] = (utf16_length, "type")

        merged = dict(base)
        merged.update(engine)
        merged.update(self.comment_tokens(lines, protected))
        return self._delta_encode(merged)

    def _delta_encode(
        self, tokens: Dict[Tuple[int, int], Tuple[int, str]]
    ) -> List[int]:
        encoded: List[int] = []
        previous_line = 0
        previous_start = 0
        for (line_no, start), (length, token_type) in sorted(tokens.items()):
            if token_type not in TOKEN_TYPE_INDEX:
                continue
            delta_line = line_no - previous_line
            delta_start = start - previous_start if delta_line == 0 else start
            encoded.extend([
                delta_line,
                delta_start,
                length,
                TOKEN_TYPE_INDEX[token_type],
                0,
            ])
            previous_line = line_no
            previous_start = start
        return encoded

    def comment_tokens(
        self,
        lines: List[str],
        protected: ProtectedSpans,
    ) -> Dict[Tuple[int, int], Tuple[int, str]]:
        tokens: Dict[Tuple[int, int], Tuple[int, str]] = {}
        in_block_comment = False
        block_is_doc = False

        def add(line_no: int, line: str, start: int, end: int, token_type: str) -> None:
            if end <= start:
                return
            utf16_start = utf16_column(line, start)
            utf16_end = utf16_column(line, end)
            if utf16_end > utf16_start:
                tokens[(line_no, utf16_start)] = (utf16_end - utf16_start, token_type)

        for line_no, line in enumerate(lines):
            for span_start, span_end in protected.get(line_no, []):
                if span_start >= len(line):
                    continue
                segment = line[span_start:span_end]
                is_comment = False
                is_doc = False
                if in_block_comment:
                    is_comment = True
                    is_doc = block_is_doc
                    if "*/" in segment:
                        in_block_comment = False
                        block_is_doc = False
                elif segment.startswith("//"):
                    is_comment = True
                    is_doc = segment.startswith("///")
                elif segment.startswith("/*"):
                    is_comment = True
                    is_doc = segment.startswith("/**")
                    if "*/" not in segment:
                        in_block_comment = True
                        block_is_doc = is_doc
                if not is_comment:
                    continue

                cursor = span_start
                if is_doc:
                    for match in re.finditer(r"@[A-Za-z_][A-Za-z0-9_]*", segment):
                        tag_start = span_start + match.start()
                        tag_end = span_start + match.end()
                        add(line_no, line, cursor, tag_start, "comment")
                        add(line_no, line, tag_start, tag_end, "docTag")
                        cursor = tag_end
                add(line_no, line, cursor, span_end, "comment")
        return tokens

    def generic_type_argument_spans(
        self, lines: List[str], tokens: List[list]
    ) -> List[Tuple[int, int, int]]:
        def kind(index: int) -> str:
            return str(tokens[index][3])

        def lexeme(index: int) -> str:
            line_no, column, length = map(int, tokens[index][:3])
            if not 0 <= line_no < len(lines):
                return ""
            data = lines[line_no].encode("utf-8")
            return data[column:column + length].decode("utf-8", errors="replace")

        def find_close(open_index: int) -> Optional[int]:
            depth = 1
            for index in range(open_index + 1, len(tokens)):
                if kind(index) == "LT":
                    depth += 1
                elif kind(index) == "GT":
                    depth -= 1
                elif kind(index) == "RSH":
                    depth -= 2
                if depth <= 0:
                    return index
            return None

        candidates = set()
        for index in range(len(tokens) - 1):
            if kind(index) != "IMPORT" or kind(index + 1) != "L_BRACE":
                continue
            cursor = index + 2
            while cursor < len(tokens) and kind(cursor) != "R_BRACE":
                if kind(cursor) == "IDENTIFIER":
                    candidates.add(lexeme(cursor))
                cursor += 1

        for index in range(len(tokens) - 2):
            if kind(index) == "STRUCT" and kind(index + 1) == "IDENTIFIER" and kind(index + 2) == "LT":
                candidates.add(lexeme(index + 1))
            if kind(index) != "IDENTIFIER" or kind(index + 1) != "LT":
                continue
            close = find_close(index + 1)
            if close is not None and close + 1 < len(tokens) and kind(close + 1) == "L_PARENTHESES":
                candidates.add(lexeme(index))

        spans: List[Tuple[int, int, int]] = []
        for index in range(len(tokens) - 1):
            if kind(index) != "IDENTIFIER" or lexeme(index) not in candidates or kind(index + 1) != "LT":
                continue
            close = find_close(index + 1)
            if close is None:
                continue
            for argument_index in range(index + 2, close):
                if kind(argument_index) == "IDENTIFIER":
                    spans.append(tuple(map(int, tokens[argument_index][:3])))
        return spans
