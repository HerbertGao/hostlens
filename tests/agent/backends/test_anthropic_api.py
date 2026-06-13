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
    # ``capabilities`` is now a per-instance attribute (design.md D-3); the
    # default construction must still equal the historical 7-field constant.
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


def test_prompt_caching_false_flips_only_that_field() -> None:
    """``prompt_caching=False`` → that one field flips; the other 6 stay."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, prompt_caching=False)
    assert backend.capabilities.prompt_caching is False
    assert backend.capabilities == BackendCapabilities(
        prompt_caching=False,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )


def test_prompt_caching_default_true() -> None:
    """Default (no kwarg) → ``prompt_caching=True`` (behavior unchanged)."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    assert backend.capabilities.prompt_caching is True


def test_extra_headers_passed_to_sdk_default_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra_headers`` non-None → SDK client built with ``default_headers``."""

    captured: dict[str, Any] = {}

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return AsyncMock()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _spy)
    headers = {"HTTP-Referer": "https://x", "X-OpenRouter-Title": "hostlens"}
    AnthropicAPIBackend(api_key=_FAKE_KEY, extra_headers=headers)
    assert captured["default_headers"] == headers


def test_extra_headers_default_omits_default_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (no ``extra_headers``) → SDK client built WITHOUT
    ``default_headers`` (real Anthropic request shape unchanged)."""

    captured: dict[str, Any] = {}

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return AsyncMock()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _spy)
    AnthropicAPIBackend(api_key=_FAKE_KEY)
    assert "default_headers" not in captured


def test_repr_does_not_leak_api_key() -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, base_url="https://api.anthropic.com")
    rendered = repr(backend)
    assert _FAKE_KEY not in rendered
    # The fingerprint helper output should appear instead.
    assert "sk-a...klmn" in rendered


def test_repr_masks_extra_header_values() -> None:
    """``extra_headers`` values are masked to ``***`` unconditionally (D-5).

    The probe value is deliberately a form ``redact_text`` does NOT match
    (no ``sk-`` / Bearer / JWT / URL shape) so an implementation that wrongly
    routes through form-based redaction would leak it and fail this assertion.
    """

    backend = AnthropicAPIBackend(
        api_key=_FAKE_KEY,
        extra_headers={"X-Custom-Auth": "not-a-real-secret-PROBE-0001"},
    )
    rendered = repr(backend)
    assert "not-a-real-secret-PROBE-0001" not in rendered
    # Key kept for debugging, value fully masked.
    assert "X-Custom-Auth" in rendered
    assert "***" in rendered


def test_repr_extra_headers_none_renders_none() -> None:
    """Default backend (no extra_headers) renders ``extra_headers=None``."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    assert "extra_headers=None" in repr(backend)


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


# ---------------------------------------------------------------------------
# add-backend-disable-thinking: thinking:disabled injection + error normalization
# ---------------------------------------------------------------------------

_DISABLED_EXTRA_BODY = {"thinking": {"type": "disabled"}}


async def _call_simple(backend: AnthropicAPIBackend) -> Any:
    return await backend.messages_create(
        model="m",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_disable_thinking_true_injects_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.1: ``disable_thinking=True`` → SDK call carries the disabled extra_body."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, disable_thinking=True)
    mock = _patch_messages_create(
        monkeypatch, backend, return_value=_FakeSDKMessage(_ok_message_dict())
    )
    await _call_simple(backend)
    assert mock.call_args.kwargs["extra_body"] == _DISABLED_EXTRA_BODY


@pytest.mark.asyncio
async def test_disable_thinking_false_passes_no_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.2: default → NO thinking field at all (real Anthropic shape unchanged)."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    mock = _patch_messages_create(
        monkeypatch, backend, return_value=_FakeSDKMessage(_ok_message_dict())
    )
    await _call_simple(backend)
    kwargs = mock.call_args.kwargs
    assert "extra_body" not in kwargs
    assert "thinking" not in kwargs


@pytest.mark.asyncio
async def test_disable_thinking_injection_no_cache_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.4: the injected ``extra_body`` carries no ``cache_control`` and the
    capability gate (which scans system/messages/tools, not extra_body) is not
    tripped — the call completes without ``BackendCapabilityViolation``."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, disable_thinking=True)
    mock = _patch_messages_create(
        monkeypatch, backend, return_value=_FakeSDKMessage(_ok_message_dict())
    )
    result = await _call_simple(backend)
    assert isinstance(result, MessageResponse)
    assert "cache_control" not in mock.call_args.kwargs["extra_body"]["thinking"]


@pytest.mark.asyncio
async def test_disable_thinking_injected_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.5: two consecutive calls on the same backend both inject (proves OUR
    side always injects; does NOT prove provider multi-turn behavior)."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, disable_thinking=True)
    mock = _patch_messages_create(
        monkeypatch, backend, return_value=_FakeSDKMessage(_ok_message_dict())
    )
    await _call_simple(backend)
    await _call_simple(backend)
    assert mock.call_count == 2
    for call in mock.call_args_list:
        assert call.kwargs["extra_body"] == _DISABLED_EXTRA_BODY


def test_disable_thinking_does_not_flip_extended_thinking_capability() -> None:
    """4.7③: capability stays False even with the suppression switch on."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY, disable_thinking=True)
    assert backend.capabilities.extended_thinking is False


@pytest.mark.asyncio
async def test_unmodeled_content_block_normalizes_to_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truly unmodeled block type (neither text/tool_use nor
    thinking/redacted_thinking) → ``BackendError(kind="unsupported_content_block")``,
    never a bare ``ValidationError``. ``thinking`` is now modeled and no longer
    triggers this path (see ``test_thinking_block_parses_not_unsupported``)."""

    payload = _ok_message_dict()
    payload["content"] = [{"type": "some_future_block", "data": "x"}]
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(monkeypatch, backend, return_value=_FakeSDKMessage(payload))

    with pytest.raises(BackendError) as exc_info:
        await _call_simple(backend)
    assert exc_info.value.kind == "unsupported_content_block"
    assert exc_info.value.cause is not None


@pytest.mark.asyncio
async def test_thinking_block_parses_not_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``type="thinking"`` block now parses successfully into the
    ``ContentBlock`` union; it must NOT trigger
    ``unsupported_content_block`` (tolerate-inbound-thinking Path 1)."""

    payload = _ok_message_dict()
    payload["content"] = [
        {"type": "thinking", "thinking": "synthetic reasoning", "signature": "sig-1"},
        {"type": "text", "text": "ok"},
    ]
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(monkeypatch, backend, return_value=_FakeSDKMessage(payload))

    result = await _call_simple(backend)
    assert isinstance(result, MessageResponse)
    assert result.content[0].type == "thinking"
    assert result.content[0].signature == "sig-1"


@pytest.mark.asyncio
async def test_thinking_block_missing_signature_normalizes_to_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thinking block missing its required ``signature`` is a field-level
    validation failure (not an unknown block type) → ``invalid_response``,
    NOT ``unsupported_content_block`` (spec D-7)."""

    payload = _ok_message_dict()
    payload["content"] = [{"type": "thinking", "thinking": "no signature here"}]
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(monkeypatch, backend, return_value=_FakeSDKMessage(payload))

    with pytest.raises(BackendError) as exc_info:
        await _call_simple(backend)
    assert exc_info.value.kind == "invalid_response"
    assert exc_info.value.cause is not None


@pytest.mark.asyncio
async def test_generic_validation_drift_normalizes_to_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.7②: a non-content-block format failure (unknown ``stop_reason`` enum)
    → ``BackendError(kind="invalid_response")``, NOT mislabeled as a thinking
    block problem."""

    payload = _ok_message_dict()
    payload["stop_reason"] = "totally_unknown_reason"
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    _patch_messages_create(monkeypatch, backend, return_value=_FakeSDKMessage(payload))

    with pytest.raises(BackendError) as exc_info:
        await _call_simple(backend)
    assert exc_info.value.kind == "invalid_response"
