"""Structural / inheritance / Literal-domain tests for backend exceptions.

Complements ``test_backend_exceptions_repr.py`` (which focuses on
``__str__`` defensive scrubbing): this file covers the typed-field contract
(``retry_after_seconds``), the Literal value-domain enforcement on
``BackendCapabilityViolation`` / ``BackendDaemonUnsafe``, the inheritance
chain (all subclasses must reach ``HostlensError``), and a baseline
secret-redaction sanity check for ``BackendError.__str__``.
"""

from __future__ import annotations

import pytest

from hostlens.core.exceptions import (
    BackendCapabilityViolation,
    BackendDaemonUnsafe,
    BackendError,
    BackendRateLimited,
    BackendUnavailable,
    HostlensError,
)


def test_backend_rate_limited_retains_retry_after() -> None:
    """``retry_after_seconds`` is exposed as an instance attribute."""

    err = BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=30.5)
    assert err.retry_after_seconds == 30.5


def test_backend_rate_limited_retry_after_can_be_none() -> None:
    """529 / soft-limit paths construct with ``retry_after_seconds=None``."""

    err = BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=None)
    assert err.retry_after_seconds is None


def test_backend_capability_violation_rejects_free_text_attempted_feature() -> None:
    """The Literal domain blocks free-text injection.

    A free-text value (e.g. ``"x; rm -rf /"``) would otherwise reach
    ``__str__`` and become a log/prompt-injection vector.
    """

    with pytest.raises(ValueError):
        BackendCapabilityViolation(
            backend_name="fake",
            capability="prompt_caching",
            attempted_feature="x; rm -rf /",  # type: ignore[arg-type]
        )


def test_backend_capability_violation_rejects_free_text_capability() -> None:
    """The Literal domain blocks invalid ``capability`` values."""

    with pytest.raises(ValueError):
        BackendCapabilityViolation(
            backend_name="fake",
            capability="not_a_real_capability",  # type: ignore[arg-type]
            attempted_feature="cache_control_in_system_block",
        )


def test_backend_daemon_unsafe_rejects_free_text_reason() -> None:
    """``BackendDaemonUnsafe.reason`` is similarly Literal-constrained."""

    with pytest.raises(ValueError):
        BackendDaemonUnsafe(
            backend_name="claude_subscription",
            reason="custom; rm -rf /",  # type: ignore[arg-type]
        )


def test_backend_error_str_does_not_leak_sk_ant_secret() -> None:
    """Baseline scrub test (full coverage lives in ``..._repr.py``)."""

    fake_leak = (
        "sk-" + "ant-" + "leakvaluexxxxxxxxxxxxxxxxxxxxxxxx"
    )  # pragma: allowlist secret — fake fixture, not a real key
    cause = Exception(f"api_key={fake_leak} tail")
    err = BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=cause)
    assert fake_leak not in str(err)


def test_all_backend_exceptions_inherit_from_hostlens_error() -> None:
    """Every backend exception subclass walks up to ``HostlensError``.

    Lets callers ``except HostlensError`` to catch all Hostlens-originated
    errors regardless of backend specialization.
    """

    rate = BackendRateLimited(backend_name="x", retry_after_seconds=1.0)
    unavail = BackendUnavailable(backend_name="x")
    cap = BackendCapabilityViolation(
        backend_name="x",
        capability="prompt_caching",
        attempted_feature="cache_control_in_system_block",
    )
    daemon = BackendDaemonUnsafe(backend_name="x", reason="subscription_in_daemon")
    base = BackendError(backend_name="x")

    for exc in (rate, unavail, cap, daemon, base):
        assert isinstance(exc, BackendError)
        assert isinstance(exc, HostlensError)


def test_backend_capability_violation_accepts_all_literal_attempted_features() -> None:
    """Every allowed ``attempted_feature`` Literal value constructs cleanly.

    Catches regressions where a new value gets added to the Literal but the
    runtime allowlist (``_BACKEND_ATTEMPTED_FEATURES``) is not synced.
    """

    for feature in (
        "cache_control_in_system_block",
        "cache_control_in_messages_block",
        "cache_control_in_tools_array",
        "tools_array_non_empty",
    ):
        err = BackendCapabilityViolation(
            backend_name="x",
            capability="prompt_caching",
            attempted_feature=feature,  # type: ignore[arg-type]
        )
        assert err.attempted_feature == feature


def test_backend_daemon_unsafe_accepts_all_literal_reasons() -> None:
    """Every allowed ``reason`` Literal value constructs cleanly."""

    for reason in ("subscription_in_daemon", "concurrent_request_limit_exceeded"):
        err = BackendDaemonUnsafe(backend_name="x", reason=reason)  # type: ignore[arg-type]
        assert err.reason == reason
