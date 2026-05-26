"""Capability gate behavior across the ``check_capability_consistency`` helper.

Exercises every place ``cache_control`` can appear in the Anthropic
Messages API request shape (``system`` / ``messages[*].content`` /
``tools[*]``) and the separate ``tool_use=False + tools_non_empty`` path.

Per spec §需求:`BackendCapabilityViolation`, a backend that declares a
capability ``False`` MUST raise (not silently strip) so cache-hit-rate
metrics stay observable.
"""

from __future__ import annotations

import pytest

from hostlens.agent.backend import (
    BackendCapabilities,
    MessageResponse,
    TextBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.core.exceptions import BackendCapabilityViolation


def _noop_response() -> MessageResponse:
    """Minimal valid response — never returned in these tests but required
    by ``FakeBackend.__init__`` for a non-empty queue."""

    return MessageResponse(
        id="msg_noop",
        model="claude-opus-4-7",
        role="assistant",
        content=[TextBlock(type="text", text="noop")],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _caps_disable(**overrides: bool) -> BackendCapabilities:
    """Build a capability set with selected fields flipped to False.

    Defaults match ``AnthropicAPIBackend.capabilities``; tests call this
    helper to keep their arrange section focused on the field that matters.
    """

    base = {
        "prompt_caching": True,
        "tool_use": True,
        "structured_output": True,
        "parallel_tool_use": True,
        "extended_thinking": False,
        "vision": True,
        "streaming": False,
    }
    base.update(overrides)
    return BackendCapabilities(**base)


@pytest.mark.asyncio
async def test_cache_control_in_system_block_raises_when_caching_disabled() -> None:
    backend = FakeBackend(
        responses=[_noop_response()],
        capabilities=_caps_disable(prompt_caching=False),
    )
    with pytest.raises(BackendCapabilityViolation) as exc_info:
        await backend.messages_create(
            model="m",
            system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
            messages=[],
            tools=[],
            max_tokens=10,
            timeout=10.0,
        )
    assert exc_info.value.attempted_feature == "cache_control_in_system_block"
    assert exc_info.value.capability == "prompt_caching"


@pytest.mark.asyncio
async def test_cache_control_in_messages_block_raises_when_caching_disabled() -> None:
    backend = FakeBackend(
        responses=[_noop_response()],
        capabilities=_caps_disable(prompt_caching=False),
    )
    with pytest.raises(BackendCapabilityViolation) as exc_info:
        await backend.messages_create(
            model="m",
            system="plain",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
                    ],
                }
            ],
            tools=[],
            max_tokens=10,
            timeout=10.0,
        )
    assert exc_info.value.attempted_feature == "cache_control_in_messages_block"


@pytest.mark.asyncio
async def test_cache_control_in_tools_array_raises_when_caching_disabled() -> None:
    backend = FakeBackend(
        responses=[_noop_response()],
        capabilities=_caps_disable(prompt_caching=False),
    )
    with pytest.raises(BackendCapabilityViolation) as exc_info:
        await backend.messages_create(
            model="m",
            system="plain",
            messages=[],
            tools=[{"name": "x", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
            max_tokens=10,
            timeout=10.0,
        )
    assert exc_info.value.attempted_feature == "cache_control_in_tools_array"


@pytest.mark.asyncio
async def test_tools_non_empty_raises_when_tool_use_disabled() -> None:
    backend = FakeBackend(
        responses=[_noop_response()],
        capabilities=_caps_disable(tool_use=False),
    )
    with pytest.raises(BackendCapabilityViolation) as exc_info:
        await backend.messages_create(
            model="m",
            system="plain",
            messages=[],
            tools=[{"name": "x", "input_schema": {}}],
            max_tokens=10,
            timeout=10.0,
        )
    assert exc_info.value.capability == "tool_use"
    assert exc_info.value.attempted_feature == "tools_array_non_empty"


@pytest.mark.asyncio
async def test_anthropic_default_capabilities_allow_cache_control_everywhere() -> None:
    """Production-path default (``prompt_caching=True``) MUST pass through
    ``cache_control`` blocks in all three locations without raising."""

    backend = FakeBackend(responses=[_noop_response()])  # default caps
    # No exception expected. We use a single call exercising all 3 sites.
    result = await backend.messages_create(
        model="m",
        system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        tools=[{"name": "x", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
        max_tokens=10,
        timeout=10.0,
    )
    assert result.id == "msg_noop"


def test_attempted_feature_field_is_literal_constrained() -> None:
    """The exception class itself enforces the Literal domain at __init__
    time so the helper can never produce free-text ``attempted_feature``
    values that would defeat the prompt-/log-injection bound."""

    with pytest.raises(ValueError):
        BackendCapabilityViolation(
            backend_name="x",
            capability="prompt_caching",
            attempted_feature="cache_control; rm -rf /",  # type: ignore[arg-type]
        )
