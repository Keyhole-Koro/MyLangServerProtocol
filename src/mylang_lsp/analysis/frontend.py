import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    AnalysisUnit,
    CLOSE_TO_OPEN,
    DeclarationInfo,
    FunctionDoc,
    FunctionInfo,
    ImplInfo,
    OPEN_TO_CLOSE,
    ParamDoc,
    ParameterInfo,
    Token,
)
from .documents import DocumentSnapshot, SourceSpan
from .resolution import type_before


@dataclass(frozen=True)
class TraitRegion:
    kind: str                 # "TRAIT" or "IMPL"
    name: str                 # the trait's name
    open_index: int           # body braces; -1 for `impl ... = value;`
    close_index: int
    impl: Optional[ImplInfo]


def _replace_self(text: str, self_type: str) -> str:
    return re.sub(r"\bSelf\b", self_type, text)


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
        regions = self._trait_regions(snapshot, tokens)
        functions, declaration_opens = self._functions(
            snapshot, tokens, result.get("symbols", []), regions
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
            impls=[region.impl for region in regions if region.impl is not None],
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
            if token is None or kind not in ("function", "method", "struct", "enum", "type", "interface"):
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
                elif depth == 0 and member.role in ("enumMember", "resultVariant"):
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

    def _trait_regions(
        self, snapshot: DocumentSnapshot, tokens: Sequence[Token]
    ) -> List["TraitRegion"]:
        """Find top-level `trait` and `impl` declarations and their bodies."""
        regions: List[TraitRegion] = []
        depth = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.kind == "L_BRACE":
                depth += 1
            elif token.kind == "R_BRACE":
                depth = max(0, depth - 1)
            if depth != 0 or token.kind not in ("TRAIT", "IMPL"):
                index += 1
                continue
            name_index = index + 1
            if token.kind == "IMPL" and name_index < len(tokens) and tokens[name_index].kind == "LT":
                # `impl<T: Eq> Eq for Box<T>`: the trait follows the parameters.
                depth = 0
                while name_index < len(tokens):
                    kind = tokens[name_index].kind
                    depth += 1 if kind == "LT" else -1 if kind == "GT" else -2 if kind == "RSH" else 0
                    name_index += 1
                    if depth <= 0:
                        break
            if name_index >= len(tokens) or tokens[name_index].kind != "IDENTIFIER":
                index += 1
                continue
            cursor = name_index + 1
            while cursor < len(tokens) and tokens[cursor].kind not in (
                "L_BRACE", "SEMICOLON", "ASSIGN", "FOR"
            ):
                cursor += 1
            header_end = cursor
            self_type = None
            if token.kind == "IMPL" and cursor < len(tokens) and tokens[cursor].kind == "FOR":
                type_start = cursor + 1
                cursor = type_start
                while cursor < len(tokens) and tokens[cursor].kind not in (
                    "L_BRACE", "SEMICOLON", "ASSIGN"
                ):
                    cursor += 1
                self_type = self._joined_source(snapshot, tokens[type_start:cursor]).strip()
            open_index = cursor if cursor < len(tokens) and tokens[cursor].kind == "L_BRACE" else None
            close_index = self._matching_close(tokens, open_index) if open_index is not None else None
            impl = None
            if token.kind == "IMPL" and self_type:
                impl = ImplInfo(
                    trait_name=tokens[name_index].text,
                    trait_text=self._joined_source(snapshot, tokens[name_index:header_end]).strip(),
                    self_type=self_type,
                    uri=snapshot.uri,
                    span=tokens[name_index].span,
                    is_exported=index > 0 and tokens[index - 1].kind == "EXPORT",
                    is_short=open_index is None,
                )
            regions.append(TraitRegion(
                kind=token.kind,
                name=tokens[name_index].text,
                open_index=open_index if open_index is not None else -1,
                close_index=close_index if close_index is not None else -1,
                impl=impl,
            ))
            index = (close_index if close_index is not None else cursor) + 1
            # The body's braces were skipped, so depth stays at the top level.
        return regions

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
        regions: Sequence["TraitRegion"] = (),
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
            region = next(
                (item for item in regions
                 if item.open_index < name_index < item.close_index),
                None,
            )
            impl_trait = impl_type = in_trait = None
            is_exported = any(item.kind == "EXPORT" for item in tokens[boundary:name_index])
            if region is not None and region.impl is not None:
                impl_trait = region.impl.trait_text
                impl_type = region.impl.self_type
                is_exported = is_exported or region.impl.is_exported
                # `Self` inside an impl is the impl's type.
                if receiver_type is not None:
                    receiver_type = _replace_self(receiver_type, impl_type)
                return_type = _replace_self(return_type, impl_type)
            elif region is not None and region.kind == "TRAIT":
                in_trait = region.name
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
                is_exported=is_exported,
                doc=doc,
                impl_trait=impl_trait,
                impl_type=impl_type,
                in_trait=in_trait,
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
        end = len(tokens) - 1
        if end >= 0 and tokens[end].role in ("variable", "parameter"):
            end -= 1
        return type_before(tokens, end)

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

