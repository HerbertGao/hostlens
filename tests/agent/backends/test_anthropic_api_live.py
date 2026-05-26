"""Live Anthropic API smoke test (skipped in CI; opt-in via ``-m live``).

Triggers a single real ``messages.create`` against the configured
``ANTHROPIC_API_KEY`` so SDK API changes / network reachability /
authentication regressions are caught by a developer running
``pytest -m live`` locally before opening a PR.

CI does NOT run this file by default. ``[tool.pytest.ini_options].addopts``
sets ``-m 'not live'`` so the marker filter selects only opt-in
invocations.

Two assertions in one test:

1. ``response.stop_reason == "end_turn"`` — basic happy-path check that the
   real API answered the ping prompt within ``max_tokens=20``.
2. ``MessageResponse.model_validate(message.model_dump())`` round-trip on
   the real-API response payload — same field-alignment contract as
   ``tests/agent/test_message_response_sdk_contract.py`` (which uses a
   handwritten SDK ``Message``), but here run against an actual server
   payload so a downstream SDK rename / field reshuffle would surface
   before a PR merges.
"""

from __future__ import annotations

import os

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message as SDKMessage
from anthropic.types import TextBlock as SDKTextBlock

from hostlens.agent.backend import MessageResponse, TextBlock


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_messages_create_ping_and_round_trip() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set; skip live ping")

    # Drive the SDK directly rather than via AnthropicAPIBackend so the
    # ``message.model_dump()`` round-trip below can compare field-by-field
    # against the real SDK ``Message`` object instance, not the lossy
    # ``MessageResponse`` projection.
    client = AsyncAnthropic(api_key=api_key, max_retries=0)
    sdk_message: SDKMessage = await client.messages.create(
        model="claude-haiku-4-5",
        system="You answer in one short word.",
        messages=[{"role": "user", "content": "Say 'pong'."}],
        max_tokens=20,
        timeout=30.0,
    )

    # The model SHOULD complete with end_turn — anything else (max_tokens,
    # refusal, etc.) is suspicious for a 20-token "pong" reply.
    assert sdk_message.stop_reason == "end_turn"

    # Round-trip the real-API SDK Message through MessageResponse and assert
    # every field the contract test asserts on aligns. Spec §需求:MessageResponse
    # §场景:Anthropic SDK Message.model_dump() 字段对齐契约.
    response = MessageResponse.model_validate(sdk_message.model_dump())

    assert response.id == sdk_message.id
    assert response.model == sdk_message.model
    assert response.role == "assistant"
    assert response.stop_reason == sdk_message.stop_reason

    # Real-API responses for this ping prompt return exactly one TextBlock;
    # we still iterate so the round-trip is exercised generically rather
    # than positionally.
    assert len(response.content) == len(sdk_message.content)
    for projected, sdk_block in zip(response.content, sdk_message.content, strict=True):
        assert projected.type == sdk_block.type
        if isinstance(sdk_block, SDKTextBlock):
            assert isinstance(projected, TextBlock)
            assert projected.text == sdk_block.text

    # Usage fields — input / output / cache_creation / cache_read.
    sdk_usage = sdk_message.usage
    assert response.usage.input_tokens == sdk_usage.input_tokens
    assert response.usage.output_tokens == sdk_usage.output_tokens
    # SDK uses int (defaults to 0); the ``or 0`` fallback covers a future
    # SDK switch to ``int | None``.
    assert response.usage.cache_creation_input_tokens == (
        sdk_usage.cache_creation_input_tokens or 0
    )
    assert response.usage.cache_read_input_tokens == (sdk_usage.cache_read_input_tokens or 0)
