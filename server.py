import hashlib
import json
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from queue import PriorityQueue
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from lsp_analysis import (
    DocumentSnapshot,
    DocumentStore,
    FrontendBackend,
    LanguageFeatures,
    LineMap,
    WorkspaceIndex,
)

TOKEN_TYPES = [
    "comment",
    "docTag",
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
    "enum",
    "enumMember",
    "result",
    "resultVariant",
]
TOKEN_MODIFIERS: List[str] = []
TOKEN_TYPE_INDEX = {name: i for i, name in enumerate(TOKEN_TYPES)}

DIAGNOSTIC_SEVERITY_ERROR = 1

# Lexical token kind (as emitted by tokenkind2str in the C lexer) -> LSP semantic
# token type. Note the C aliases: LAND->"AND", LOR->"OR", ARROW->"MEMBER".
# Kinds not listed (DOT, HASH, UNDERSCORE, brackets, EOT, UNKNOWN) are left
# unhighlighted, matching the previous regex behavior.
KIND_TO_TYPE = {
    "NUMBER": "number",
    "STRING_LITERAL": "string", "CHAR_LITERAL": "string",
    "TRUE_LITERAL": "variable", "FALSE_LITERAL": "variable",
    "BOOL": "type", "U8": "type", "U16": "type", "I32": "type", "U32": "type", "CHAR": "type", "FLOAT": "type",
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
    # `test` is reserved, but it reads as a namespace wherever it is still a
    # plain name (`package test;`, `test.pass()`); the grammar's @namespace
    # annotation retags those. This is the fallback for an unparsable file.
    "TEST": "keyword",
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

# Engine symbol-kind string -> SYMBOL_KIND key (type aliases shown as struct).
ENGINE_SYMBOL_KIND = {
    "function": "function",
    "method": "method",
    "struct": "struct",
    "enum": "enum",
    "type": "struct",
    "variable": "variable",
}

class LspServer:
    def __init__(self) -> None:
        self.document_store = DocumentStore()
        # Compatibility alias for existing tests and small integrations.
        self.docs = self.document_store.documents
        self.running = True
        self.repo_root = Path(__file__).resolve().parents[2]
        self.syntax_check_dir = self.repo_root / "toolchain" / "MyLangCompiler"
        self.syntax_check_bin = self.syntax_check_dir / "mylang-syntax-check"
        base_g = self.repo_root / "toolchain" / "MySyntaxEngine" / "tests" / "fixtures" / "grammars" / "mylang_lsp.grammar"
        mlx_g = self.repo_root / "toolchain" / "MySyntaxEngine" / "tests" / "fixtures" / "grammars" / "mlx.grammar"
        self.syntax_check_grammar = f"{base_g},{mlx_g}"
        self.syntax_check_cache = self.syntax_check_dir / "mylang-syntax-check-lsp.table"
        self.grammar_path_obj = base_g
        self.syntax_check_build_attempted = False
        self.syntax_check_proc: Optional[subprocess.Popen[bytes]] = None
        # Diagnostics, editor features, and semantic highlighting all consume
        # the same frontend response. Keep a small content-addressed cache so
        # opening a document does not parse identical text several times.
        self.syntax_result_cache: OrderedDict[bytes, dict] = OrderedDict()
        self.syntax_result_cache_limit = 16
        self.workspace_index = WorkspaceIndex()
        self.frontend_analysis = FrontendBackend(self.query_syntax_checker)
        self.language_features = LanguageFeatures(self.frontend_analysis, self.workspace_index)
        self.indexing_documents: set[str] = set()
        self.open_documents: set[str] = set()
        self.semantic_tokens_enabled = True
        self.pending_diagnostics: Dict[str, int] = {}
        self.send_lock = threading.Lock()
        self.cancel_lock = threading.Lock()
        self.cancelled_requests: set[object] = set()

    def send(self, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with self.send_lock:
            sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()

    def send_response(self, id_value, result) -> None:
        with self.cancel_lock:
            cancelled = id_value in self.cancelled_requests
            if cancelled:
                self.cancelled_requests.discard(id_value)
        if cancelled:
            self.send_error(id_value, -32800, "Request cancelled")
            return
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

    def get_doc(self, uri: str) -> Optional[DocumentSnapshot]:
        doc = self.document_store.get(uri)
        if doc:
            return doc
        path = self.uri_to_path(uri)
        if path:
            return self.document_store.load(uri, path)
        return None

    def run(self) -> None:
        # One worker protects the stateful syntax-check subprocess. Tasks from
        # the same document generation are priority ordered so Hover and
        # Signature Help can pass background symbol work, while document
        # updates from different generations retain protocol ordering.
        tasks: PriorityQueue[tuple[int, int, int, Optional[dict]]] = PriorityQueue()

        def work() -> None:
            while True:
                _generation, _priority, _sequence, message = tasks.get()
                try:
                    if message is None:
                        return
                    self.handle_queued(message)
                finally:
                    tasks.task_done()

        worker = threading.Thread(target=work, name="mylang-analysis", daemon=True)
        worker.start()
        generation = 0
        sequence = 0
        try:
            while self.running:
                try:
                    msg = self.read_message()
                except Exception:
                    break
                if msg is None:
                    break
                if msg.get("method") == "$/cancelRequest":
                    request_id = msg.get("params", {}).get("id")
                    with self.cancel_lock:
                        self.cancelled_requests.add(request_id)
                    continue
                method = msg.get("method")
                if method in {
                    "textDocument/didOpen",
                    "textDocument/didChange",
                    "textDocument/didClose",
                    "workspace/didChangeWatchedFiles",
                }:
                    generation += 1
                tasks.put((generation, self.message_priority(method), sequence, msg))
                sequence += 1
        finally:
            tasks.put((generation + 1, 100, sequence, None))
            worker.join()

    def message_priority(self, method: Optional[str]) -> int:
        """Lower values run first within one document generation."""
        if method in {
            "initialize",
            "initialized",
            "shutdown",
            "exit",
            "textDocument/didOpen",
            "textDocument/didChange",
            "textDocument/didClose",
        }:
            return 0
        if method in {
            "textDocument/hover",
            "textDocument/definition",
            "textDocument/signatureHelp",
            "textDocument/semanticTokens/full",
        }:
            return 1
        if method == "textDocument/documentSymbol":
            return 5
        return 3

    def handle_queued(self, msg: dict) -> None:
        id_value = msg.get("id") if isinstance(msg, dict) else None
        if id_value is not None:
            with self.cancel_lock:
                cancelled = id_value in self.cancelled_requests
                if cancelled:
                    self.cancelled_requests.discard(id_value)
            if cancelled:
                self.send_error(id_value, -32800, "Request cancelled")
                return
        try:
            self.handle(msg)
        except Exception as error:
            if id_value is not None:
                try:
                    self.send_error(id_value, -32603, f"Internal error: {error}")
                except Exception:
                    pass

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        id_value = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            capabilities = {
                "textDocumentSync": 1,
                "positionEncoding": "utf-16",
                "hoverProvider": True,
                "definitionProvider": True,
                "signatureHelpProvider": {
                    "triggerCharacters": ["(", ","],
                    "retriggerCharacters": [","],
                },
                "documentSymbolProvider": True,
            }
            init_options = params.get("initializationOptions") or {}
            self.semantic_tokens_enabled = init_options.get("semanticTokens", True)
            if self.semantic_tokens_enabled:
                capabilities["semanticTokensProvider"] = {
                    "legend": {
                        "tokenTypes": TOKEN_TYPES,
                        "tokenModifiers": TOKEN_MODIFIERS,
                    },
                    "full": True,
                }
            result = {
                "capabilities": capabilities,
                "serverInfo": {
                    "name": "mylang-lsp",
                    "version": "0.1.0",
                },
            }
            self.send_response(id_value, result)
            return
        if method == "initialized":
            return
        if method in ("$/cancelRequest", "$/setTrace", "workspace/didChangeConfiguration"):
            return
        if method == "workspace/didChangeWatchedFiles":
            for change in params.get("changes", []):
                changed_uri = change.get("uri")
                if not changed_uri or changed_uri in self.open_documents:
                    continue
                self.document_store.close(changed_uri)
                self.frontend_analysis.invalidate(changed_uri)
                self.workspace_index.remove(changed_uri)
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
            self.open_documents.add(td["uri"])
            self.document_store.update(td["uri"], td.get("text", ""), td.get("version", 0))
            self.frontend_analysis.invalidate(td["uri"])
            self.workspace_index.remove(td["uri"])
            if self.semantic_tokens_enabled:
                self.pending_diagnostics[td["uri"]] = td.get("version", 0)
            else:
                self.publish_diagnostics(td["uri"], td.get("text", ""))
            return
        if method == "textDocument/didChange":
            td = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                text = changes[-1].get("text", "")
                self.document_store.update(td["uri"], text, td.get("version", 0))
                self.frontend_analysis.invalidate(td["uri"])
                self.workspace_index.remove(td["uri"])
                if self.semantic_tokens_enabled:
                    self.pending_diagnostics[td["uri"]] = td.get("version", 0)
                else:
                    self.publish_diagnostics(td["uri"], text)
            return
        if method == "textDocument/didClose":
            td = params["textDocument"]
            self.open_documents.discard(td["uri"])
            self.document_store.close(td["uri"])
            self.frontend_analysis.invalidate(td["uri"])
            self.workspace_index.remove(td["uri"])
            self.pending_diagnostics.pop(td["uri"], None)
            self.clear_diagnostics(td["uri"])
            return
        if method == "textDocument/semanticTokens/full":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_error(id_value, -32602, f"Document not found: {uri}")
                return
            tokens = self.semantic_tokens_for_uri(uri, doc.text)
            # Flush highlighting before doing any follow-up analysis. The
            # frontend result is cached, so diagnostics can reuse it without a
            # second native parse. Documentation remains lazy until hover or
            # signature help is actually requested.
            self.send_response(id_value, {"data": tokens})
            self.publish_pending_diagnostics(uri, doc)
            return
        if method == "textDocument/documentSymbol":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_error(id_value, -32602, f"Document not found: {uri}")
                return
            self.send_response(id_value, self.document_symbols_for_uri(uri, doc.text))
            return
        if method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_response(id_value, None)
                return
            self.index_document(uri)
            position = params.get("position", {})
            result = self.language_features.definition(doc, position)
            if result is None:
                target_uri = self.language_features.definition_import_target_at(doc, position)
                if target_uri is not None and self.workspace_index.get(target_uri) is None:
                    self.index_import(target_uri)
                    result = self.language_features.definition(doc, position)
            self.send_response(id_value, result)
            return
        if method == "textDocument/hover":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_response(id_value, None)
                return
            position = params.get("position", {})
            if self.workspace_index.get(uri) is None:
                self.send_response(id_value, self.loading_hover())
                self.prepare_hover(doc, position)
                self.notify_hover_ready(doc, position)
                return

            result = self.language_features.hover(doc, position)
            if result is None:
                # Resolve only the import selected by the pointer. Unrelated
                # direct and transitive imports stay cold.
                target_uri = self.language_features.import_target_at(doc, position)
                if target_uri is not None and self.workspace_index.get(target_uri) is None:
                    self.send_response(id_value, self.loading_hover())
                    self.index_import(target_uri)
                    self.notify_hover_ready(doc, position)
                    return
            self.send_response(id_value, result)
            return
        if method == "textDocument/signatureHelp":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_response(id_value, None)
                return
            self.index_document(uri)
            position = params.get("position", {})
            result = self.language_features.signature_help(doc, position)
            if result is None:
                target_uri = self.language_features.import_target_at(doc, position)
                if target_uri is not None:
                    self.index_import(target_uri)
                    result = self.language_features.signature_help(doc, position)
            self.send_response(
                id_value,
                result,
            )
            return

        if id_value is not None:
            self.send_error(id_value, -32601, f"Method not found: {method}")

    def publish_diagnostics(self, uri: str, text: str) -> None:
        self.send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": self.syntax_diagnostics_for_uri(uri, text),
            },
        })

    def publish_pending_diagnostics(self, uri: str, doc: DocumentSnapshot) -> None:
        version = self.pending_diagnostics.get(uri)
        if version != doc.version:
            return
        self.pending_diagnostics.pop(uri, None)
        self.publish_diagnostics(uri, doc.text)

    def index_document(self, uri: str) -> None:
        if uri in self.indexing_documents:
            return
        doc = self.document_store.get(uri)
        if doc is None:
            return
        self.indexing_documents.add(uri)
        try:
            unit = self.language_features.unit(doc)
            importer_path = self.uri_to_path(uri)
            if not importer_path:
                return
            importer_dir = Path(importer_path).parent
            for import_name, relative_path in unit.import_sources.items():
                if not relative_path.endswith(".mln"):
                    continue
                try:
                    target_path = (importer_dir / relative_path).resolve(strict=True)
                except OSError:
                    continue
                target_uri = target_path.as_uri()
                unit.import_targets[import_name] = target_uri
        finally:
            self.indexing_documents.remove(uri)

    def index_import(self, target_uri: str) -> None:
        """Index exactly one source import selected by an interactive request."""
        target_doc = self.document_store.get(target_uri)
        if target_doc is None:
            target_path = self.uri_to_path(target_uri)
            if target_path:
                target_doc = self.document_store.load(target_uri, target_path)
        if target_doc is not None:
            self.index_document(target_uri)

    def prepare_hover(self, doc: DocumentSnapshot, position: dict) -> None:
        """Populate just enough index state for a subsequent hover request."""
        self.index_document(doc.uri)
        target_uri = self.language_features.import_target_at(doc, position)
        if target_uri is not None and self.workspace_index.get(target_uri) is None:
            self.index_import(target_uri)

    def loading_hover(self) -> dict:
        return {
            "contents": {
                "kind": "markdown",
                "value": "Loading...",
            }
        }

    def notify_hover_ready(self, doc: DocumentSnapshot, position: dict) -> None:
        self.send({
            "jsonrpc": "2.0",
            "method": "mylang/hoverReady",
            "params": {
                "uri": doc.uri,
                "version": doc.version,
                "position": position,
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

    def syntax_diagnostics_for_uri(self, uri: str, text: str) -> List[dict]:
        return self.syntax_diagnostics(text)

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
                    self.utf16_column(line, char_no),
                    self.utf16_column(line, char_no + 1),
                    f"Unexpected '{ch}'.",
                ))

        for opener, line_no, char_no in stack:
            line = lines[line_no]
            diagnostics.append(self.make_diagnostic(
                line_no,
                self.utf16_column(line, char_no),
                self.utf16_column(line, char_no + 1),
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
        line_map = LineMap(text)
        for diagnostic in result.get("diagnostics", []):
            line_no = int(diagnostic.get("line", 0))
            start_byte = int(diagnostic.get("character", 0))
            end_byte = int(diagnostic.get("endCharacter", start_byte + 1))
            start = line_map.offset_to_lsp(line_map.native_offset(line_no, start_byte))["character"]
            end = line_map.offset_to_lsp(line_map.native_offset(line_no, end_byte))["character"]
            diagnostics.append(self.make_diagnostic(
                line_no,
                start,
                end,
                str(diagnostic.get("message", "Syntax error.")),
            ))
        return diagnostics

    def query_syntax_checker(self, text: str) -> Optional[dict]:
        data = text.encode("utf-8")
        cache_key = hashlib.sha256(data).digest()
        cached = self.syntax_result_cache.get(cache_key)
        if cached is not None:
            self.syntax_result_cache.move_to_end(cache_key)
            return cached
        if not self.start_syntax_checker():
            return None
        if not self.syntax_check_proc or not self.syntax_check_proc.stdin:
            return None

        try:
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
            result = json.loads(line)
            self.syntax_result_cache[cache_key] = result
            self.syntax_result_cache.move_to_end(cache_key)
            while len(self.syntax_result_cache) > self.syntax_result_cache_limit:
                self.syntax_result_cache.popitem(last=False)
            return result
        except (json.JSONDecodeError, OSError):
            self.stop_syntax_checker()
            return None

    def start_syntax_checker(self) -> bool:
        if self.syntax_check_proc and self.syntax_check_proc.poll() is None:
            return True
        if not self.ensure_syntax_checker_binary() or not self.grammar_path_obj.exists():
            return False

        try:
            self.syntax_check_proc = subprocess.Popen(
                [
                    str(self.syntax_check_bin),
                    "--stdio",
                    str(self.syntax_check_grammar),
                    str(self.syntax_check_cache),
                ],
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

    def utf16_column(self, line: str, codepoint_column: int) -> int:
        return len(line[:codepoint_column].encode("utf-16-le")) // 2

    def semantic_tokens_for_uri(self, uri: str, text: str) -> List[int]:
        return self.semantic_tokens(text)

    def semantic_tokens(self, text: str) -> List[int]:
        lines = text.splitlines()
        line_map = LineMap(text)
        protected = self.protected_spans(lines)

        # Base layer: lexical tokens straight from the C lexer (strings, numbers,
        # operators, keywords, builtin types, identifiers).
        base: Dict[Tuple[int, int], Tuple[int, str]] = {}
        # Grammar-derived classification (function/type/struct/namespace/
        # parameter/property) the LR1 parser attached to identifiers. Authoritative
        # over the base layer; no source-text heuristics involved.
        engine: Dict[Tuple[int, int], Tuple[int, str]] = {}
        result = self.query_syntax_checker(text)
        if result:
            for entry in result.get("tokens", []):
                line_no, col, length, kind = entry[0], entry[1], entry[2], entry[3]
                if 0 <= line_no < len(lines):
                    start_pos = line_map.offset_to_lsp(line_map.native_offset(line_no, col))
                    end_pos = line_map.offset_to_lsp(line_map.native_offset(line_no, col + length))
                    col = start_pos["character"]
                    length = max(0, end_pos["character"] - col)
                token_type = KIND_TO_TYPE.get(kind)
                if token_type and length:
                    base[(line_no, col)] = (length, token_type)
                if length and len(entry) > 4 and entry[4] in TOKEN_TYPE_INDEX:
                    engine[(line_no, col)] = (length, entry[4])

            # The grammar checker deliberately elides recognized generic
            # argument lists before parsing: that keeps '<' relational unless
            # the name is a declared or named-imported template.  Restore the
            # editor-facing meaning of identifier type arguments here.  Builtin
            # arguments (i32, etc.) are already classified by the lexer.
            for line_no, col, length in self.generic_type_argument_spans(lines, result.get("tokens", [])):
                start_pos = line_map.offset_to_lsp(line_map.native_offset(line_no, col))
                end_pos = line_map.offset_to_lsp(line_map.native_offset(line_no, col + length))
                utf16_col = start_pos["character"]
                utf16_length = max(0, end_pos["character"] - utf16_col)
                if utf16_length:
                    engine[(line_no, utf16_col)] = (utf16_length, "type")

        # Comments: the C lexer discards comments, so they are detected here from
        # the protected spans. (Emitting comment trivia from the lexer is a
        # follow-up; this is the only remaining source-text scan.)
        comments = self.comment_semantic_tokens(lines, protected)

        # Priority: base (lexical) < engine (grammar-derived) < comments.
        merged = dict(base)
        merged.update(engine)
        merged.update(comments)

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

    def comment_semantic_tokens(
        self,
        lines: List[str],
        protected: Dict[int, List[Tuple[int, int]]],
    ) -> Dict[Tuple[int, int], Tuple[int, str]]:
        tokens: Dict[Tuple[int, int], Tuple[int, str]] = {}
        in_block_comment = False
        block_is_doc = False

        def add(line_no: int, line: str, start: int, end: int, token_type: str) -> None:
            if end <= start:
                return
            utf16_start = self.utf16_column(line, start)
            utf16_end = self.utf16_column(line, end)
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

    def generic_type_argument_spans(self, lines: List[str], tokens: List[list]) -> List[Tuple[int, int, int]]:
        """Return identifier spans inside known generic argument/parameter lists.

        The checker has no module resolver, so a named import is a candidate
        template name.  This is intentionally the same conservative rule it
        uses when it hides generic spans from the LR grammar; `a < b > c` is
        never treated as a generic expression unless `a` is such a candidate.
        """
        def kind(index: int) -> str:
            return str(tokens[index][3])

        def lexeme(index: int) -> str:
            line_no, col, length = (int(tokens[index][0]), int(tokens[index][1]), int(tokens[index][2]))
            if not (0 <= line_no < len(lines)):
                return ""
            data = lines[line_no].encode("utf-8")
            return data[col:col + length].decode("utf-8", errors="replace")

        def find_close(open_index: int) -> Optional[int]:
            depth = 1
            for index in range(open_index + 1, len(tokens)):
                token_kind = kind(index)
                if token_kind == "LT":
                    depth += 1
                elif token_kind == "GT":
                    depth -= 1
                elif token_kind == "RSH":
                    depth -= 2
                if depth <= 0:
                    return index
            return None

        candidates = set()
        for index, token in enumerate(tokens):
            if kind(index) != "IMPORT" or index + 1 >= len(tokens) or kind(index + 1) != "L_BRACE":
                continue
            cursor = index + 2
            while cursor < len(tokens) and kind(cursor) != "R_BRACE":
                if kind(cursor) == "IDENTIFIER":
                    candidates.add(lexeme(cursor))
                cursor += 1

        # Local struct declarations and every `name<T>(...)` form establish a
        # template candidate without requiring cross-file resolution.
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
            for arg_index in range(index + 2, close):
                if kind(arg_index) == "IDENTIFIER":
                    line_no, col, length = (int(tokens[arg_index][0]), int(tokens[arg_index][1]), int(tokens[arg_index][2]))
                    spans.append((line_no, col, length))
        return spans

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

    def document_symbols_for_uri(self, uri: str, text: str) -> List[dict]:
        return self.document_symbols(text)

    def document_symbols(self, text: str) -> List[dict]:
        # Top-level declarations come from the parser (engine symbol output),
        # not source-text regexes.
        result = self.query_syntax_checker(text)
        if not result:
            return []
        lines = text.splitlines()
        line_map = LineMap(text)
        symbols = []
        for entry in result.get("symbols", []):
            line_no, col, length, kind = entry[0], entry[1], entry[2], entry[3]
            if not (0 <= line_no < len(lines)):
                continue
            start_offset = line_map.native_offset(line_no, col)
            end_offset = line_map.native_offset(line_no, col + length)
            name = line_map.data[start_offset:end_offset].decode("utf-8", errors="replace")
            start = line_map.offset_to_lsp(start_offset)["character"]
            end = line_map.offset_to_lsp(end_offset)["character"]
            symbol_kind = SYMBOL_KIND[ENGINE_SYMBOL_KIND.get(kind, "variable")]
            symbols.append(self.make_symbol(name, symbol_kind, line_no, start, end))
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
