from typing import List, Optional, Sequence, Tuple

from analysis_model import (
    AnalysisUnit,
    CLOSE_TO_OPEN,
    CallContext,
    DeclarationInfo,
    FunctionInfo,
    OPEN_TO_CLOSE,
    Token,
    WorkspaceIndex,
)
from document_model import DocumentSnapshot
from frontend_analysis import FrontendBackend


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

