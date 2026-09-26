"""Scoped bindings and receiver-expression types shared by editor features.

Only imports required by the expression are requested. The protocol loads a
missing unit and retries; syntax analysis never performs filesystem IO here.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .models import AnalysisUnit, Token, WorkspaceIndex


BUILTINS = {"BOOL", "U8", "U16", "I32", "U32", "CHAR", "FLOAT", "DOUBLE",
            "VOID", "LONG", "SHORT"}
MODIFIERS = {"CONST", "REF", "MUT", "ASTARISK"}


def base_type(text: str) -> str:
    names = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)
    return next((name for name in names if name not in {"const", "ref", "mut"}), "")


def type_before(tokens, end):
    """Read a declaration's type, ending immediately before its name."""
    stop = end
    while end >= 0 and tokens[end].kind in MODIFIERS:
        end -= 1
    if end < 0:
        return None
    if tokens[end].kind in ("GT", "RSH"):
        end = generic_base_index(tokens, end)
    if end < 0 or not (tokens[end].role == "type" or tokens[end].kind in BUILTINS):
        return None
    start = end
    while start > 0 and tokens[start - 1].kind in MODIFIERS:
        start -= 1
    return " ".join(token.text for token in tokens[start:stop + 1])


def generic_base_index(tokens, end):
    depth = 0
    for index in range(end, -1, -1):
        kind = tokens[index].kind
        if kind == "GT":
            depth += 1
        elif kind == "RSH":
            depth += 2
        elif kind == "LT":
            depth -= 1
            if depth == 0:
                return index - 1
    return -1


@dataclass(frozen=True)
class Binding:
    token: Token
    type: str
    start: int
    end: int


class ExpressionResolver:
    def __init__(self, frontend, index: WorkspaceIndex, unit: AnalysisUnit):
        self.frontend = frontend
        self.index = index
        self.unit = unit
        self.pending_import: Optional[str] = None
        self.bindings = self._bindings()

    def _bindings(self):
        tokens = self.unit.tokens
        scopes = []
        stack = []
        for token in tokens:
            if token.kind == "L_BRACE":
                stack.append(token.index)
            elif token.kind == "R_BRACE" and stack:
                scopes.append((stack.pop(), token.index))
        bindings = []
        for token in tokens:
            if token.role not in ("variable", "parameter"):
                continue
            declared = type_before(tokens, token.index - 1)
            if declared is None:
                continue
            scope = min((scope for scope in scopes if scope[0] < token.index < scope[1]),
                        key=lambda scope: scope[1] - scope[0], default=(0, len(tokens)))
            # Parameter lists precede their body's braces, including receivers.
            if token.role == "parameter":
                function = next((f for f in self.unit.functions
                                 if f.declaration_span.start <= token.span.start
                                 < tokens[f.close_paren_index].span.end), None)
                if function is None:
                    continue
                body = function.close_paren_index + 1
                if body >= len(tokens) or tokens[body].kind != "L_BRACE":
                    continue
                scope = next((s for s in scopes if s[0] == body), (body, len(tokens)))
            bindings.append(Binding(token, declared, scope[0], scope[1]))
        return bindings

    def binding(self, name, at):
        return next((b for b in reversed(self.bindings)
                     if b.token.text == name and b.token.index <= at
                     and b.start <= at < b.end), None)

    def package(self, name_index):
        tokens = self.unit.tokens
        if name_index < 2 or tokens[name_index - 1].kind != "DOT":
            return None
        qualifier = tokens[name_index - 2]
        if (qualifier.text in self.unit.imported_packages
                and self.binding(qualifier.text, name_index) is None):
            return qualifier.text
        return None

    def receiver_type(self, name_index, depth=0):
        tokens = self.unit.tokens
        if name_index < 2 or tokens[name_index - 1].kind not in ("DOT", "MEMBER"):
            return None
        return self.expression_type(name_index - 2, name_index, depth + 1)

    def functions(self, name_index, depth=0):
        tokens = self.unit.tokens
        token = tokens[name_index]
        package = self.package(name_index)
        method = name_index > 0 and tokens[name_index - 1].kind in ("DOT", "MEMBER") and package is None
        if not method and package is None and self.binding(token.text, name_index):
            return []
        receiver = self.receiver_type(name_index, depth) if method else None
        if method and receiver is None:
            return []
        target = self.unit.import_targets.get(package or (base_type(receiver) if receiver else token.text))
        if target and self.index.get(target) is None:
            self.pending_import = target
        return self.index.resolve_all(token.text, self.unit.snapshot.uri,
                                      "method" if method else "function", receiver, package)

    def expression_type(self, end, at, depth=0):
        if end < 0 or depth > 64:
            return None
        tokens = self.unit.tokens
        token = tokens[end]
        if token.kind == "R_PARENTHESES":
            opening = self.frontend._matching_open(tokens, end)
            if opening is None:
                return None
            callee = opening - 1
            if callee >= 0 and tokens[callee].kind in ("GT", "RSH"):
                callee = generic_base_index(tokens, callee)
            if callee >= 0 and tokens[callee].kind == "IDENTIFIER":
                functions = self.functions(callee, depth + 1)
                if len(functions) != 1:
                    return None
                function = functions[0]
                result = function.return_type
                if function.kind == "method":
                    receiver = self.receiver_type(callee, depth + 1)
                    result = self._substitute(result, function.receiver_type or "", receiver or "")
                return result
            return self.expression_type(end - 1, at, depth + 1)
        if token.kind == "R_BRACKET":
            opening = self.frontend._matching_open(tokens, end)
            return self.expression_type(opening - 1, at, depth + 1) if opening is not None else None
        if token.kind != "IDENTIFIER":
            return None
        if end >= 2 and tokens[end - 1].kind in ("DOT", "MEMBER"):
            owner_type = self.expression_type(end - 2, at, depth + 1)
            if owner_type is None:
                return None
            owner = base_type(owner_type)
            target = self.unit.import_targets.get(owner)
            unit = self.index.get(target) if target else self.unit
            if unit is None:
                self.pending_import = target
                return None
            declaration = next((d for d in unit.declarations if d.name == owner
                                and d.kind == "struct"), None)
            if declaration is None:
                return None
            start = next(t.index for t in unit.tokens if t.span == declaration.span)
            opening = next((t.index for t in unit.tokens[start:] if t.kind == "L_BRACE"), None)
            if opening is None:
                return None
            close = self.frontend._matching_close(unit.tokens, opening)
            for field in unit.tokens[opening + 1:close]:
                if field.text == token.text:
                    declared = type_before(unit.tokens, field.index - 1)
                    if declared:
                        template = " ".join(t.text for t in unit.tokens[start:opening])
                        return self._substitute(declared, template, owner_type)
            return None
        binding = self.binding(token.text, at)
        return binding.type if binding else None

    @staticmethod
    def _substitute(result, template, concrete):
        def arguments(text):
            if "<" not in text:
                return []
            body = text[text.index("<") + 1:text.rfind(">")]
            parts, start, depth = [], 0, 0
            for index, char in enumerate(body):
                if char == "<": depth += 1
                elif char == ">": depth -= 1
                elif char == "," and depth == 0:
                    parts.append(body[start:index].strip())
                    start = index + 1
            return parts + [body[start:].strip()]
        mapping = dict(zip(arguments(template), arguments(concrete)))
        return re.sub(r"\b[A-Za-z_][A-Za-z_0-9]*\b", lambda m: mapping.get(m[0], m[0]), result)
