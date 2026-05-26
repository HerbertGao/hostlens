"""Discriminator + ``extra="ignore"`` contract for ``MessageResponse``.

These tests pin the *handwritten dict* validation path: discriminator routing
between ``TextBlock`` and ``ToolUseBlock``, the explicit reject on an unknown
block ``type``, the SDK-future-proof ignore-extra behaviour, and the
defaulting of the two prompt-cache usage fields (which cassettes / fakes
routinely omit).

The SDK-shape contract test (``message.model_dump() -> model_validate``)
lives in ``test_message_response_sdk_contract.py`` so this file stays free
of any ``anthropic`` import.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.agent.backend import (
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)


def _base_payload() -> dict[str, object]:
    return {
        "id": "msg_test",
        "model": "claude-opus-4-7",
        "role": "assistant",
        "content": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def test_discriminator_routes_text_and_tool_use_blocks_to_typed_classes() -> None:
    """Mixed-block content list MUST resolve each entry to its specific
    block class via the ``type`` discriminator, not the raw union."""

    payload = _base_payload()
    payload["content"] = [
        {"type": "text", "text": "hi"},
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "list_inspectors",
            "input": {"query": "all"},
        },
    ]

    response = MessageResponse.model_validate(payload)

    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].text == "hi"
    assert isinstance(response.content[1], ToolUseBlock)
    assert response.content[1].id == "toolu_01"
    assert response.content[1].name == "list_inspectors"
    assert response.content[1].input == {"query": "all"}


def test_unknown_block_type_raises_validation_error() -> None:
    """An unknown ``type`` must fail fast — silent-drop would hide an SDK
    drift / a backend that returned a block the Agent loop cannot dispatch."""

    payload = _base_payload()
    payload["content"] = [{"type": "unknown_block_type", "foo": "bar"}]

    with pytest.raises(ValidationError):
        MessageResponse.model_validate(payload)


def test_undeclared_top_level_field_is_ignored_not_rejected() -> None:
    """The Anthropic SDK adds fields over time (``container`` /
    ``stop_details`` / ...). ``extra="ignore"`` keeps us forward-compatible.

    Spec §需求:MessageResponse §场景:Anthropic SDK 新增字段不破坏解析.
    """

    payload = _base_payload()
    payload["container"] = {"id": "ctr_x"}  # type: ignore[index]
    payload["stop_details"] = None  # type: ignore[index]
    payload["type"] = "message"  # type: ignore[index]
    payload["stop_sequence"] = None  # type: ignore[index]
    payload["content"] = [{"type": "text", "text": "ok"}]

    response = MessageResponse.model_validate(payload)

    assert response.id == "msg_test"
    assert isinstance(response.content[0], TextBlock)
    # Undeclared field MUST be silently dropped, not attached.
    assert not hasattr(response, "container")


def test_cache_read_input_tokens_defaults_to_zero_when_omitted() -> None:
    """Cassettes and ``FakeBackend`` constructs frequently omit the
    cache-usage fields; defaulting to 0 keeps those paths valid while
    preserving the field's existence on the model (so the Agent loop can
    always read ``usage.cache_read_input_tokens`` unconditionally)."""

    payload = _base_payload()
    payload["content"] = [{"type": "text", "text": "ok"}]
    payload["usage"] = {"input_tokens": 100, "output_tokens": 20}

    response = MessageResponse.model_validate(payload)

    assert isinstance(response.usage, Usage)
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.cache_creation_input_tokens == 0
    assert response.usage.cache_read_input_tokens == 0
