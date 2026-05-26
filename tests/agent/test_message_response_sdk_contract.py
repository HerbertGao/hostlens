"""SDK field-alignment contract for ``MessageResponse``.

Spec §需求:MessageResponse §场景:Anthropic SDK Message.model_dump() 字段对齐契约
requires the round-trip ``Message → model_dump() → MessageResponse.model_validate()``
to preserve every field the Agent loop cares about. The dump MUST come from a
real ``anthropic.types.Message`` instance — a handwritten dict could not catch
the case where the SDK renames a field on upgrade.

A live-API variant of the same round-trip is invoked from
``tests/agent/backends/test_anthropic_api_live.py`` (created by group 5) so
the contract is also exercised against real-server payloads.
"""

from __future__ import annotations

from anthropic.types import Message as SDKMessage
from anthropic.types import TextBlock as SDKTextBlock
from anthropic.types import ToolUseBlock as SDKToolUseBlock
from anthropic.types import Usage as SDKUsage

from hostlens.agent.backend import (
    MessageResponse,
    TextBlock,
    ToolUseBlock,
)


def _build_real_sdk_message() -> SDKMessage:
    """Construct a real SDK ``Message`` covering every field the contract
    asserts on."""

    usage = SDKUsage(
        input_tokens=120,
        output_tokens=42,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=33,
    )
    text_block = SDKTextBlock(type="text", text="diagnosis complete")
    tool_block = SDKToolUseBlock(
        type="tool_use",
        id="toolu_01ABC",
        name="run_inspector",
        input={"name": "nginx_status", "target": "web01"},
    )
    return SDKMessage(
        id="msg_01XYZ",
        type="message",
        role="assistant",
        model="claude-opus-4-7-20260301",
        content=[text_block, tool_block],
        stop_reason="tool_use",
        stop_sequence=None,
        usage=usage,
    )


def test_message_response_round_trip_aligns_with_real_sdk_message() -> None:
    sdk_message = _build_real_sdk_message()

    response = MessageResponse.model_validate(sdk_message.model_dump())

    assert response.id == sdk_message.id
    assert response.model == sdk_message.model
    assert response.role == "assistant"
    assert response.stop_reason == sdk_message.stop_reason

    assert len(response.content) == len(sdk_message.content)
    # content[0] — TextBlock
    sdk_text = sdk_message.content[0]
    assert isinstance(sdk_text, SDKTextBlock)
    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].type == sdk_text.type
    assert response.content[0].text == sdk_text.text
    # content[1] — ToolUseBlock
    sdk_tool = sdk_message.content[1]
    assert isinstance(sdk_tool, SDKToolUseBlock)
    assert isinstance(response.content[1], ToolUseBlock)
    assert response.content[1].type == sdk_tool.type
    assert response.content[1].id == sdk_tool.id
    assert response.content[1].name == sdk_tool.name
    assert response.content[1].input == sdk_tool.input

    # Usage — input / output and the two prompt-cache fields.
    sdk_usage = sdk_message.usage
    assert response.usage.input_tokens == sdk_usage.input_tokens
    assert response.usage.output_tokens == sdk_usage.output_tokens
    # SDK uses int (defaults to 0) — the ``or 0`` fallback in the spec covers
    # the case where the SDK ever switches the field to optional.
    assert response.usage.cache_creation_input_tokens == (
        sdk_usage.cache_creation_input_tokens or 0
    )
    assert response.usage.cache_read_input_tokens == (sdk_usage.cache_read_input_tokens or 0)


def test_message_response_round_trip_handles_sdk_usage_with_none_cache_fields() -> None:
    """Real SDK ``Usage`` emits explicit ``None`` for the cache fields when
    they were not passed to the constructor (non-cached response). The
    ``model_dump()`` round-trip MUST normalize those to 0 so the Agent loop
    can read them as plain ``int``.

    Spec §需求:MessageResponse §场景:SDK Usage None 字段归零归一.
    """

    # Note: cache_creation_input_tokens / cache_read_input_tokens NOT passed
    # to the constructor. SDK sets them to None, model_dump() emits None.
    sdk_usage = SDKUsage(input_tokens=7, output_tokens=3)
    dumped = sdk_usage.model_dump()
    # Sanity: confirm the SDK behavior this test relies on.
    assert dumped["cache_creation_input_tokens"] is None
    assert dumped["cache_read_input_tokens"] is None

    text_block = SDKTextBlock(type="text", text="no cache")
    sdk_message = SDKMessage(
        id="msg_no_cache",
        type="message",
        role="assistant",
        model="claude-opus-4-7-20260301",
        content=[text_block],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=sdk_usage,
    )

    response = MessageResponse.model_validate(sdk_message.model_dump())

    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3
    assert response.usage.cache_creation_input_tokens == 0
    assert response.usage.cache_read_input_tokens == 0
