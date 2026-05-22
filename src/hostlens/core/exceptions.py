"""Hostlens core exception hierarchy.

M0 scope per openspec/changes/bootstrap-project-skeleton/specs/core-services/spec.md
- exactly four classes: HostlensError, ConfigError, TargetError, InspectorError.
Future milestones (M2+) may extend this module with additional subclasses.
"""

__all__ = [
    "ConfigError",
    "HostlensError",
    "InspectorError",
    "TargetError",
]


class HostlensError(Exception):
    """Base exception for all Hostlens-defined errors."""


class ConfigError(HostlensError):
    """Raised when configuration loading or validation fails.

    `original` optionally chains the underlying exception (e.g. a
    `pydantic.ValidationError` captured by `load_settings()`), so callers
    can introspect raw error details while the formatted message stays
    redacted for sensitive fields.
    """

    def __init__(self, message: str, *, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original


class TargetError(HostlensError):
    """Raised on ExecutionTarget errors (used from M1+)."""


class InspectorError(HostlensError):
    """Raised on Inspector loading or execution errors (used from M1+)."""
