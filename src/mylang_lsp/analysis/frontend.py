from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    AnalysisUnit,
    CLOSE_TO_OPEN,
    DeclarationInfo,
    FunctionDoc,
    FunctionInfo,
    OPEN_TO_CLOSE,
    ParamDoc,
    ParameterInfo,
    Token,
)
from .documents import DocumentSnapshot, SourceSpan


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


