"""Unit tests for ``FakeBackend``.

Pins: (a) FIFO replay of canned responses, (b) ``IndexError`` on exhaustion,
(c) default capability set aligned with ``AnthropicAPIBackend``, (d)
constructor capability override, (e) backend name identity.
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


def _make_response(text: str, msg_id: str) -> MessageResponse:
    return MessageResponse(
        id=msg_id,
        model="claude-opus-4-7",
        role="assistant",
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
async def test_returns_responses_in_order() -> None:
    r1 = _make_response("first", "msg_1")
    r2 = _make_response("second", "msg_2")
    r3 = _make_response("third", "msg_3")
    backend = FakeBackend(responses=[r1, r2, r3])

    out1 = await backend.messages_create(
        model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
    )
    out2 = await backend.messages_create(
        model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
    )
    out3 = await backend.messages_create(
        model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
    )
    assert [out1.id, out2.id, out3.id] == ["msg_1", "msg_2", "msg_3"]


@pytest.mark.asyncio
async def test_exhaustion_raises_index_error_with_marker_substring() -> None:
    backend = FakeBackend(responses=[_make_response("only", "msg_only")])
    await backend.messages_create(
        model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
    )
    with pytest.raises(IndexError) as exc_info:
        await backend.messages_create(
            model="m", system="s", messages=[], tools=[], max_tokens=10, timeout=10.0
        )
    assert "FakeBackend exhausted" in str(exc_info.value)


def test_default_capabilities_align_with_anthropic_api() -> None:
    """The default capability set must match ``AnthropicAPIBackend``'s
    declaration so ``FakeBackend()`` is a drop-in production substitute
    for the "normal path" in tests (no surprise gate flips).
    """

    backend = FakeBackend(responses=[])
    assert backend.capabilities == BackendCapabilities(
        prompt_caching=True,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )


def test_constructor_capabilities_override() -> None:
    custom = BackendCapabilities(
        prompt_caching=False,
        tool_use=False,
        structured_output=False,
        parallel_tool_use=False,
        extended_thinking=False,
        vision=False,
        streaming=False,
    )
    backend = FakeBackend(responses=[], capabilities=custom)
    assert backend.capabilities == custom


def test_backend_name_class_attribute() -> None:
    assert FakeBackend.name == "fake"
    assert FakeBackend(responses=[]).name == "fake"
