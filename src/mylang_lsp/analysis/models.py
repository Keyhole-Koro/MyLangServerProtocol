"""Editor-facing analysis records and workspace declaration index."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .documents import DocumentSnapshot, SourceSpan

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

