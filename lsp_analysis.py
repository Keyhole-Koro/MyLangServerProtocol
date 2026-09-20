"""Compatibility facade for the MyLang analysis API.

The implementation is split by responsibility. Importing from this module remains
supported for extensions, tests, and older integrations.
"""

from analysis_model import (
    AnalysisUnit,
    CLOSE_TO_OPEN,
    CallContext,
    DeclarationInfo,
    FunctionDoc,
    FunctionInfo,
    OPEN_TO_CLOSE,
    ParamDoc,
    ParameterInfo,
    Token,
    WorkspaceIndex,
)
from document_model import DocumentSnapshot, DocumentStore, LineMap, SourceSpan
from frontend_analysis import FrontendBackend
from language_features import LanguageFeatures

__all__ = [
    "AnalysisUnit",
    "CLOSE_TO_OPEN",
    "CallContext",
    "DeclarationInfo",
    "DocumentSnapshot",
    "DocumentStore",
    "FrontendBackend",
    "FunctionDoc",
    "FunctionInfo",
    "LanguageFeatures",
    "LineMap",
    "OPEN_TO_CLOSE",
    "ParamDoc",
    "ParameterInfo",
    "SourceSpan",
    "Token",
    "WorkspaceIndex",
]
