"""Protocol-shape contract for ``BackendDiagnostics``.

Three things ``hostlens doctor`` relies on:

1. A class implementing all three methods passes
   ``isinstance(backend, BackendDiagnostics)`` (duck-type detection —
   design.md D-3).
2. A class missing one method does NOT pass — otherwise ``doctor`` would
   call a non-existent method at runtime.
3. ``BackendHealth`` accepts any ``error`` string at construction time;
   redaction is the backend's job (see ``hostlens.core.redact``), tested
   separately in the AnthropicAPIBackend tests (group 3 / §8).
"""

from __future__ import annotations

from hostlens.agent.backend import (
    BackendDiagnostics,
    BackendHealth,
    QuotaStatus,
)

_FAKE_LEAK_MSG = (
    "api_key=" + "sk-" + "ant-" + "leak failed"
)  # pragma: allowlist secret — fake fixture, not a real key


class _FullDiagnostics:
    """Implements every ``BackendDiagnostics`` method — should pass."""

    async def health_check(self) -> BackendHealth:
        return BackendHealth(is_healthy=True, backend_name="full")

    async def quota_check(self) -> QuotaStatus | None:
        return None

    def ensure_safe_for_daemon(self) -> None:
        return None


class _PartialDiagnostics:
    """Missing ``ensure_safe_for_daemon`` — must NOT pass isinstance."""

    async def health_check(self) -> BackendHealth:
        return BackendHealth(is_healthy=True, backend_name="partial")

    async def quota_check(self) -> QuotaStatus | None:
        return None


def test_full_implementation_passes_isinstance_check() -> None:
    assert isinstance(_FullDiagnostics(), BackendDiagnostics)


def test_partial_implementation_fails_isinstance_check() -> None:
    """Missing one method MUST be detected — silent acceptance would let
    ``doctor`` call ``ensure_safe_for_daemon`` on a backend that does not
    have it, raising ``AttributeError`` from deep in the daemon-safe path."""

    assert not isinstance(_PartialDiagnostics(), BackendDiagnostics)


def test_backend_health_accepts_arbitrary_error_string() -> None:
    """``BackendHealth`` is a passive carrier — the redaction contract lives
    inside ``BackendDiagnostics.health_check`` implementations, not on the
    model itself. The constructor MUST therefore accept any string
    (including one that *looks* sensitive) without raising; the redaction
    of that string is asserted in the AnthropicAPIBackend test suite."""

    health = BackendHealth(
        is_healthy=False,
        backend_name="x",
        error=_FAKE_LEAK_MSG,
    )
    assert health.is_healthy is False
    assert health.backend_name == "x"
    assert health.error == _FAKE_LEAK_MSG
