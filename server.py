import json
import subprocess
import re
import sys
from dataclasses import dataclass
from pathlib import Path
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
BUILTIN_TYPES = {"bool", "i8", "i16", "i32", "u8", "u16", "u32", "char", "float", "double", "void", "long", "short"}
DIAGNOSTIC_SEVERITY_ERROR = 1
KEYWORD_PATTERN = r"(?:if|else|while|do|for|switch|case|default|break|continue|return|yield|of|import|from|export|package|rest)"
FUNCTION_DEF_RE = re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]+|\*+[ \t]*)(?!(?:{KEYWORD_PATTERN})\b)([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?=\()")
FUNCTION_CALL_RE = re.compile(rf"\b(?!(?:{KEYWORD_PATTERN})\b)([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?=\()")
PACKAGE_RE = re.compile(r"\b(package|import|from)\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]*\")")
STRUCT_NAME_RE = re.compile(r"\bstruct\s+([A-Za-z_][A-Za-z0-9_]*)")
TYPEDEF_ALIAS_RE = re.compile(r"\btypedef\b[^;{}]*\b([A-Za-z_][A-Za-z0-9_]*)\s*;")
TYPEDEF_STRUCT_ALIAS_RE = re.compile(r"}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;")
TYPE_USAGE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b(?=\s*(?:\*+\s*)?[A-Za-z_][A-Za-z0-9_]*\s*(?:\[[^\]]*\]\s*)?(?:[=;,)]))")
PARAM_NAME_RE = re.compile(r"(?:\b[A-Za-z_][A-Za-z0-9_]*\b\s*(?:\*+\s*)?)([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*$")
PROPERTY_RE = re.compile(r"(?:\.|->)\s*([A-Za-z_][A-Za-z0-9_]*)")

# Lexical token kind (as emitted by tokenkind2str in the C lexer) -> LSP semantic
# token type. Note the C aliases: LAND->"AND", LOR->"OR", ARROW->"MEMBER".
# Kinds not listed (DOT, HASH, UNDERSCORE, brackets, EOT, UNKNOWN) are left
# unhighlighted, matching the previous regex behavior.
KIND_TO_TYPE = {
    "NUMBER": "number",
    "STRING_LITERAL": "string", "CHAR_LITERAL": "string",
    "BOOL": "type", "I32": "type", "U32": "type", "CHAR": "type", "FLOAT": "type",
    "DOUBLE": "type", "VOID": "type", "LONG": "type", "SHORT": "type",
    "REF": "ownershipRef", "MUT": "ownershipMut", "AMPERSAND": "ownershipRef",
    "EQ": "operator", "NEQ": "operator", "LTE": "operator", "GTE": "operator",
    "AND": "operator", "OR": "operator", "LSH": "operator", "RSH": "operator",
    "INC": "operator", "DEC": "operator", "ASTARISK": "operator", "MEMBER": "operator",
    "ADD": "operator", "SUB": "operator", "DIV": "operator", "MOD": "operator",
    "ASSIGN": "operator", "LT": "operator", "GT": "operator", "NOT": "operator",
    "QUESTION": "operator", "COLON": "operator", "BITOR": "operator",
    "BITXOR": "operator", "BITNOT": "operator",
    "CONST": "keyword", "STATIC": "keyword", "EXTERN": "keyword", "AUTO": "keyword",
    "REGISTER": "keyword", "SIZEOF": "keyword", "IF": "keyword", "ELSE": "keyword",
    "WHILE": "keyword", "DO": "keyword", "FOR": "keyword", "SWITCH": "keyword",
    "CASE": "keyword", "DEFAULT": "keyword", "BREAK": "keyword", "CONTINUE": "keyword",
    "RETURN": "keyword", "YIELD": "keyword", "UNCHECKED": "keyword", "OF": "keyword",
    "TYPEDEF": "keyword", "STRUCT": "keyword", "UNION": "keyword", "ENUM": "keyword",
    "IMPORT": "keyword", "FROM": "keyword", "REST": "keyword", "EXPORT": "keyword",
    "PACKAGE": "keyword",
    "IDENTIFIER": "variable",
}

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
        self.repo_root = Path(__file__).resolve().parents[2]
        self.syntax_check_dir = self.repo_root / "toolchain" / "MyLangCompiler"
        self.syntax_check_bin = self.syntax_check_dir / "mylang-syntax-check"
        self.syntax_check_grammar = (
            self.repo_root
            / "toolchain"
            / "MyLangSyntaxEngine"
            / "tests"
            / "fixtures"
            / "grammars"
            / "mylang_lsp.grammar"
        )
        self.syntax_check_build_attempted = False
        self.syntax_check_proc: Optional[subprocess.Popen[bytes]] = None

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
            self.stop_syntax_checker()
            self.running = False
            return
        if method == "textDocument/didOpen":
            td = params["textDocument"]
            self.docs[td["uri"]] = Document(td["uri"], td.get("text", ""), td.get("version", 0))
            self.publish_diagnostics(td["uri"], td.get("text", ""))
            return
        if method == "textDocument/didChange":
            td = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                text = changes[-1].get("text", "")
                self.docs[td["uri"]] = Document(td["uri"], text, td.get("version", 0))
                self.publish_diagnostics(td["uri"], text)
            return
        if method == "textDocument/didClose":
            td = params["textDocument"]
            self.docs.pop(td["uri"], None)
            self.clear_diagnostics(td["uri"])
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

    def publish_diagnostics(self, uri: str, text: str) -> None:
        self.send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": self.syntax_diagnostics(text),
            },
        })

    def clear_diagnostics(self, uri: str) -> None:
        self.send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [],
            },
        })

    def syntax_diagnostics(self, text: str) -> List[dict]:
        diagnostics = self.bracket_diagnostics(text)
        if diagnostics:
            return diagnostics
        diagnostics.extend(self.lr1_diagnostics(text))
        return diagnostics

    def bracket_diagnostics(self, text: str) -> List[dict]:
        lines = text.splitlines()
        protected = self.protected_spans(lines)
        stack: List[Tuple[str, int, int]] = []
        diagnostics: List[dict] = []
        pairs = {"(": ")", "{": "}", "[": "]"}
        closers = {")": "(", "}": "{", "]": "["}

        for line_no, line in enumerate(lines):
            for char_no, ch in enumerate(line):
                if self.is_protected(line_no, char_no, char_no + 1, protected):
                    continue
                if ch in pairs:
                    stack.append((ch, line_no, char_no))
                    continue
                if ch not in closers:
                    continue
                if stack and stack[-1][0] == closers[ch]:
                    stack.pop()
                    continue
                diagnostics.append(self.make_diagnostic(
                    line_no,
                    char_no,
                    char_no + 1,
                    f"Unexpected '{ch}'.",
                ))

        for opener, line_no, char_no in stack:
            diagnostics.append(self.make_diagnostic(
                line_no,
                char_no,
                char_no + 1,
                f"Expected '{pairs[opener]}' before end of file.",
            ))

        return diagnostics

    def lr1_diagnostics(self, text: str) -> List[dict]:
        result = self.query_syntax_checker(text)
        if not result:
            return []
        if result.get("status") == "ok":
            return []

        diagnostics = []
        for diagnostic in result.get("diagnostics", []):
            diagnostics.append(self.make_diagnostic(
                int(diagnostic.get("line", 0)),
                int(diagnostic.get("character", 0)),
                int(diagnostic.get("endCharacter", int(diagnostic.get("character", 0)) + 1)),
                str(diagnostic.get("message", "Syntax error.")),
            ))
        return diagnostics

    def query_syntax_checker(self, text: str) -> Optional[dict]:
        if not self.start_syntax_checker():
            return None
        if not self.syntax_check_proc or not self.syntax_check_proc.stdin:
            return None

        try:
            data = text.encode("utf-8")
            self.syntax_check_proc.stdin.write(f"content {len(data)}\n".encode("ascii"))
            self.syntax_check_proc.stdin.write(data)
            self.syntax_check_proc.stdin.write(b"\n")
            self.syntax_check_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.stop_syntax_checker()
            return None

        try:
            line = self.read_syntax_checker_line()
            if line is None:
                self.stop_syntax_checker()
                return None
            return json.loads(line)
        except (json.JSONDecodeError, OSError):
            self.stop_syntax_checker()
            return None

    def start_syntax_checker(self) -> bool:
        if self.syntax_check_proc and self.syntax_check_proc.poll() is None:
            return True
        if not self.ensure_syntax_checker_binary() or not self.syntax_check_grammar.exists():
            return False

        try:
            self.syntax_check_proc = subprocess.Popen(
                [str(self.syntax_check_bin), "--stdio", str(self.syntax_check_grammar)],
                cwd=str(self.syntax_check_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.syntax_check_proc = None
            return False

        line = self.read_syntax_checker_line()
        if line and line.rstrip("\n") == "ready":
            return True

        self.stop_syntax_checker()
        return False

    def stop_syntax_checker(self) -> None:
        proc = self.syntax_check_proc
        self.syntax_check_proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def read_syntax_checker_line(self) -> Optional[str]:
        proc = self.syntax_check_proc
        if not proc or not proc.stdout:
            return None
        line = proc.stdout.readline()
        return line.decode("utf-8") if line else None

    def ensure_syntax_checker_binary(self) -> bool:
        if self.syntax_check_bin.exists():
            return True
        if self.syntax_check_build_attempted:
            return False

        self.syntax_check_build_attempted = True
        if not self.syntax_check_dir.exists():
            return False

        try:
            subprocess.run(
                ["make", "syntax-check"],
                cwd=str(self.syntax_check_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        return self.syntax_check_bin.exists()

    def make_diagnostic(self, line_no: int, start: int, end: int, message: str) -> dict:
        return {
            "range": {
                "start": {"line": line_no, "character": start},
                "end": {"line": line_no, "character": max(end, start + 1)},
            },
            "severity": DIAGNOSTIC_SEVERITY_ERROR,
            "source": "mylang",
            "message": message,
        }

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
        lines = text.splitlines()
        protected = self.protected_spans(lines)

        # Base layer: lexical tokens straight from the C lexer (via the syntax
        # checker). Replaces the old per-line lexing regexes (strings, numbers,
        # operators, keywords, builtin types, identifiers).
        base: Dict[Tuple[int, int], Tuple[int, str]] = {}
        result = self.query_syntax_checker(text)
        if result:
            for entry in result.get("tokens", []):
                line_no, col, length, kind = entry[0], entry[1], entry[2], entry[3]
                token_type = KIND_TO_TYPE.get(kind)
                if not token_type:
                    continue
                if 0 <= line_no < len(lines):
                    # LSP semantic tokens cannot cross a line boundary; clamp.
                    max_len = max(0, len(lines[line_no]) - col)
                    if max_len:
                        length = min(length, max_len)
                base[(line_no, col)] = (length, token_type)

        # Overlay layer: context-sensitive classification the lexer cannot do
        # (function vs type vs property vs parameter vs namespace). Overrides the
        # base IDENTIFIER->variable. The C lexer drops comments, so comments are
        # still detected here.
        overlay: Dict[Tuple[int, int], Tuple[int, str]] = {}
        for line_no, line in enumerate(lines):
            for span_start, span_end in protected.get(line_no, []):
                if span_start >= len(line):
                    continue
                if line[span_start] == "/" and span_start + 1 < len(line) and line[span_start + 1] in ("/", "*"):
                    overlay[(line_no, span_start)] = (span_end - span_start, "comment")
            for match in PACKAGE_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(2)
                if not name.startswith('"'):
                    overlay[(line_no, match.start(2))] = (len(name), "namespace")
            for match in TYPEDEF_ALIAS_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                alias = match.group(1)
                overlay[(line_no, match.start(1))] = (len(alias), "type")
            for match in TYPEDEF_STRUCT_ALIAS_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                alias = match.group(1)
                overlay[(line_no, match.start(1))] = (len(alias), "struct")
            for match in STRUCT_NAME_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                overlay[(line_no, match.start(1))] = (len(name), "struct")
            for match in FUNCTION_DEF_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                ret_type, fn_name = match.group(1), match.group(2)
                kind = "type" if ret_type[0].isupper() else "type" if ret_type in BUILTIN_TYPES else None
                if kind:
                    overlay[(line_no, match.start(1))] = (len(ret_type), kind)
                overlay[(line_no, match.start(2))] = (len(fn_name), "function")
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
                                overlay[(line_no, name_start)] = (len(name), "parameter")
                        offset = part_start + len(part) + 1 if part_start >= 0 else offset + len(part) + 1
            for match in TYPE_USAGE_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                token_type = "type" if (name in BUILTIN_TYPES or name[:1].isupper()) else None
                if token_type:
                    overlay[(line_no, match.start(1))] = (len(name), token_type)
            for match in FUNCTION_CALL_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                if name in KEYWORDS:
                    continue
                overlay[(line_no, match.start(1))] = (len(name), "function")
            for match in PROPERTY_RE.finditer(line):
                if self.is_protected(line_no, match.start(), match.end(), protected):
                    continue
                name = match.group(1)
                overlay[(line_no, match.start(1))] = (len(name), "property")

        # Overlay wins over base at the same start position.
        merged = dict(base)
        merged.update(overlay)

        encoded: List[int] = []
        prev_line = 0
        prev_start = 0
        for (line_no, start), (length, token_type) in sorted(merged.items()):
            if token_type not in TOKEN_TYPE_INDEX:
                continue
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
