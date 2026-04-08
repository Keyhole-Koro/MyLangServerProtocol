import json
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

TOKEN_TYPES = [
    "comment",
    "string",
    "keyword",
    "operator",
    "ownershipRef",
    "ownershipMut",
    "namespace",
    "number",
    "type",
    "struct",
    "function",
    "method",
    "parameter",
    "variable",
    "property",
]
TOKEN_MODIFIERS: List[str] = []
TOKEN_TYPE_INDEX = {name: i for i, name in enumerate(TOKEN_TYPES)}

KEYWORDS = {
    "if", "else", "while", "do", "for", "switch", "case", "default",
    "break", "continue", "return", "yield", "of", "import", "from",
    "export", "package", "typedef", "struct", "const", "static", "extern",
    "auto", "register", "union", "enum", "ref", "mut", "unchecked", "rest",
}
OWNERSHIP_REF_WORDS = {"ref"}
OWNERSHIP_MUT_WORDS = {"mut"}
BUILTIN_TYPES = {"bool", "i8", "i16", "i32", "u8", "u16", "u32", "char", "float", "double", "void", "long", "short"}
BOOLS = {"true", "false", "null"}
TYPE_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
NUMBER_RE = re.compile(r"\b(?:0x[0-9A-Fa-f]+|[0-9]+)\b")
STRING_RE = re.compile(r'"([^"\\]|\\.)*"')
CHAR_RE = re.compile(r"'([^'\\]|\\.)*'")
KEYWORD_PATTERN = r"(?:if|else|while|do|for|switch|case|default|break|continue|return|yield|of|import|from|export|package|rest)"
FUNCTION_DEF_RE = re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]+|\*+[ \t]*)(?!(?:{KEYWORD_PATTERN})\b)([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?=\()")
FUNCTION_CALL_RE = re.compile(rf"\b(?!(?:{KEYWORD_PATTERN})\b)([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?=\()")
PACKAGE_RE = re.compile(r"\b(package|import|from)\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]*\")")
STRUCT_NAME_RE = re.compile(r"\bstruct\s+([A-Za-z_][A-Za-z0-9_]*)")
TYPEDEF_ALIAS_RE = re.compile(r"\btypedef\b[^;{}]*\b([A-Za-z_][A-Za-z0-9_]*)\s*;")
TYPEDEF_STRUCT_ALIAS_RE = re.compile(r"}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;")
TYPE_USAGE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b(?=\s*(?:\*+\s*)?[A-Za-z_][A-Za-z0-9_]*\s*(?:\[[^\]]*\]\s*)?(?:[=;,)]))")
PARAM_SPLIT_RE = re.compile(r",")
PARAM_NAME_RE = re.compile(r"(?:\b[A-Za-z_][A-Za-z0-9_]*\b\s*(?:\*+\s*)?)([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*$")
PROPERTY_RE = re.compile(r"(?:\.|->)\s*([A-Za-z_][A-Za-z0-9_]*)")
OWNERSHIP_OP_RE = re.compile(r"&mut\b|&(?!&)")
OPERATOR_RE = re.compile(r"==|!=|<=|>=|&&|\|\||<<|>>|->|\+\+|--|[=+\-*/%<>&|^~?:!]")

SYMBOL_KIND = {
    "file": 1,
    "module": 2,
    "namespace": 3,
    "package": 4,
    "class": 5,
    "method": 6,
    "property": 7,
    "field": 8,
    "constructor": 9,
    "enum": 10,
    "interface": 11,
    "function": 12,
    "variable": 13,
    "constant": 14,
    "string": 15,
    "number": 16,
    "boolean": 17,
    "array": 18,
    "object": 19,
    "key": 20,
    "null": 21,
    "enumMember": 22,
    "struct": 23,
    "event": 24,
    "operator": 25,
    "typeParameter": 26,
}

@dataclass
class Document:
    uri: str
    text: str
    version: int


class LspServer:
    def __init__(self) -> None:
        self.docs: Dict[str, Document] = {}
        self.running = True

    def send(self, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    def send_response(self, id_value, result) -> None:
        self.send({"jsonrpc": "2.0", "id": id_value, "result": result})

    def send_error(self, id_value, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": id_value, "error": {"code": code, "message": message}})

    def read_message(self) -> Optional[dict]:
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            try:
                key, _, value = line.decode("utf-8").partition(":")
                headers[key.strip().lower()] = value.strip()
            except Exception:
                continue
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = sys.stdin.buffer.read(length)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def uri_to_path(self, uri: str) -> Optional[str]:
        if uri.startswith("file://"):
            parsed = urlparse(uri)
            return unquote(parsed.path)
        return None

    def get_doc(self, uri: str) -> Optional[Document]:
        doc = self.docs.get(uri)
        if doc:
            return doc
        path = self.uri_to_path(uri)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                doc = Document(uri=uri, text=text, version=0)
                self.docs[uri] = doc
                return doc
            except OSError:
                return None
        return None

    def run(self) -> None:
        while self.running:
            try:
                msg = self.read_message()
            except Exception:
                break
            if msg is None:
                break
            try:
                self.handle(msg)
            except Exception as e:
                id_value = msg.get("id") if isinstance(msg, dict) else None
                if id_value is not None:
                    try:
                        self.send_error(id_value, -32603, f"Internal error: {e}")
                    except Exception:
                        pass

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        id_value = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            result = {
                "capabilities": {
                    "textDocumentSync": 1,
                    "semanticTokensProvider": {
                        "legend": {
                            "tokenTypes": TOKEN_TYPES,
                            "tokenModifiers": TOKEN_MODIFIERS,
                        },
                        "full": True,
                    },
                    "documentSymbolProvider": True,
                },
                "serverInfo": {
                    "name": "mylang-lsp",
                    "version": "0.1.0",
                },
            }
            self.send_response(id_value, result)
            return
        if method == "initialized":
            return
        if method == "shutdown":
            self.send_response(id_value, None)
            return
        if method == "exit":
            self.running = False
            return
        if method == "textDocument/didOpen":
            td = params["textDocument"]
            self.docs[td["uri"]] = Document(td["uri"], td.get("text", ""), td.get("version", 0))
            return
        if method == "textDocument/didChange":
            td = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                self.docs[td["uri"]] = Document(td["uri"], changes[-1].get("text", ""), td.get("version", 0))
            return
        if method == "textDocument/didClose":
            td = params["textDocument"]
            self.docs.pop(td["uri"], None)
            return
        if method == "textDocument/semanticTokens/full":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_error(id_value, -32602, f"Document not found: {uri}")
                return
            self.send_response(id_value, {"data": self.semantic_tokens(doc.text)})
            return
        if method == "textDocument/documentSymbol":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_error(id_value, -32602, f"Document not found: {uri}")
                return
            self.send_response(id_value, self.document_symbols(doc.text))
            return

        if id_value is not None:
            self.send_error(id_value, -32601, f"Method not found: {method}")

    def find_matching_close_paren(self, line: str, open_pos: int) -> int:
        depth = 1
        i = open_pos + 1
        while i < len(line):
            if line[i] == "(":
                depth += 1
            elif line[i] == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def split_params(self, params: str) -> List[str]:
        parts: List[str] = []
        depth = 0
        current: List[str] = []
        for ch in params:
            if ch in ("(", "["):
                depth += 1
            elif ch in (")", "]"):
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append("".join(current))
        return parts

    def semantic_tokens(self, text: str) -> List[int]:
        tokens: List[Tuple[int, int, int, str]] = []
        lines = text.splitlines()
        protected = self.protected_spans(lines)
        for line_no, line in enumerate(lines):
            for span_start, span_end in protected.get(line_no, []):
                if span_start >= len(line):
                    continue
                first = line[span_start]
                if first == "/" and span_start + 1 < len(line) and line[span_start + 1] in ("/", "*"):
                    tokens.append((line_no, span_start, span_end - span_start, "comment"))
                elif first in ('"', "'"):
                    tokens.append((line_no, span_start, span_end - span_start, "string"))
            for match in OWNERSHIP_OP_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                value = match.group(0)
                token_type = "ownershipMut" if value.startswith("&mut") else "ownershipRef"
                tokens.append((line_no, match.start(), len(value), token_type))
            for match in IDENT_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                value = match.group(0)
                if value in OWNERSHIP_REF_WORDS:
                    tokens.append((line_no, match.start(), len(value), "ownershipRef"))
                    continue
                if value in OWNERSHIP_MUT_WORDS:
                    tokens.append((line_no, match.start(), len(value), "ownershipMut"))
                    continue
                if value in KEYWORDS:
                    tokens.append((line_no, match.start(), len(value), "keyword"))
            for match in PACKAGE_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(2)
                if not name.startswith('"'):
                    tokens.append((line_no, match.start(2), len(name), "namespace"))
            for match in TYPEDEF_ALIAS_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                alias = match.group(1)
                tokens.append((line_no, match.start(1), len(alias), "type"))
            for match in TYPEDEF_STRUCT_ALIAS_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                alias = match.group(1)
                tokens.append((line_no, match.start(1), len(alias), "struct"))
            for match in STRUCT_NAME_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                tokens.append((line_no, match.start(1), len(name), "struct"))
            for match in FUNCTION_DEF_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                ret_type, fn_name = match.group(1), match.group(2)
                kind = "type" if ret_type[0].isupper() else "type" if ret_type in BUILTIN_TYPES else None
                if kind:
                    tokens.append((line_no, match.start(1), len(ret_type), kind))
                tokens.append((line_no, match.start(2), len(fn_name), "function"))
                open_paren = line.find("(", match.end())
                close_paren = self.find_matching_close_paren(line, open_paren) if open_paren >= 0 else -1
                if open_paren >= 0 and close_paren > open_paren:
                    params = line[open_paren + 1:close_paren]
                    offset = open_paren + 1
                    for part in self.split_params(params):
                        m = PARAM_NAME_RE.search(part.strip())
                        if not m:
                            offset += len(part) + 1
                            continue
                        name = m.group(1)
                        part_start = line.find(part, offset)
                        if part_start >= 0:
                            name_start = line.find(name, part_start)
                            if name_start >= 0:
                                tokens.append((line_no, name_start, len(name), "parameter"))
                        offset = part_start + len(part) + 1 if part_start >= 0 else offset + len(part) + 1
            for match in TYPE_USAGE_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                token_type = "type" if (name in BUILTIN_TYPES or name[:1].isupper()) else None
                if token_type:
                    tokens.append((line_no, match.start(1), len(name), token_type))
            for match in FUNCTION_CALL_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                if name in KEYWORDS:
                    continue
                tokens.append((line_no, match.start(1), len(name), "function"))
            for match in PROPERTY_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                tokens.append((line_no, match.start(1), len(name), "property"))
            for match in NUMBER_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                tokens.append((line_no, match.start(), len(match.group(0)), "number"))
            for match in OPERATOR_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                tokens.append((line_no, match.start(), len(match.group(0)), "operator"))
            for match in IDENT_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                value = match.group(0)
                if value in BOOLS:
                    tokens.append((line_no, match.start(), len(value), "variable"))
                elif value in BUILTIN_TYPES:
                    tokens.append((line_no, match.start(), len(value), "type"))
                elif value not in KEYWORDS:
                    tokens.append((line_no, match.start(), len(value), "variable"))

        tokens.sort(key=lambda t: (t[0], t[1], -t[2]))
        filtered: List[Tuple[int, int, int, str]] = []
        occupied = set()
        for line_no, start, length, token_type in tokens:
            key = (line_no, start)
            if key in occupied:
                continue
            occupied.add(key)
            filtered.append((line_no, start, length, token_type))

        encoded: List[int] = []
        prev_line = 0
        prev_start = 0
        for line_no, start, length, token_type in filtered:
            delta_line = line_no - prev_line
            delta_start = start - prev_start if delta_line == 0 else start
            encoded.extend([delta_line, delta_start, length, TOKEN_TYPE_INDEX[token_type], 0])
            prev_line = line_no
            prev_start = start
        return encoded

    def protected_spans(self, lines: List[str]) -> Dict[int, List[Tuple[int, int]]]:
        spans: Dict[int, List[Tuple[int, int]]] = {}
        in_block_comment = False
        for line_no, line in enumerate(lines):
            line_spans: List[Tuple[int, int]] = []
            i = 0
            while i < len(line):
                if in_block_comment:
                    end = line.find("*/", i)
                    if end < 0:
                        line_spans.append((i, len(line)))
                        i = len(line)
                        break
                    line_spans.append((i, end + 2))
                    i = end + 2
                    in_block_comment = False
                    continue

                ch = line[i]
                nxt = line[i + 1] if i + 1 < len(line) else ""

                if ch == '"':
                    end = i + 1
                    escaped = False
                    while end < len(line):
                        if escaped:
                            escaped = False
                        elif line[end] == "\\":
                            escaped = True
                        elif line[end] == '"':
                            end += 1
                            break
                        end += 1
                    line_spans.append((i, end))
                    i = end
                    continue

                if ch == "'":
                    end = i + 1
                    escaped = False
                    while end < len(line):
                        if escaped:
                            escaped = False
                        elif line[end] == "\\":
                            escaped = True
                        elif line[end] == "'":
                            end += 1
                            break
                        end += 1
                    line_spans.append((i, end))
                    i = end
                    continue

                if ch == "/" and nxt == "/":
                    line_spans.append((i, len(line)))
                    i = len(line)
                    break

                if ch == "/" and nxt == "*":
                    end = line.find("*/", i + 2)
                    if end < 0:
                        line_spans.append((i, len(line)))
                        in_block_comment = True
                        i = len(line)
                        break
                    line_spans.append((i, end + 2))
                    i = end + 2
                    continue

                i += 1

            if line_spans:
                spans[line_no] = line_spans
        return spans

    def is_protected(self, line_no: int, start: int, end: int, protected: Dict[int, List[Tuple[int, int]]]) -> bool:
        for span_start, span_end in protected.get(line_no, []):
            if start < span_end and end > span_start:
                return True
        return False

    def document_symbols(self, text: str) -> List[dict]:
        symbols = []
        lines = text.splitlines()
        for line_no, line in enumerate(lines):
            m = TYPEDEF_ALIAS_RE.search(line)
            if m:
                name = m.group(1)
                symbols.append(self.make_symbol(name, SYMBOL_KIND["struct"], line_no, m.start(1), m.end(1)))
            m = TYPEDEF_STRUCT_ALIAS_RE.search(line)
            if m:
                name = m.group(1)
                symbols.append(self.make_symbol(name, SYMBOL_KIND["struct"], line_no, m.start(1), m.end(1)))
            m = STRUCT_NAME_RE.search(line)
            if m:
                name = m.group(1)
                symbols.append(self.make_symbol(name, SYMBOL_KIND["struct"], line_no, m.start(1), m.end(1)))
            m = FUNCTION_DEF_RE.search(line)
            if m:
                name = m.group(2)
                symbols.append(self.make_symbol(name, SYMBOL_KIND["function"], line_no, m.start(2), m.end(2)))
                continue
            m = TYPE_USAGE_RE.search(line)
            if m and "(" not in line:
                ident_match = re.search(r"(?:\*+\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*(?:=|;)", line)
                if ident_match:
                    name = ident_match.group(1)
                    symbols.append(self.make_symbol(name, SYMBOL_KIND["variable"], line_no, ident_match.start(1), ident_match.end(1)))
        return symbols

    def make_symbol(self, name: str, kind: int, line_no: int, start: int, end: int) -> dict:
        return {
            "name": name,
            "kind": kind,
            "range": {
                "start": {"line": line_no, "character": start},
                "end": {"line": line_no, "character": end},
            },
            "selectionRange": {
                "start": {"line": line_no, "character": start},
                "end": {"line": line_no, "character": end},
            },
        }


if __name__ == "__main__":
    LspServer().run()
