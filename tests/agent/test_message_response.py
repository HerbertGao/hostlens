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
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
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


def test_thinking_block_parses_with_signature() -> None:
    """A ``type="thinking"`` block MUST parse to ``ThinkingBlock`` with its
    ``signature`` captured — thinking-on endpoints force these into the
    response and the union must tolerate them, not crash.

    Spec §需求:MessageResponse §场景:thinking 块解析为 ThinkingBlock.
    """

    payload = _base_payload()
    payload["content"] = [
        {"type": "thinking", "thinking": "let me reason", "signature": "abc"},
        {"type": "text", "text": "hi"},
    ]

    response = MessageResponse.model_validate(payload)

    assert isinstance(response.content[0], ThinkingBlock)
    assert response.content[0].thinking == "let me reason"
    assert response.content[0].signature == "abc"
    assert isinstance(response.content[1], TextBlock)


def test_redacted_thinking_block_parses_to_its_own_class() -> None:
    """A ``type="redacted_thinking"`` block carries only ``data`` (no
    ``signature``) and MUST route to ``RedactedThinkingBlock`` — filtering
    only on ``type="thinking"`` would drop it and break verbatim relay.

    Spec §需求:MessageResponse §场景:redacted_thinking 块解析为 RedactedThinkingBlock.
    """

    payload = _base_payload()
    payload["content"] = [{"type": "redacted_thinking", "data": "opaque"}]

    response = MessageResponse.model_validate(payload)

    assert isinstance(response.content[0], RedactedThinkingBlock)
    assert response.content[0].data == "opaque"


def test_thinking_block_round_trip_preserves_extra_fields() -> None:
    """``extra="allow"`` keeps provider-private fields through
    ``model_dump()`` so the Agent loop relays the block byte-for-byte; the
    dump MUST NOT inject ``null`` for absent declared fields either.

    Spec §需求:MessageResponse §场景:thinking 块 verbatim round-trip 保真.
    """

    block_dict = {
        "type": "thinking",
        "thinking": "x",
        "signature": "s",
        "vendor_field": 1,
    }

    block = ThinkingBlock.model_validate(block_dict)
    dumped = block.model_dump()

    assert dumped["signature"] == "s"
    assert dumped["vendor_field"] == 1
    assert dumped == block_dict


def test_thinking_block_missing_signature_raises_validation_error() -> None:
    """``signature`` is required ``str``; a block lacking it is invalid (it
    must surface as ``invalid_response`` upstream, not be silently
    optional) — design.md D-5."""

    payload = _base_payload()
    payload["content"] = [{"type": "thinking", "thinking": "no sig"}]

    with pytest.raises(ValidationError):
        MessageResponse.model_validate(payload)


def test_genuinely_unknown_type_still_raises_despite_thinking_in_union() -> None:
    """Adding ``ThinkingBlock`` / ``RedactedThinkingBlock`` to the union MUST
    NOT weaken rejection of a truly unmodeled block type."""

    payload = _base_payload()
    payload["content"] = [{"type": "some_future_block", "foo": "bar"}]

    with pytest.raises(ValidationError):
        MessageResponse.model_validate(payload)


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
