"""Deterministic source analysis used by the isolated LAE agent runner."""

from .analyzer import AnalysisError, analyze_source
from .ai import (
    AIDiagnosticError,
    AIControllerClientConfig,
    KNOWLEDGE_VERSION,
    OpenAICompatibleConfig,
    apply_ai_proposal,
    authorized,
    build_ai_request,
    call_openai_compatible,
    manifest_candidate_from_plan,
    load_knowledge_pack,
    request_ai_analysis,
    unsupported_findings,
)
from .canonical import canonical_bytes, digest_json

__all__ = [
    "AIDiagnosticError",
    "AIControllerClientConfig",
    "KNOWLEDGE_VERSION",
    "AnalysisError",
    "OpenAICompatibleConfig",
    "analyze_source",
    "apply_ai_proposal",
    "authorized",
    "build_ai_request",
    "call_openai_compatible",
    "canonical_bytes",
    "digest_json",
    "manifest_candidate_from_plan",
    "load_knowledge_pack",
    "request_ai_analysis",
    "unsupported_findings",
]
