"""Unit tests for ``AnthropicAPIBackend``.

Covers the spec §需求:`AnthropicAPIBackend` necessary contracts:

- SDK client constructed with ``max_retries=0`` (single-source retry).
- Capability declaration matches the production constant.
- ``__repr__`` redacts the api_key.
- Each SDK exception class wraps into the correct ``Backend*`` subtype
  (constructed with **real** ``httpx.Response`` objects per spec note —
  not mock shims, so SDK behavior changes surface as test failures).
- ``health_check`` populates ``BackendHealth`` correctly on success /
  scrubs the error text on failure.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from hostlens.agent.backend import BackendCapabilities, MessageResponse
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.core.exceptions import (
    BackendError,
    BackendRateLimited,
    BackendUnavailable,
)

_FAKE_KEY = (
    "sk-" + "ant-" + "abcdefghijklmn"
)  # pragma: allowlist secret — fake fixture, not a real key
"""Long-enough fingerprint test key; never the real value of any deployed
secret. Used by ``__repr__`` redaction tests to assert the literal does not
appear in the rendered string."""

_FAKE_LEAK_KEY = (
    "sk-" + "ant-" + "realleakvaluexyz123"
)  # pragma: allowlist secret — fake fixture, not a real key
"""Synthetic api-key-shaped substring fed into a backend exception message;
the redaction assertion in ``test_health_check_failure_redacts_api_key_in_error``
checks this literal never reaches ``BackendHealth.error``."""


def _request() -> httpx.Request:
    """Standalone helper: a real ``httpx.Request`` is required to construct
    Anthropic's ``APIStatusError`` family (the SDK uses ``request.url`` /
    ``request.method`` internally)."""

    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _ok_message_dict(*, msg_id: str = "msg_ok") -> dict[str, object]:
    """Shape of ``Message.model_dump()`` we hand back from a mocked SDK
    success path. Matches the ``MessageResponse`` field set."""

    return {
        "id": msg_id,
        "model": "claude-opus-4-7",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


class _FakeSDKMessage:
    """Stand-in for ``anthropic.types.Message`` exposing only ``model_dump``.

    The backend code never calls anything else on the SDK return value,
    so the minimal duck-typed surface is enough.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return self._payload


def _patch_messages_create(
    monkeypatch: pytest.MonkeyPatch,
    backend: AnthropicAPIBackend,
    *,
    side_effect: Any = None,
    return_value: Any = None,
) -> AsyncMock:
    """Install ``AsyncMock`` over ``backend._client.messages.create``.

    Returns the mock so the test can inspect call counts / arguments.
    """

    mock = AsyncMock(side_effect=side_effect, return_value=return_value)
    monkeypatch.setattr(backend._client.messages, "create", mock)
    return mock


def test_sdk_client_constructed_with_max_retries_zero() -> None:
    """Single-source retry budget (D-5): SDK retries MUST be off."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    assert backend._client.max_retries == 0


def test_capabilities_match_production_constant() -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    assert backend.capabilities == BackendCapabilities(
        prompt_caching=True,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )


def test_repr_does_not_leak_api_key() -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, base_url="https://api.anthropic.com")
    rendered = repr(backend)
    assert _FAKE_KEY not in rendered
    # The fingerprint helper output should appear instead.
    assert "sk-a...klmn" in rendered


@pytest.mark.asyncio
async def test_rate_limit_error_wraps_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, headers={"retry-after": "30"}, request=_request()),
        body=None,
    )
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendRateLimited) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    assert exc_info.value.retry_after_seconds == 30.0
    assert exc_info.value.cause is sdk_exc


@pytest.mark.asyncio
async def test_overloaded_error_wraps_with_no_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic._exceptions.OverloadedError(
        "overloaded",
        response=httpx.Response(529, request=_request()),
        body=None,
    )
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendRateLimited) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    assert exc_info.value.retry_after_seconds is None


@pytest.mark.asyncio
async def test_other_5xx_wraps_as_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic.APIStatusError(
        "bad gateway",
        response=httpx.Response(502, request=_request()),
        body=None,
    )
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendUnavailable) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    assert exc_info.value.cause is sdk_exc


@pytest.mark.asyncio
async def test_connection_error_wraps_as_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic.APIConnectionError(request=_request())
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendUnavailable):
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )


@pytest.mark.asyncio
async def test_authentication_error_wraps_as_backend_error_with_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic.AuthenticationError(
        "bad key",
        response=httpx.Response(401, request=_request()),
        body=None,
    )
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendError) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    # Specifically a generic BackendError, NOT a BackendUnavailable
    # (authentication failures should not be treated as retryable
    # availability events by the Agent loop).
    assert not isinstance(exc_info.value, BackendUnavailable)
    assert not isinstance(exc_info.value, BackendRateLimited)
    assert exc_info.value.kind == "auth_invalid"


@pytest.mark.asyncio
async def test_permission_denied_error_wraps_as_backend_error_with_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 403 → ``BackendError(kind="auth_invalid")``.

    Anthropic SDK raises ``PermissionDeniedError`` (subclass of
    ``APIStatusError``) for 403 responses. Like 401, this is a non-retryable
    credential failure — the Agent loop must NOT treat it as a transient
    availability event, so the wrap path mirrors ``AuthenticationError``.
    """

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    sdk_exc = anthropic.PermissionDeniedError(
        "forbidden",
        response=httpx.Response(403, request=_request()),
        body=None,
    )
    _patch_messages_create(monkeypatch, backend, side_effect=sdk_exc)

    with pytest.raises(BackendError) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    # Same classification as 401: NOT BackendUnavailable / BackendRateLimited.
    assert not isinstance(exc_info.value, BackendUnavailable)
    assert not isinstance(exc_info.value, BackendRateLimited)
    assert exc_info.value.kind == "auth_invalid"
    assert exc_info.value.cause is sdk_exc


@pytest.mark.asyncio
async def test_health_check_success_reports_healthy_with_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(
        monkeypatch,
        backend,
        return_value=_FakeSDKMessage(_ok_message_dict()),
    )

    health = await backend.health_check()
    assert health.is_healthy is True
    assert health.backend_name == "anthropic_api"
    assert health.latency_ms is not None and health.latency_ms >= 0
    assert health.error is None


@pytest.mark.asyncio
async def test_health_check_failure_redacts_api_key_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    # The SDK exception message embeds an api-key shape; the redactor must
    # strip it before the text lands in ``BackendHealth.error``.
    leaky_message = f"auth failed: api_key={_FAKE_LEAK_KEY} expired"
    _patch_messages_create(
        monkeypatch,
        backend,
        side_effect=RuntimeError(leaky_message),
    )

    health = await backend.health_check()
    assert health.is_healthy is False
    assert health.error is not None
    assert _FAKE_LEAK_KEY not in health.error


@pytest.mark.asyncio
async def test_health_check_uses_constructor_injected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ping model is the ``health_check_model`` kwarg — must NOT be
    hardcoded inside the method and MUST NOT call ``settings`` (backend
    is Settings-agnostic per ADR-008)."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, health_check_model="custom-haiku")
    mock = _patch_messages_create(
        monkeypatch,
        backend,
        return_value=_FakeSDKMessage(_ok_message_dict()),
    )
    await backend.health_check()
    # Inspect the model kwarg passed to the SDK.
    assert mock.call_args.kwargs["model"] == "custom-haiku"


@pytest.mark.asyncio
async def test_quota_check_returns_none_in_m2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    result: Awaitable[Any] = backend.quota_check()
    assert await result is None


def test_ensure_safe_for_daemon_is_noop() -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    # No assertion — must not raise. The API-key auth path is daemon-safe.
    backend.ensure_safe_for_daemon()


@pytest.mark.asyncio
async def test_messages_create_round_trip_parses_sdk_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the SDK return value's ``model_dump()`` flows through
    ``MessageResponse.model_validate`` to typed attribute access."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(
        monkeypatch,
        backend,
        return_value=_FakeSDKMessage(_ok_message_dict(msg_id="msg_round_trip")),
    )
    result = await backend.messages_create(
        model="claude-opus-4-7",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
        timeout=10.0,
    )
    assert isinstance(result, MessageResponse)
    assert result.id == "msg_round_trip"
