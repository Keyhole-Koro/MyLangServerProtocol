from typing import List, Optional, Sequence, Tuple

from .models import (
    AnalysisUnit,
    CLOSE_TO_OPEN,
    CallContext,
    DeclarationInfo,
    FunctionInfo,
    OPEN_TO_CLOSE,
    Token,
    WorkspaceIndex,
)
from .documents import DocumentSnapshot
from .frontend import FrontendBackend
from .resolution import ExpressionResolver, base_type, generic_base_index


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
                functions = ExpressionResolver(self.frontend, self.index, unit).functions(token.index)
                function = functions[0] if len(functions) == 1 else None
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

    def definition(
        self, snapshot: DocumentSnapshot, position: dict
    ) -> Optional[dict | List[dict]]:
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

        generic_parameter = self._generic_parameter_declaration(unit, token)
        if generic_parameter is not None:
            return {
                "uri": snapshot.uri,
                "range": snapshot.line_map.range(generic_parameter.span),
            }

        resolver = ExpressionResolver(self.frontend, self.index, unit)
        if token.role in ("variable", "parameter", "function") and resolver.binding(token.text, token.index):
            return None
        if self._is_callee(unit, token.index) and token.role not in ("enumMember", "resultVariant"):
            functions = resolver.functions(token.index)
            if functions:
                return self._definition_locations([
                    DeclarationInfo(
                        function.name,
                        function.kind,
                        function.uri,
                        function.name_span,
                        function.is_exported,
                    )
                    for function in functions
                ])
            # A failed typed call must not fall back to unrelated declarations
            # with the same spelling (including methods of other receivers).
            return None

        functions = self._function_value_targets(unit, token)
        if functions:
            return self._definition_locations([
                DeclarationInfo(
                    function.name,
                    function.kind,
                    function.uri,
                    function.name_span,
                    function.is_exported,
                )
                    for function in functions
                ])

        if token.role == "property":
            return None
        container = None
        if token.role in ("enumMember", "resultVariant") and token.index >= 2:
            if unit.tokens[token.index - 1].kind == "COLONCOLON":
                qualifier = token.index - 2
                if unit.tokens[qualifier].kind in ("GT", "RSH"):
                    qualifier = generic_base_index(unit.tokens, qualifier)
                if qualifier >= 0:
                    container = unit.tokens[qualifier].text
        role = token.role
        if token.text in unit.imported_names:
            before = unit.tokens[:token.index]
            boundary = max((t.index for t in before if t.kind == "SEMICOLON"), default=-1)
            if any(t.kind == "IMPORT" for t in before[boundary + 1:]):
                role = "importedSymbol"
        qualified_import = role == "importedSymbol" or (
            token.index >= 2
            and unit.tokens[token.index - 1].kind in ("DOT", "COLONCOLON")
            and unit.tokens[token.index - 2].text in unit.import_targets
        )
        if not qualified_import:
            candidates = self._matching_declarations(
                unit, token.text, role, container
            )
            if candidates:
                return self._definition_locations(candidates)

        target_uri = self.definition_import_target_at(snapshot, position)
        target = self.index.get(target_uri) if target_uri is not None else None
        if target is None:
            return None
        candidates = [
            declaration
            for declaration in self._matching_declarations(
                target, token.text, role, container
            )
            if declaration.is_exported
        ]
        return self._definition_locations(candidates)

    def references(
        self,
        snapshot: DocumentSnapshot,
        position: dict,
        include_declaration: bool = True,
    ) -> List[dict]:
        """Find references to the symbol at *position* in indexed units."""
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        selected = self._token_at(unit.tokens, offset)
        if selected is None:
            return []

        targets = self._location_keys(self.definition(snapshot, position))
        if not targets:
            return []

        locations: dict[tuple, dict] = {}
        for candidate_unit in self.index.units():
            for token in candidate_unit.tokens:
                if token.kind != "IDENTIFIER" or token.text != selected.text:
                    continue
                token_location = {
                    "uri": candidate_unit.snapshot.uri,
                    "range": candidate_unit.snapshot.line_map.range(token.span),
                }
                token_key = self._location_key(token_location)
                if not include_declaration and token_key in targets:
                    continue
                definitions = self.definition(
                    candidate_unit.snapshot,
                    candidate_unit.snapshot.line_map.offset_to_lsp(token.span.start),
                )
                if targets.intersection(self._location_keys(definitions)):
                    locations[token_key] = token_location

        return [locations[key] for key in sorted(locations)]

    def identifier_at(
        self, snapshot: DocumentSnapshot, position: dict
    ) -> Optional[str]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        token = self._token_at(unit.tokens, offset)
        if token is None or token.kind != "IDENTIFIER":
            return None
        return token.text

    def definition_import_target_at(
        self, snapshot: DocumentSnapshot, position: dict
    ) -> Optional[str]:
        unit = self.unit(snapshot)
        offset = snapshot.line_map.lsp_to_offset(position)
        token = self._token_at(unit.tokens, offset)
        if token is None:
            return None
        resolver = ExpressionResolver(self.frontend, self.index, unit)
        if self._is_callee(unit, token.index) or token.role == "property":
            resolver.functions(token.index)
            if resolver.pending_import is not None:
                return resolver.pending_import
        if token.role in ("enumMember", "resultVariant") and token.index >= 2:
            qualifier = token.index - 2
            if unit.tokens[token.index - 1].kind == "COLONCOLON":
                if unit.tokens[qualifier].kind in ("GT", "RSH"):
                    qualifier = generic_base_index(unit.tokens, qualifier)
                if qualifier >= 0 and unit.tokens[qualifier].text in unit.import_targets:
                    return unit.import_targets[unit.tokens[qualifier].text]
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
                receiver_name = base_type(receiver_type or "")
                if receiver_name in unit.import_targets:
                    return unit.import_targets[receiver_name]
        value_context = self._function_value_context(unit, token)
        if value_context is not None:
            _kind, receiver_type, package = value_context
            for key in (package, token.text, receiver_type):
                if key is not None and key in unit.import_targets:
                    return unit.import_targets[key]
        return None

    def _function_value_targets(
        self, unit: AnalysisUnit, token: Token
    ) -> List[FunctionInfo]:
        context = self._function_value_context(unit, token)
        if context is None:
            return []
        kind, receiver_type, package = context
        return self.index.resolve_all(
            token.text,
            unit.snapshot.uri,
            kind,
            receiver_type,
            package,
        )

    def _function_value_context(
        self, unit: AnalysisUnit, token: Token
    ) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
        """Describe an identifier used as a function pointer rather than called."""
        if token.kind != "IDENTIFIER" or self._is_callee(unit, token.index):
            return None
        if token.role == "variable":
            if ExpressionResolver(self.frontend, self.index, unit).binding(token.text, token.index):
                return None
            return "function", None, None
        if token.role != "property":
            return None
        package = self._package_for_call(unit, token.index)
        if package is not None:
            return "function", None, package
        receiver_type = self._receiver_type_for_call(unit, token.index)
        if receiver_type is not None:
            return "method", receiver_type, None
        return None

    def _location_keys(self, result: Optional[dict | List[dict]]) -> set[tuple]:
        if result is None:
            return set()
        locations = result if isinstance(result, list) else [result]
        return {self._location_key(location) for location in locations}

    def _location_key(self, location: dict) -> tuple:
        start = location["range"]["start"]
        end = location["range"]["end"]
        return (
            location["uri"],
            start["line"],
            start["character"],
            end["line"],
            end["character"],
        )

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
            "resultVariant": {"enumMember"},
            "importedSymbol": {"function", "struct", "enum", "type"},
        }.get(role, set())
        return [
            declaration
            for declaration in unit.declarations
            if declaration.name == name
            and declaration.kind in compatible
            and (container is None or declaration.container == container)
        ]

    def _generic_parameter_declaration(
        self, unit: AnalysisUnit, selected: Token
    ) -> Optional[Token]:
        """Resolve a type/const parameter inside its declaration's scope.

        Generic parameters are syntax symbols rather than workspace-level type
        declarations. Keeping this lookup lexical prevents two unrelated
        `T`/`N` parameters from becoming ambiguous definition targets.
        """
        if selected.kind != "IDENTIFIER" or selected.role != "type":
            return None
        tokens = unit.tokens
        scopes: List[Tuple[int, int, List[Token]]] = []

        def generic_close(open_index: int) -> Optional[int]:
            depth = 1
            for index in range(open_index + 1, len(tokens)):
                if tokens[index].kind == "LT":
                    depth += 1
                elif tokens[index].kind == "GT":
                    depth -= 1
                elif tokens[index].kind == "RSH":
                    depth -= 2
                if depth <= 0:
                    return index
            return None

        def scope_end(after: int) -> int:
            open_brace = next(
                (index for index in range(after + 1, len(tokens))
                 if tokens[index].kind in ("L_BRACE", "SEMICOLON")),
                len(tokens) - 1,
            )
            if tokens[open_brace].kind == "SEMICOLON":
                return open_brace
            depth = 1
            for index in range(open_brace + 1, len(tokens)):
                if tokens[index].kind == "L_BRACE":
                    depth += 1
                elif tokens[index].kind == "R_BRACE":
                    depth -= 1
                    if depth == 0:
                        return index
            return len(tokens) - 1

        for index in range(len(tokens) - 2):
            declaration = (
                tokens[index].kind in ("STRUCT", "ENUM")
                and tokens[index + 1].kind == "IDENTIFIER"
                and tokens[index + 2].kind == "LT"
            )
            function = (
                tokens[index].kind == "IDENTIFIER"
                and tokens[index].role in ("function", "method")
                and tokens[index + 1].kind == "LT"
            )
            if not declaration and not function:
                continue
            open_index = index + (2 if declaration else 1)
            close_index = generic_close(open_index)
            if close_index is None:
                continue
            parameters = [
                token for token in tokens[open_index + 1:close_index]
                if token.kind == "IDENTIFIER" and token.role == "type"
            ]
            if parameters:
                scopes.append((index, scope_end(close_index), parameters))

        candidates = [
            (end - start, parameter)
            for start, end, parameters in scopes
            if start <= selected.index <= end
            for parameter in parameters
            if parameter.text == selected.text
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def _declaration_location(self, declaration: DeclarationInfo) -> Optional[dict]:
        unit = self.index.get(declaration.uri)
        if unit is None:
            return None
        return {
            "uri": declaration.uri,
            "range": unit.snapshot.line_map.range(declaration.span),
        }

    def _definition_locations(
        self, declarations: Sequence[DeclarationInfo]
    ) -> Optional[dict | List[dict]]:
        locations = [
            location
            for declaration in declarations
            if (location := self._declaration_location(declaration)) is not None
        ]
        if not locations:
            return None
        return locations[0] if len(locations) == 1 else locations

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
            target = self.definition_import_target_at(snapshot, position)
            if target is not None:
                return target
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

                resolver = ExpressionResolver(self.frontend, self.index, unit)
                callee = next(t for t in unit.tokens if t.span == context.name_span)
                resolver.functions(callee.index)
                if resolver.pending_import is not None:
                    return resolver.pending_import

        for key in (package, name, base_type(receiver_type or "")):
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
        resolver = ExpressionResolver(self.frontend, self.index, unit)
        if name_index > 0 and tokens[name_index - 1].kind not in ("DOT", "MEMBER") and resolver.binding(name.text, name_index):
            return None
        kind = "method" if name.role == "property" else "function"
        receiver_type = self._receiver_type_for_call(unit, name_index) if kind == "method" else None
        package = self._package_for_call(unit, name_index) if kind == "function" else None
        return CallContext(
            name.text, name.span, open_index, active, kind, receiver_type, package
        )

    def _package_for_call(self, unit: AnalysisUnit, name_index: int) -> Optional[str]:
        return ExpressionResolver(self.frontend, self.index, unit).package(name_index)

    def _receiver_type_for_call(self, unit: AnalysisUnit, name_index: int) -> Optional[str]:
        return ExpressionResolver(self.frontend, self.index, unit).receiver_type(name_index)

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
