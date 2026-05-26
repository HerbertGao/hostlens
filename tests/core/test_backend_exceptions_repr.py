"""Defensive ``__str__`` contract for ``BackendError``.

The Backend exception family's ``__str__`` is the **only** sanctioned surface
through which an upstream SDK exception flows into Hostlens logs / CLI
output. ``cause`` may be an ``anthropic.AuthenticationError`` with
``response.headers["x-api-key"]`` populated, an ``OSError`` with empty
``args``, an exception constructed with bytes instead of str — the
``__str__`` implementation MUST tolerate all of these without raising and
MUST NOT dump ``cause.__dict__`` / ``response.headers`` / ``body``.

The tests below cover one positive case (the main scrub path) plus six
defensive fallback cases on the ``_extract_cause_text`` fallback ladder.
"""

from __future__ import annotations

from types import SimpleNamespace

from hostlens.core.exceptions import BackendError

_FAKE_MSG_LEAK = (
    "sk-" + "ant-" + "leakvaluexxxxxxxxxxxxxxxxxxxxxxxxxxx"
)  # pragma: allowlist secret — fake fixture, not a real key
_FAKE_HEADER_LEAK = (
    "sk-" + "ant-" + "secret9999999999999"
)  # pragma: allowlist secret — fake fixture, not a real key
_FAKE_BODY_LEAK = (
    "sk-" + "ant-" + "bodyleak"
)  # pragma: allowlist secret — fake fixture, not a real key


def test_str_redacts_sk_ant_jwt_bearer_and_never_dumps_response_headers() -> None:
    """Main scrub path.

    The ``cause`` carries (a) an ``sk-ant-...`` secret in the message,
    (b) a ``Bearer <token>`` substring, and (c) a synthetic ``response``
    attribute mirroring an httpx response with ``x-api-key`` and
    ``Authorization`` headers. ``__str__`` must NOT include any of those
    secrets, and must NOT mention ``response`` / ``headers`` / ``body``
    structural names.
    """

    cause = Exception(f"hello {_FAKE_MSG_LEAK} Bearer xyz123abc")
    # Simulate ``response.headers`` attribute carrying sensitive header
    # values — backend layer code reading ``cause.response`` would expose
    # them; the ``__str__`` whitelist must keep them out.
    cause.response = SimpleNamespace(  # type: ignore[attr-defined]
        headers={"x-api-key": _FAKE_HEADER_LEAK, "Authorization": "Bearer xyz123abc"},
        text='{"error": "bad"}',
    )
    cause.body = {"error": f"leak {_FAKE_BODY_LEAK}"}  # type: ignore[attr-defined]

    err = BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=cause)
    text = str(err)

    # Secret substrings from the cause's message must be masked.
    assert _FAKE_MSG_LEAK not in text
    assert "Bearer xyz123abc" not in text
    # Secret substrings from cause.response / body must NEVER appear (the
    # ``__str__`` does not read those attributes at all).
    assert _FAKE_HEADER_LEAK not in text
    assert _FAKE_BODY_LEAK not in text
    # Structural attribute names must not be in the rendered output —
    # confirms ``__str__`` is a fixed whitelist, not a ``__dict__`` dump.
    assert "response" not in text
    assert "headers" not in text
    assert "body" not in text


def test_str_does_not_raise_on_empty_args() -> None:
    """``cause = Exception()`` (args = empty tuple) falls back to type name."""

    cause = Exception()
    err = BackendError(backend_name="x", cause=cause)
    text = str(err)  # MUST NOT raise
    assert "cause=Exception" in text


def test_str_does_not_raise_on_non_string_args0_bytes() -> None:
    """``cause = Exception(b"binary")`` falls back to type name of args[0]."""

    cause = Exception(b"binary-payload-not-str")
    err = BackendError(backend_name="x", cause=cause)
    text = str(err)  # MUST NOT raise
    assert "cause=bytes" in text


def test_str_does_not_raise_on_non_string_args0_dict() -> None:
    """``cause = Exception({"k": "v"})`` falls back to ``dict`` type name."""

    cause = Exception({"k": "v"})
    err = BackendError(backend_name="x", cause=cause)
    text = str(err)  # MUST NOT raise
    assert "cause=dict" in text


def test_str_does_not_raise_on_none_cause() -> None:
    """``cause=None`` renders an empty cause segment."""

    err = BackendError(backend_name="x", cause=None)
    text = str(err)  # MUST NOT raise
    assert "cause=," in text


def test_message_attribute_preferred_over_args_zero() -> None:
    """Fallback step 2 (``cause.message``) wins over step 3 (``args[0]``).

    Some SDK exceptions (incl. ``anthropic.*``) set ``.message`` separately
    from ``args``. The contract is: if ``.message`` is a string, use it.
    """

    cause = Exception("ignored value in args[0]")
    cause.message = "preferred message from .message attribute"  # type: ignore[attr-defined]
    err = BackendError(backend_name="x", cause=cause)
    text = str(err)
    assert "preferred message from .message attribute" in text
    assert "ignored value in args[0]" not in text


def test_status_and_request_id_safe_extraction_with_no_attributes() -> None:
    """``cause`` without ``status_code`` / ``request_id`` renders ``None``.

    The ``getattr(..., None)`` fallback ladder must never raise on missing
    attributes. ``cause`` is a plain ``Exception`` instance — it has neither
    ``status_code`` nor ``request_id`` — so the rendered output is
    ``status=None`` / ``request_id=None``.
    """

    cause = Exception("simple message")
    err = BackendError(backend_name="x", cause=cause)
    text = str(err)
    assert "status=None" in text
    assert "request_id=None" in text
