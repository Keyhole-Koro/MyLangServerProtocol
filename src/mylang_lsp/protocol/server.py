import json
import os
import sys
import threading
from pathlib import Path
from queue import PriorityQueue
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from ..analysis.documents import DocumentSnapshot, DocumentStore, LineMap
from ..analysis.frontend import FrontendBackend
from ..analysis.language_features import LanguageFeatures
from ..analysis.models import WorkspaceIndex
from ..features.diagnostics import DIAGNOSTIC_SEVERITY_ERROR, DiagnosticsService
from ..features.document_symbols import (
    ENGINE_SYMBOL_KIND,
    SYMBOL_KIND,
    DocumentSymbolService,
)
from ..features.semantic_tokens import (
    KIND_TO_TYPE,
    TOKEN_MODIFIERS,
    TOKEN_TYPE_INDEX,
    TOKEN_TYPES,
    SemanticTokenService,
)
from ..frontend.native import NativeFrontendClient
from ..source_text import is_protected, protected_spans, utf16_column

class LspServer:
    def __init__(self) -> None:
        self.document_store = DocumentStore()
        # Compatibility alias for existing tests and small integrations.
        self.docs = self.document_store.documents
        self.running = True
        self.repo_root = Path(__file__).resolve().parents[5]
        self.native_frontend = NativeFrontendClient(self.repo_root)
        # Compatibility alias retained for existing integrations and tests.
        self.syntax_result_cache = self.native_frontend.result_cache
        self.semantic_token_service = SemanticTokenService(self.query_syntax_checker)
        self.diagnostics_service = DiagnosticsService(self.query_syntax_checker)
        self.document_symbol_service = DocumentSymbolService(self.query_syntax_checker)
        self.workspace_index = WorkspaceIndex()
        self.frontend_analysis = FrontendBackend(self.query_syntax_checker)
        self.language_features = LanguageFeatures(self.frontend_analysis, self.workspace_index)
        self.indexing_documents: set[str] = set()
        self.open_documents: set[str] = set()
        self.workspace_roots: List[Path] = []
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
            "textDocument/references",
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
                "referencesProvider": True,
                "signatureHelpProvider": {
                    "triggerCharacters": ["(", ","],
                    "retriggerCharacters": [","],
                },
                "documentSymbolProvider": True,
            }
            init_options = params.get("initializationOptions") or {}
            workspace_folders = params.get("workspaceFolders") or []
            root_uris = [folder.get("uri") for folder in workspace_folders]
            if not root_uris:
                root_uris = [params.get("rootUri")]
            self.workspace_roots = [
                Path(path)
                for uri in root_uris
                if uri and (path := self.uri_to_path(uri)) is not None
            ]
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
            visited = set()
            while result is None:
                target_uri = self.language_features.definition_import_target_at(doc, position)
                if target_uri is None or target_uri in visited or self.workspace_index.get(target_uri) is not None:
                    break
                visited.add(target_uri)
                self.index_import(target_uri)
                result = self.language_features.definition(doc, position)
            self.send_response(id_value, result)
            return
        if method == "textDocument/references":
            uri = params["textDocument"]["uri"]
            doc = self.get_doc(uri)
            if doc is None:
                self.send_response(id_value, [])
                return
            self.index_document(uri)
            position = params.get("position", {})
            target_uri = self.language_features.definition_import_target_at(doc, position)
            if target_uri is not None and self.workspace_index.get(target_uri) is None:
                self.index_import(target_uri)
            name = self.language_features.identifier_at(doc, position)
            if name is not None:
                self.index_workspace_candidates(name)
            for open_uri in list(self.open_documents):
                self.index_document(open_uri)
            include_declaration = params.get("context", {}).get(
                "includeDeclaration", True
            )
            self.send_response(
                id_value,
                self.language_features.references(
                    doc, position, bool(include_declaration)
                ),
            )
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

    def index_workspace_candidates(self, name: str) -> None:
        """Index workspace sources that may mention a requested symbol."""
        needle = name.encode("utf-8")
        ignored = {".git", ".hg", ".svn", "node_modules", "__pycache__"}
        for root in self.workspace_roots:
            if not root.is_dir():
                continue
            for directory, directory_names, file_names in os.walk(root):
                directory_names[:] = [
                    item for item in directory_names if item not in ignored
                ]
                for file_name in file_names:
                    if not file_name.endswith((".mln", ".mlx")):
                        continue
                    path = Path(directory) / file_name
                    uri = path.as_uri()
                    if self.workspace_index.get(uri) is not None:
                        continue
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    if needle not in data:
                        continue
                    doc = self.document_store.get(uri)
                    if doc is None:
                        try:
                            doc = self.document_store.load(uri, str(path))
                        except OSError:
                            continue
                    if doc is not None:
                        self.index_document(uri)

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
        return self.diagnostics_service.analyze(text)

    def syntax_diagnostics(self, text: str) -> List[dict]:
        return self.diagnostics_service.analyze(text)

    def bracket_diagnostics(self, text: str) -> List[dict]:
        return self.diagnostics_service.bracket_diagnostics(text)

    def lr1_diagnostics(self, text: str) -> List[dict]:
        return self.diagnostics_service.frontend_diagnostics(text)

    def query_syntax_checker(self, text: str) -> Optional[dict]:
        return self.native_frontend.query(text)

    def start_syntax_checker(self) -> bool:
        return self.native_frontend.start()

    def stop_syntax_checker(self) -> None:
        self.native_frontend.stop()

    def make_diagnostic(self, line_no: int, start: int, end: int, message: str) -> dict:
        return self.diagnostics_service.make(line_no, start, end, message)

    def utf16_column(self, line: str, codepoint_column: int) -> int:
        return utf16_column(line, codepoint_column)

    def semantic_tokens_for_uri(self, uri: str, text: str) -> List[int]:
        return self.semantic_token_service.encode(text)

    def semantic_tokens(self, text: str) -> List[int]:
        return self.semantic_token_service.encode(text)

    def comment_semantic_tokens(
        self,
        lines: List[str],
        protected: Dict[int, List[Tuple[int, int]]],
    ) -> Dict[Tuple[int, int], Tuple[int, str]]:
        return self.semantic_token_service.comment_tokens(lines, protected)

    def generic_type_argument_spans(
        self, lines: List[str], tokens: List[list]
    ) -> List[Tuple[int, int, int]]:
        return self.semantic_token_service.generic_type_argument_spans(lines, tokens)

    def protected_spans(self, lines: List[str]) -> Dict[int, List[Tuple[int, int]]]:
        return protected_spans(lines)

    def is_protected(
        self,
        line_no: int,
        start: int,
        end: int,
        protected: Dict[int, List[Tuple[int, int]]],
    ) -> bool:
        return is_protected(line_no, start, end, protected)
    def document_symbols_for_uri(self, uri: str, text: str) -> List[dict]:
        return self.document_symbol_service.symbols(text)

    def document_symbols(self, text: str) -> List[dict]:
        return self.document_symbol_service.symbols(text)

    def make_symbol(self, name: str, kind: int, line_no: int, start: int, end: int) -> dict:
        return self.document_symbol_service.make(name, kind, line_no, start, end)


if __name__ == "__main__":
    LspServer().run()
