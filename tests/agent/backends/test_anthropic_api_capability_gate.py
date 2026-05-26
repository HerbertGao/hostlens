"""``AnthropicAPIBackend`` capability gate — normal path must NOT trigger.

The production backend declares ``prompt_caching=True`` and ``tool_use=True``,
so ``cache_control`` blocks and non-empty ``tools`` arrays should flow
through to the SDK without raising ``BackendCapabilityViolation``. This
test pins the negative path (no violation when capabilities are present)
so a future refactor cannot accidentally turn the gate into an always-on
guard.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.core.exceptions import BackendCapabilityViolation

_FAKE_KEY = (
    "sk-" + "ant-" + "abcdefghijklmn"
)  # pragma: allowlist secret — fake fixture, not a real key


class _FakeSDKMessage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


def _ok_message_dict() -> dict[str, Any]:
    return {
        "id": "msg_ok",
        "model": "claude-opus-4-7",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.mark.asyncio
async def test_cache_control_in_system_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    monkeypatch.setattr(
        backend._client.messages,
        "create",
        AsyncMock(return_value=_FakeSDKMessage(_ok_message_dict())),
    )
    # No exception expected: production capabilities allow ``cache_control``
    # in the ``system`` block.
    result = await backend.messages_create(
        model="claude-opus-4-7",
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
        timeout=10.0,
    )
    assert result.id == "msg_ok"


@pytest.mark.asyncio
async def test_cache_control_in_messages_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    monkeypatch.setattr(
        backend._client.messages,
        "create",
        AsyncMock(return_value=_FakeSDKMessage(_ok_message_dict())),
    )
    result = await backend.messages_create(
        model="claude-opus-4-7",
        system="plain",
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        tools=[],
        max_tokens=10,
        timeout=10.0,
    )
    assert result.id == "msg_ok"


@pytest.mark.asyncio
async def test_cache_control_in_tools_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    monkeypatch.setattr(
        backend._client.messages,
        "create",
        AsyncMock(return_value=_FakeSDKMessage(_ok_message_dict())),
    )
    result = await backend.messages_create(
        model="claude-opus-4-7",
        system="plain",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "x", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
        max_tokens=10,
        timeout=10.0,
    )
    assert result.id == "msg_ok"


@pytest.mark.asyncio
async def test_gate_is_active_for_unknown_attempted_feature_construction() -> None:
    """Defense in depth: the Literal-domain check happens in the exception
    constructor, so even if the gate logic were bypassed the type system
    would still reject free-text feature names. Mirror of
    ``test_capability_gate.test_attempted_feature_field_is_literal_constrained``
    but kept here to lock the production-path file too.
    """

    with pytest.raises(ValueError):
        BackendCapabilityViolation(
            backend_name="anthropic_api",
            capability="prompt_caching",
            attempted_feature="cache_control; rm -rf /",  # type: ignore[arg-type]
        )
