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
    # Set for an item of `impl Trait for Type { ... }`: the trait as written
    # (`From<SsdError>`) and the impl's type. `Self` in the receiver and return
    # type has already been replaced by impl_type.
    impl_trait: Optional[str] = None
    impl_type: Optional[str] = None
    # Set for a prototype inside `trait Name { ... }`; such an item is a
    # contract, never a call target.
    in_trait: Optional[str] = None


@dataclass(frozen=True)
class ImplInfo:
    """`impl Trait<Args> for Type`, located by its trait name token."""

    trait_name: str
    trait_text: str
    self_type: str
    uri: str
    span: SourceSpan
    is_exported: bool
    is_short: bool


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


@dataclass(frozen=True)
class ModelSource:
    """How the analysis of a `.model.mln` relates to the file as written.

    The analysed text is the file followed by the code generated from it, so a
    span before `generated_start` is already a position in the file. A span
    from there on belongs to a generated line, which came from one source word.
    """

    document: DocumentSnapshot
    generated_start: int
    generated_line: int
    origins: Tuple[SourceSpan, ...]
    # A word of the model to what it names: itself for a declaration, None for
    # a keyword.
    words: Dict[SourceSpan, Optional[SourceSpan]]
    token_count: int

    def origin(self, line: int) -> SourceSpan:
        index = min(max(line - self.generated_line, 0), len(self.origins) - 1)
        return self.origins[index]


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
    impls: List[ImplInfo] = field(default_factory=list)
    model: Optional[ModelSource] = None

    @property
    def document(self) -> DocumentSnapshot:
        """The document as written, which positions from an editor address."""
        return self.model.document if self.model else self.snapshot

    @property
    def source_tokens(self) -> List[Token]:
        """The tokens somebody wrote, without any that were generated."""
        return self.tokens[:self.model.token_count] if self.model else self.tokens

    def written_span(self, span: SourceSpan) -> SourceSpan:
        """Where `span` shows in the document as written."""
        if self.model is None or span.start < self.model.generated_start:
            return span
        line = self.snapshot.line_map.offset_to_lsp(span.start)["line"]
        return self.model.origin(line)

    def written_range(self, span: SourceSpan) -> dict:
        return self.snapshot.line_map.range(self.written_span(span))


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

    def units(self) -> List[AnalysisUnit]:
        """Return a snapshot of the currently indexed analysis units."""
        return list(self._units.values())

    def resolve(
        self,
        name: str,
        current_uri: str,
        kind: Optional[str] = None,
        receiver_type: Optional[str] = None,
        package: Optional[str] = None,
    ) -> Optional[FunctionInfo]:
        candidates = self.resolve_all(
            name, current_uri, kind, receiver_type, package
        )
        return candidates[0] if len(candidates) == 1 else None

    def resolve_all(
        self,
        name: str,
        current_uri: str,
        kind: Optional[str] = None,
        receiver_type: Optional[str] = None,
        package: Optional[str] = None,
    ) -> List[FunctionInfo]:
        # Import locally to keep the metadata module independent at import time.
        from .resolution import BUILTIN_TYPE_NAMES, base_type

        if kind == "method" and receiver_type is None:
            return []
        receiver_name = base_type(receiver_type) if receiver_type else None

        def matches(function: FunctionInfo) -> bool:
            if function.name != name or function.in_trait is not None:
                return False
            if kind is not None and function.kind != kind:
                return False
            if receiver_name is not None and base_type(function.receiver_type or "") != receiver_name:
                return False
            return True

        current = self._units.get(current_uri)
        if current and package is None:
            local = [function for function in current.functions if matches(function)]
            if local:
                return local
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
                    kind == "method"
                    and current is not None
                    and receiver_name in current.imported_names
                    and uri == current.import_targets.get(receiver_name)
                )
                or (
                    kind == "method"
                    and current is not None
                    and receiver_name in BUILTIN_TYPE_NAMES
                    and uri in {
                        current.import_targets.get(package_name)
                        for package_name in current.imported_packages
                    }
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
        return exported


OPEN_TO_CLOSE = {
    "L_PARENTHESES": "R_PARENTHESES",
    "L_BRACKET": "R_BRACKET",
    "L_BRACE": "R_BRACE",
}
CLOSE_TO_OPEN = {value: key for key, value in OPEN_TO_CLOSE.items()}
