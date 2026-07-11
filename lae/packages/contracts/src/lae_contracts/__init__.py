"""Canonical LAE contracts and their dependency-free validator."""

from .validator import (
    ContractRepositoryError,
    EXPECTED_SCHEMAS,
    ValidationIssue,
    is_safe_external_image_reference,
    load_schema,
    specs_root,
    validate_instance,
    validate_repository,
)

__all__ = [
    "ContractRepositoryError",
    "EXPECTED_SCHEMAS",
    "ValidationIssue",
    "is_safe_external_image_reference",
    "load_schema",
    "specs_root",
    "validate_instance",
    "validate_repository",
]
