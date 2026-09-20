"""Versioned documents and editor features built from MyLang frontend tokens."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple


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


@dataclass(frozen=True)
class Token:
    index: int
    line: int
    byte_column: int
    byte_length: int
    kind: str
    role: Optional[str]
    span: SourceSpan
    text: str


@dataclass(frozen=True)
class ParamDoc:
    name: str
    text: str


@dataclass(frozen=True)
class FunctionDoc:
    summary: str = ""
    body: str = ""
    param_docs: Dict[str, ParamDoc] = field(default_factory=dict)
    return_doc: Optional[str] = None
    comment_span: Optional[SourceSpan] = None


@dataclass(frozen=True)
class ParameterInfo:
    name: str
    label: str
    span: SourceSpan
    is_rest: bool = False


@dataclass(frozen=True)
class FunctionInfo:
    symbol_id: str
    uri: str
    package: str
    name: str
    kind: str
    receiver_type: Optional[str]
    signature: str
    return_type: str
    parameters: Tuple[ParameterInfo, ...]
    name_span: SourceSpan
    declaration_span: SourceSpan
    open_paren_index: int
    close_paren_index: int
    is_exported: bool
    doc: FunctionDoc


@dataclass(frozen=True)
class CallContext:
    name: str
    name_span: SourceSpan
    open_paren_index: int
    active_parameter: int
    kind: str
    receiver_type: Optional[str]
    package: Optional[str]


@dataclass(frozen=True)
class DeclarationInfo:
    name: str
    kind: str
    uri: str
    span: SourceSpan
    is_exported: bool
    container: Optional[str] = None


@dataclass
class AnalysisUnit:
    snapshot: DocumentSnapshot
    tokens: List[Token]
    functions: List[FunctionInfo]
    declaration_open_indices: set[int]
    imported_names: set[str]
    imported_packages: set[str]
    import_sources: Dict[str, str]
    declarations: List[DeclarationInfo]
    import_targets: Dict[str, str] = field(default_factory=dict)


class WorkspaceIndex:
    """Small index whose population can grow from open files to the workspace."""

    def __init__(self) -> None:
        self._units: Dict[str, AnalysisUnit] = {}

    def update(self, unit: AnalysisUnit) -> None:
        self._units[unit.snapshot.uri] = unit

    def remove(self, uri: str) -> None:
        self._units.pop(uri, None)

    def get(self, uri: str) -> Optional[AnalysisUnit]:
        return self._units.get(uri)

    def resolve(
        self,
        name: str,
        current_uri: str,
        kind: Optional[str] = None,
        receiver_type: Optional[str] = None,
        package: Optional[str] = None,
    ) -> Optional[FunctionInfo]:
        def matches(function: FunctionInfo) -> bool:
            if function.name != name:
                return False
            if kind is not None and function.kind != kind:
                return False
            if receiver_type is not None and function.receiver_type != receiver_type:
                return False
            return True

        current = self._units.get(current_uri)
        if current and package is None:
            local = [function for function in current.functions if matches(function)]
            if len(local) == 1:
                return local[0]
            if len(local) > 1:
                return None
        exported = [
            function
            for uri, unit in self._units.items()
            if uri != current_uri
            for function in unit.functions
            if function.is_exported
            and matches(function)
            and (
                (
                    package is not None
                    and (
                        uri == (current.import_targets.get(package) if current else None)
                        or (
                            current is not None
                            and package not in current.import_targets
                            and function.package == package
                        )
                    )
                )
                or (
                    package is None
                    and current is not None
                    and name in current.imported_names
                    and (
                        name not in current.import_targets
                        or uri == current.import_targets[name]
                    )
                )
            )
        ]
        return exported[0] if len(exported) == 1 else None


OPEN_TO_CLOSE = {
    "L_PARENTHESES": "R_PARENTHESES",
    "L_BRACKET": "R_BRACKET",
    "L_BRACE": "R_BRACE",
}
CLOSE_TO_OPEN = {value: key for key, value in OPEN_TO_CLOSE.items()}


class FrontendBackend:
    """Build editor metadata from the syntax checker's token and symbol output."""

    def __init__(self, query_frontend: Callable[[str], Optional[dict]]) -> None:
        self.query_frontend = query_frontend
        self.cache: Dict[Tuple[str, int], AnalysisUnit] = {}

    def invalidate(self, uri: str) -> None:
        for key in list(self.cache):
            if key[0] == uri:
                del self.cache[key]

    def analyze(self, snapshot: DocumentSnapshot) -> AnalysisUnit:
        key = (snapshot.uri, snapshot.version)
        cached = self.cache.get(key)
        if cached is not None and cached.snapshot.text == snapshot.text:
            return cached
        result = self.query_frontend(snapshot.text) or {}
        tokens = self._tokens(snapshot, result.get("tokens", []))
        functions, declaration_opens = self._functions(
            snapshot, tokens, result.get("symbols", [])
        )
        imported_names, imported_packages, import_sources = self._imports(tokens)
        declarations = self._declarations(snapshot, tokens, result.get("symbols", []))
        unit = AnalysisUnit(
            snapshot,
            tokens,
            functions,
            declaration_opens,
            imported_names,
            imported_packages,
            import_sources,
            declarations,
        )
        self.invalidate(snapshot.uri)
        self.cache[key] = unit
        return unit

    def _declarations(
        self,
        snapshot: DocumentSnapshot,
        tokens: Sequence[Token],
        raw_symbols: Sequence[list],
    ) -> List[DeclarationInfo]:
        declarations: List[DeclarationInfo] = []
        token_by_position = {
            (token.line, token.byte_column, token.byte_length): token
            for token in tokens
        }
        for raw in raw_symbols:
            if len(raw) < 4:
                continue
            key = (int(raw[0]), int(raw[1]), int(raw[2]))
            token = token_by_position.get(key)
            kind = str(raw[3])
            if token is None or kind not in ("function", "method", "struct", "enum", "type"):
                continue
            declarations.append(DeclarationInfo(
                token.text,
                kind,
                snapshot.uri,
                token.span,
                self._has_export_modifier(tokens, token.index),
            ))

            if kind != "enum":
                continue
            open_index = next(
                (index for index in range(token.index + 1, len(tokens))
                 if tokens[index].kind == "L_BRACE"),
                None,
            )
            if open_index is None:
                continue
            depth = 0
            for member in tokens[open_index + 1:]:
                if member.kind == "L_BRACE":
                    depth += 1
                elif member.kind == "R_BRACE":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and member.role == "enumMember":
                    declarations.append(DeclarationInfo(
                        member.text,
                        "enumMember",
                        snapshot.uri,
                        member.span,
                        self._has_export_modifier(tokens, token.index),
                        token.text,
                    ))
        return declarations

    def _has_export_modifier(self, tokens: Sequence[Token], token_index: int) -> bool:
        for index in range(token_index - 1, -1, -1):
            kind = tokens[index].kind
            if kind == "EXPORT":
                return True
            if kind in ("SEMICOLON", "L_BRACE", "R_BRACE"):
                return False
        return False

    def _tokens(self, snapshot: DocumentSnapshot, raw_tokens: Sequence[list]) -> List[Token]:
        tokens: List[Token] = []
        for index, raw in enumerate(raw_tokens):
            if len(raw) < 4:
                continue
            line, column, length = int(raw[0]), int(raw[1]), int(raw[2])
            start = snapshot.line_map.native_offset(line, column)
            end = snapshot.line_map.native_offset(line, column + length)
            span = SourceSpan(start, end)
            tokens.append(Token(
                index=len(tokens),
                line=line,
                byte_column=column,
                byte_length=length,
                kind=str(raw[3]),
                role=str(raw[4]) if len(raw) > 4 else None,
                span=span,
                text=snapshot.line_map.slice(span),
            ))
        return tokens

    def _imports(
        self, tokens: Sequence[Token]
    ) -> Tuple[set[str], set[str], Dict[str, str]]:
        names: set[str] = set()
        packages: set[str] = set()
        sources: Dict[str, str] = {}
        index = 0
        while index < len(tokens):
            if tokens[index].kind != "IMPORT":
                index += 1
                continue
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor].kind == "L_BRACE":
                cursor += 1
                imported: List[str] = []
                while cursor < len(tokens) and tokens[cursor].kind != "R_BRACE":
                    if tokens[cursor].kind == "IDENTIFIER":
                        names.add(tokens[cursor].text)
                        imported.append(tokens[cursor].text)
                    cursor += 1
                path = self._import_path_after(tokens, cursor)
                if path is not None:
                    for name in imported:
                        sources[name] = path
            elif cursor < len(tokens) and tokens[cursor].kind == "IDENTIFIER":
                name = tokens[cursor].text
                packages.add(name)
                path = self._import_path_after(tokens, cursor)
                if path is not None:
                    sources[name] = path
            index = cursor + 1
        return names, packages, sources

    def _import_path_after(self, tokens: Sequence[Token], index: int) -> Optional[str]:
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].kind != "SEMICOLON":
            if tokens[cursor].kind == "STRING_LITERAL":
                return tokens[cursor].text.strip('"')
            cursor += 1
        return None

    def _functions(
        self,
        snapshot: DocumentSnapshot,
        tokens: List[Token],
        raw_symbols: Sequence[list],
    ) -> Tuple[List[FunctionInfo], set[int]]:
        symbol_positions = {
            (int(symbol[0]), int(symbol[1]), str(symbol[3]))
            for symbol in raw_symbols
            if len(symbol) >= 4 and str(symbol[3]) in ("function", "method")
        }
        package = ""
        for index, token in enumerate(tokens[:-1]):
            if token.kind == "PACKAGE" and tokens[index + 1].kind == "IDENTIFIER":
                package = tokens[index + 1].text
                break

        functions: List[FunctionInfo] = []
        declaration_opens: set[int] = set()
        for name_index, token in enumerate(tokens):
            symbol_kind = token.role if token.role in ("function", "method") else ""
            if (token.line, token.byte_column, symbol_kind) not in symbol_positions:
                continue
            open_index = self._call_open_after(tokens, name_index)
            if open_index is None:
                continue
            close_index = self._matching_close(tokens, open_index)
            if close_index is None:
                continue
            declaration_opens.add(open_index)
            boundary = self._declaration_boundary(tokens, name_index)
            receiver_open = None
            receiver_type = None
            if symbol_kind == "method" and name_index > 0 and tokens[name_index - 1].kind == "R_PARENTHESES":
                receiver_open = self._matching_open(tokens, name_index - 1)
                if receiver_open is not None:
                    receiver_type = self._declared_type(tokens[receiver_open + 1:name_index - 1])
            return_end = receiver_open if receiver_open is not None else name_index
            return_tokens = [
                item for item in tokens[boundary:return_end]
                if item.kind not in ("EXPORT", "EXTERN")
            ]
            return_type = self._joined_source(snapshot, return_tokens).strip() or "void"
            parameters = tuple(self._parameters(snapshot, tokens, open_index, close_index))
            generic_text = snapshot.line_map.slice(SourceSpan(token.span.start, tokens[open_index].span.start)).strip()
            signature = f"{generic_text}({', '.join(parameter.label for parameter in parameters)}) -> {return_type}"
            source_start_index = boundary if boundary < len(tokens) else name_index
            declaration_span = SourceSpan(tokens[source_start_index].span.start, tokens[close_index].span.end)
            doc = self._doc_comment(snapshot, tokens[source_start_index].line)
            symbol_id = f"{package}:{symbol_kind}:{token.text}:{snapshot.uri}:{token.span.start}"
            functions.append(FunctionInfo(
                symbol_id=symbol_id,
                uri=snapshot.uri,
                package=package,
                name=token.text,
                kind=symbol_kind,
                receiver_type=receiver_type,
                signature=signature,
                return_type=return_type,
                parameters=parameters,
                name_span=token.span,
                declaration_span=declaration_span,
                open_paren_index=open_index,
                close_paren_index=close_index,
                is_exported=any(item.kind == "EXPORT" for item in tokens[boundary:name_index]),
                doc=doc,
            ))
        return functions, declaration_opens

    def _call_open_after(self, tokens: Sequence[Token], name_index: int) -> Optional[int]:
        index = name_index + 1
        if index < len(tokens) and tokens[index].kind == "LT":
            depth = 0
            while index < len(tokens):
                if tokens[index].kind == "LT":
                    depth += 1
                elif tokens[index].kind == "GT":
                    depth -= 1
                elif tokens[index].kind == "RSH":
                    depth -= 2
                index += 1
                if depth <= 0:
                    break
        if index < len(tokens) and tokens[index].kind == "L_PARENTHESES":
            return index
        return None

    def _matching_close(self, tokens: Sequence[Token], open_index: int) -> Optional[int]:
        expected = OPEN_TO_CLOSE.get(tokens[open_index].kind)
        if expected is None:
            return None
        depth = 1
        for index in range(open_index + 1, len(tokens)):
            if tokens[index].kind == tokens[open_index].kind:
                depth += 1
            elif tokens[index].kind == expected:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _matching_open(self, tokens: Sequence[Token], close_index: int) -> Optional[int]:
        expected = CLOSE_TO_OPEN.get(tokens[close_index].kind)
        if expected is None:
            return None
        depth = 1
        for index in range(close_index - 1, -1, -1):
            if tokens[index].kind == tokens[close_index].kind:
                depth += 1
            elif tokens[index].kind == expected:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _declaration_boundary(self, tokens: Sequence[Token], name_index: int) -> int:
        depth = {"L_PARENTHESES": 0, "L_BRACKET": 0, "L_BRACE": 0}
        for index in range(name_index - 1, -1, -1):
            kind = tokens[index].kind
            if not any(depth.values()) and kind in ("SEMICOLON", "R_BRACE", "L_BRACE"):
                return index + 1
            if kind in CLOSE_TO_OPEN:
                depth[CLOSE_TO_OPEN[kind]] += 1
            elif kind in OPEN_TO_CLOSE and depth[kind] > 0:
                depth[kind] -= 1
        return 0

    def _parameters(
        self,
        snapshot: DocumentSnapshot,
        tokens: Sequence[Token],
        open_index: int,
        close_index: int,
    ) -> Iterable[ParameterInfo]:
        segments: List[Tuple[int, int]] = []
        start = open_index + 1
        stack: List[str] = []
        angle_depth = 0
        for index in range(start, close_index):
            kind = tokens[index].kind
            if kind in OPEN_TO_CLOSE:
                stack.append(kind)
            elif kind in CLOSE_TO_OPEN and stack and stack[-1] == CLOSE_TO_OPEN[kind]:
                stack.pop()
            elif kind == "LT":
                angle_depth += 1
            elif kind == "GT" and angle_depth:
                angle_depth -= 1
            elif kind == "RSH" and angle_depth:
                angle_depth = max(0, angle_depth - 2)
            elif kind == "COMMA" and not stack and angle_depth == 0:
                segments.append((start, index))
                start = index + 1
        if start < close_index:
            segments.append((start, close_index))

        for segment_start, segment_end in segments:
            items = list(tokens[segment_start:segment_end])
            if not items:
                continue
            name_token = next((item for item in items if item.role == "parameter"), None)
            if name_token is None:
                continue
            span = SourceSpan(items[0].span.start, items[-1].span.end)
            yield ParameterInfo(
                name=name_token.text,
                label=snapshot.line_map.slice(span).strip(),
                span=span,
                is_rest=any(item.kind == "REST" for item in items),
            )

    def _declared_type(self, tokens: Sequence[Token]) -> Optional[str]:
        builtin = {
            "BOOL", "U8", "U16", "I32", "U32", "CHAR", "FLOAT", "DOUBLE",
            "VOID", "LONG", "SHORT",
        }
        candidate = next(
            (token for token in reversed(tokens) if token.role == "type" or token.kind in builtin),
            None,
        )
        return candidate.text if candidate is not None else None

    def _joined_source(self, snapshot: DocumentSnapshot, tokens: Sequence[Token]) -> str:
        if not tokens:
            return ""
        return snapshot.line_map.slice(SourceSpan(tokens[0].span.start, tokens[-1].span.end))

    def _doc_comment(self, snapshot: DocumentSnapshot, declaration_line: int) -> FunctionDoc:
        lines = snapshot.text.split("\n")
        index = declaration_line - 1
        collected: List[Tuple[int, str]] = []
        while index >= 0:
            stripped = lines[index].lstrip()
            if not stripped.startswith("///"):
                break
            content = stripped[3:]
            if content.startswith(" "):
                content = content[1:]
            collected.append((index, content))
            index -= 1
        if collected:
            collected.reverse()
            first_line = collected[0][0]
            last_line = collected[-1][0]
            start = snapshot.line_map.native_offset(first_line, 0)
            end = snapshot.line_map.native_offset(
                last_line, len(lines[last_line].encode("utf-8"))
            )
            return self._parse_doc_content(
                [content for _line, content in collected], SourceSpan(start, end)
            )

        block = self._block_doc_comment(snapshot, lines, declaration_line)
        if block is None:
            return FunctionDoc()
        contents, span = block
        return self._parse_doc_content(contents, span)

    def _block_doc_comment(
        self,
        snapshot: DocumentSnapshot,
        lines: Sequence[str],
        declaration_line: int,
    ) -> Optional[Tuple[List[str], SourceSpan]]:
        end_line = declaration_line - 1
        if end_line < 0:
            return None
        end_marker = lines[end_line].rfind("*/")
        if end_marker < 0 or lines[end_line][end_marker + 2:].strip():
            return None

        start_line = end_line
        start_marker = -1
        while start_line >= 0:
            start_marker = lines[start_line].find("/**")
            if start_marker >= 0:
                break
            start_line -= 1
        if start_line < 0 or lines[start_line][:start_marker].strip():
            return None

        contents: List[str] = []
        if start_line == end_line:
            contents.append(lines[start_line][start_marker + 3:end_marker].strip())
        else:
            first = lines[start_line][start_marker + 3:].rstrip()
            if first.strip():
                contents.append(first.lstrip())
            for line_no in range(start_line + 1, end_line):
                content = lines[line_no].lstrip()
                if content.startswith("*"):
                    content = content[1:]
                    if content.startswith(" "):
                        content = content[1:]
                contents.append(content.rstrip())
            last = lines[end_line][:end_marker].lstrip()
            if last.startswith("*"):
                last = last[1:]
                if last.startswith(" "):
                    last = last[1:]
            if last.strip():
                contents.append(last.rstrip())

        while contents and not contents[0].strip():
            contents.pop(0)
        while contents and not contents[-1].strip():
            contents.pop()

        start = snapshot.line_map.native_offset(
            start_line, len(lines[start_line][:start_marker].encode("utf-8"))
        )
        end = snapshot.line_map.native_offset(
            end_line, len(lines[end_line][:end_marker + 2].encode("utf-8"))
        )
        return contents, SourceSpan(start, end)

    def _parse_doc_content(
        self, contents: Sequence[str], comment_span: SourceSpan
    ) -> FunctionDoc:
        prose: List[str] = []
        param_docs: Dict[str, ParamDoc] = {}
        return_doc: Optional[str] = None
        in_tags = False
        for content in contents:
            stripped = content.strip()
            if stripped.startswith("@param "):
                in_tags = True
                pieces = stripped.split(None, 2)
                if len(pieces) == 3 and pieces[1] not in param_docs:
                    param_docs[pieces[1]] = ParamDoc(pieces[1], pieces[2])
            elif stripped.startswith("@return ") or stripped.startswith("@returns "):
                in_tags = True
                if return_doc is None:
                    return_doc = stripped.split(None, 1)[1] if " " in stripped else ""
            elif stripped.startswith("@"):
                in_tags = True
            elif not in_tags:
                prose.append(content.rstrip())
        nonempty = [idx for idx, line in enumerate(prose) if line.strip()]
        summary = prose[nonempty[0]].strip() if nonempty else ""
        body_lines = prose[nonempty[0] + 1:] if nonempty else []
        body = "\n".join(body_lines).strip()
        return FunctionDoc(summary, body, param_docs, return_doc, comment_span)


class LanguageFeatures:
    def __init__(self, frontend: FrontendBackend, index: WorkspaceIndex) -> None:
        self.frontend = frontend
        self.index = index

    def unit(self, snapshot: DocumentSnapshot) -> AnalysisUnit:
        unit = self.frontend.analyze(snapshot)
        self.index.update(unit)
        return unit

    def hover(self, snapshot: DocumentSnapshot, position: dict) -> Optional[dict]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        function = next(
            (item for item in unit.functions if item.name_span.start <= offset < item.name_span.end),
            None,
        )
        token = self._token_at(unit.tokens, offset)
        if function is None and token is not None and token.kind == "IDENTIFIER":
            if self._is_callee(unit, token.index):
                kind = "method" if token.role == "property" else "function"
                receiver_type = self._receiver_type_for_call(unit, token.index) if kind == "method" else None
                package = self._package_for_call(unit, token.index) if kind == "function" else None
                function = self.index.resolve(
                    token.text, snapshot.uri, kind, receiver_type, package
                )
        if function is not None:
            span = token.span if token is not None else function.name_span
            return {
                "contents": {"kind": "markdown", "value": self._function_markdown(function)},
                "range": snapshot.line_map.range(span),
            }

        context = self._call_context(unit, offset)
        if context is None:
            return None
        function = self.index.resolve(
            context.name,
            snapshot.uri,
            context.kind,
            context.receiver_type,
            context.package,
        )
        if function is None or context.active_parameter >= len(function.parameters):
            return None
        parameter = function.parameters[context.active_parameter]
        documentation = function.doc.param_docs.get(parameter.name)
        value = f"```mylang\n{parameter.label}\n```"
        if documentation and documentation.text:
            value += f"\n\n{documentation.text}"
        hover_span = token.span if token is not None else context.name_span
        return {
            "contents": {"kind": "markdown", "value": value},
            "range": snapshot.line_map.range(hover_span),
        }

    def signature_help(self, snapshot: DocumentSnapshot, position: dict) -> Optional[dict]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        context = self._call_context(unit, offset)
        if context is None:
            return None
        function = self.index.resolve(
            context.name,
            snapshot.uri,
            context.kind,
            context.receiver_type,
            context.package,
        )
        if function is None:
            return None
        active = context.active_parameter
        rest_index = next(
            (index for index, parameter in enumerate(function.parameters) if parameter.is_rest),
            None,
        )
        if rest_index is not None and active >= rest_index:
            active = rest_index
        parameters = []
        search_from = 0
        for parameter in function.parameters:
            start = function.signature.find(parameter.label, search_from)
            end = start + len(parameter.label)
            search_from = max(end, search_from)
            doc = function.doc.param_docs.get(parameter.name)
            info = {
                "label": [
                    self._utf16_length(function.signature[:max(start, 0)]),
                    self._utf16_length(function.signature[:max(end, 0)]),
                ] if start >= 0 else parameter.label,
            }
            if doc and doc.text:
                info["documentation"] = {"kind": "markdown", "value": doc.text}
            parameters.append(info)
        signature = {
            "label": function.signature,
            "documentation": {
                "kind": "markdown",
                "value": self._summary_markdown(function.doc),
            },
            "parameters": parameters,
        }
        return {
            "signatures": [signature],
            "activeSignature": 0,
            "activeParameter": active,
        }

    def definition(self, snapshot: DocumentSnapshot, position: dict) -> Optional[dict]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        token = self._token_at(unit.tokens, offset)
        if token is None:
            return None

        exact = next(
            (item for item in unit.declarations if item.span == token.span),
            None,
        )
        if exact is not None:
            return self._declaration_location(exact)

        if self._is_callee(unit, token.index):
            kind = "method" if token.role == "property" else "function"
            receiver_type = self._receiver_type_for_call(unit, token.index) if kind == "method" else None
            package = self._package_for_call(unit, token.index) if kind == "function" else None
            function = self.index.resolve(
                token.text, snapshot.uri, kind, receiver_type, package
            )
            if function is not None:
                return {
                    "uri": function.uri,
                    "range": self.index.get(function.uri).snapshot.line_map.range(function.name_span),
                }

        container = None
        if token.role == "enumMember" and token.index >= 2:
            if unit.tokens[token.index - 1].kind == "COLONCOLON":
                container = unit.tokens[token.index - 2].text
        candidates = self._matching_declarations(unit, token.text, token.role, container)
        if len(candidates) == 1:
            return self._declaration_location(candidates[0])

        target_uri = self.definition_import_target_at(snapshot, position)
        target = self.index.get(target_uri) if target_uri is not None else None
        if target is None:
            return None
        candidates = [
            declaration
            for declaration in self._matching_declarations(
                target, token.text, token.role, container
            )
            if declaration.is_exported
        ]
        return self._declaration_location(candidates[0]) if len(candidates) == 1 else None

    def definition_import_target_at(
        self, snapshot: DocumentSnapshot, position: dict
    ) -> Optional[str]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        token = self._token_at(unit.tokens, offset)
        if token is None:
            return None
        if token.text in unit.import_targets:
            return unit.import_targets[token.text]
        if token.index >= 2 and unit.tokens[token.index - 1].kind in ("DOT", "COLONCOLON"):
            qualifier = unit.tokens[token.index - 2].text
            if qualifier in unit.import_targets:
                return unit.import_targets[qualifier]
        if self._is_callee(unit, token.index):
            package = self._package_for_call(unit, token.index)
            if package in unit.import_targets:
                return unit.import_targets[package]
            if token.role == "property":
                receiver_type = self._receiver_type_for_call(unit, token.index)
                if receiver_type in unit.import_targets:
                    return unit.import_targets[receiver_type]
        return None

    def _matching_declarations(
        self,
        unit: AnalysisUnit,
        name: str,
        role: Optional[str],
        container: Optional[str],
    ) -> List[DeclarationInfo]:
        compatible = {
            "function": {"function", "method"},
            "property": {"method"},
            "type": {"struct", "enum", "type"},
            "struct": {"struct"},
            "enum": {"enum"},
            "enumMember": {"enumMember"},
        }.get(role, set())
        return [
            declaration
            for declaration in unit.declarations
            if declaration.name == name
            and declaration.kind in compatible
            and (container is None or declaration.container == container)
        ]

    def _declaration_location(self, declaration: DeclarationInfo) -> Optional[dict]:
        unit = self.index.get(declaration.uri)
        if unit is None:
            return None
        return {
            "uri": declaration.uri,
            "range": unit.snapshot.line_map.range(declaration.span),
        }

    def import_target_at(
        self, snapshot: DocumentSnapshot, position: dict
    ) -> Optional[str]:
        """Return the one source import relevant to an interactive position."""
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        token = self._token_at(unit.tokens, offset)
        name: Optional[str] = None
        package: Optional[str] = None
        receiver_type: Optional[str] = None

        if token is not None and token.kind == "IDENTIFIER" and self._is_callee(unit, token.index):
            name = token.text
            if token.role == "property":
                receiver_type = self._receiver_type_for_call(unit, token.index)
            else:
                package = self._package_for_call(unit, token.index)
        else:
            context = self._call_context(unit, offset)
            if context is not None:
                name = context.name
                package = context.package
                receiver_type = context.receiver_type

        for key in (package, name, receiver_type):
            if key is not None and key in unit.import_targets:
                return unit.import_targets[key]
        return None

    def _token_at(self, tokens: Sequence[Token], offset: int) -> Optional[Token]:
        for token in tokens:
            if token.span.start <= offset < token.span.end:
                return token
        return None

    def _is_callee(self, unit: AnalysisUnit, token_index: int) -> bool:
        open_index = self.frontend._call_open_after(unit.tokens, token_index)
        return open_index is not None and open_index not in unit.declaration_open_indices

    def _call_context(self, unit: AnalysisUnit, offset: int) -> Optional[CallContext]:
        tokens = unit.tokens
        stack: List[Tuple[str, int, Optional[int]]] = []
        for index, token in enumerate(tokens):
            if token.span.start >= offset:
                break
            if token.kind in OPEN_TO_CLOSE:
                callee = None
                if token.kind == "L_PARENTHESES" and index not in unit.declaration_open_indices:
                    callee = self._callee_before_open(tokens, index)
                stack.append((token.kind, index, callee))
            elif token.kind in CLOSE_TO_OPEN:
                expected = CLOSE_TO_OPEN[token.kind]
                if stack and stack[-1][0] == expected:
                    stack.pop()
        call = next((entry for entry in reversed(stack) if entry[2] is not None), None)
        if call is None:
            return None
        _kind, open_index, name_index = call
        assert name_index is not None
        active = self._active_parameter(tokens, open_index, offset)
        name = tokens[name_index]
        kind = "method" if name.role == "property" else "function"
        receiver_type = self._receiver_type_for_call(unit, name_index) if kind == "method" else None
        package = self._package_for_call(unit, name_index) if kind == "function" else None
        return CallContext(
            name.text, name.span, open_index, active, kind, receiver_type, package
        )

    def _package_for_call(self, unit: AnalysisUnit, name_index: int) -> Optional[str]:
        tokens = unit.tokens
        if name_index < 2 or tokens[name_index - 1].kind != "DOT":
            return None
        qualifier = tokens[name_index - 2]
        if qualifier.kind == "IDENTIFIER" and qualifier.text in unit.imported_packages:
            return qualifier.text
        return None

    def _receiver_type_for_call(self, unit: AnalysisUnit, name_index: int) -> Optional[str]:
        tokens = unit.tokens
        if name_index < 2 or tokens[name_index - 1].kind not in ("DOT", "MEMBER"):
            return None
        receiver = tokens[name_index - 2]
        if receiver.kind != "IDENTIFIER":
            return None
        for index in range(name_index - 3, 0, -1):
            token = tokens[index]
            if token.text != receiver.text or token.role not in ("variable", "parameter"):
                continue
            declared = self.frontend._declared_type(tokens[max(0, index - 8):index])
            if declared is not None:
                return declared
        return None

    def _callee_before_open(self, tokens: Sequence[Token], open_index: int) -> Optional[int]:
        if open_index <= 0:
            return None
        previous = open_index - 1
        if tokens[previous].kind == "IDENTIFIER":
            return previous
        if tokens[previous].kind in ("GT", "RSH"):
            depth = 1 if tokens[previous].kind == "GT" else 2
            for index in range(previous - 1, -1, -1):
                if tokens[index].kind == "GT":
                    depth += 1
                elif tokens[index].kind == "RSH":
                    depth += 2
                elif tokens[index].kind == "LT":
                    depth -= 1
                    if depth <= 0 and index > 0 and tokens[index - 1].kind == "IDENTIFIER":
                        return index - 1
        return None

    def _active_parameter(self, tokens: Sequence[Token], open_index: int, offset: int) -> int:
        stack: List[str] = []
        active = 0
        generic_ranges = self._generic_ranges(tokens)
        generic_end_by_start = {start: end for start, end in generic_ranges}
        skip_until = -1
        for index in range(open_index + 1, len(tokens)):
            token = tokens[index]
            if token.span.start >= offset:
                break
            if index <= skip_until:
                continue
            if index in generic_end_by_start:
                skip_until = generic_end_by_start[index]
                continue
            if token.kind in OPEN_TO_CLOSE:
                stack.append(token.kind)
            elif token.kind in CLOSE_TO_OPEN:
                expected = CLOSE_TO_OPEN[token.kind]
                if stack and stack[-1] == expected:
                    stack.pop()
                elif token.kind == "R_PARENTHESES" and not stack:
                    break
            elif token.kind == "COMMA" and not stack:
                active += 1
        return active

    def _generic_ranges(self, tokens: Sequence[Token]) -> List[Tuple[int, int]]:
        ranges: List[Tuple[int, int]] = []
        for index in range(len(tokens) - 2):
            if tokens[index].kind != "IDENTIFIER" or tokens[index + 1].kind != "LT":
                continue
            depth = 1
            for end in range(index + 2, len(tokens)):
                if tokens[end].kind == "LT":
                    depth += 1
                elif tokens[end].kind == "GT":
                    depth -= 1
                elif tokens[end].kind == "RSH":
                    depth -= 2
                if depth <= 0:
                    if end + 1 < len(tokens) and tokens[end + 1].kind == "L_PARENTHESES":
                        ranges.append((index + 1, end))
                    break
        return ranges

    def _function_markdown(self, function: FunctionInfo) -> str:
        pieces = [f"```mylang\n{function.signature}\n```"]
        summary = self._summary_markdown(function.doc)
        if summary:
            pieces.append(summary)
        documented = [
            (parameter, function.doc.param_docs.get(parameter.name))
            for parameter in function.parameters
        ]
        documented = [(parameter, doc) for parameter, doc in documented if doc and doc.text]
        if documented:
            lines = ["**Parameters**"]
            lines.extend(f"- `{parameter.name}` — {doc.text}" for parameter, doc in documented)
            pieces.append("\n".join(lines))
        if function.doc.return_doc and function.return_type != "void":
            pieces.append(f"**Returns**\n\n{function.doc.return_doc}")
        return "\n\n".join(pieces)

    def _summary_markdown(self, doc: FunctionDoc) -> str:
        if doc.summary and doc.body:
            return f"{doc.summary}\n\n{doc.body}"
        return doc.summary or doc.body

    def _utf16_length(self, text: str) -> int:
        return len(text.encode("utf-16-le")) // 2
