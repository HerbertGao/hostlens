"""tolerate-inbound-thinking tasks 6.2 + 6.3 — synthetic-cassette replay +
retry/usage smoke.

Task 6.2: a HAND-WRITTEN synthetic cassette
(``tests/fixtures/cassettes/deepseek_thinking_tolerate_multiturn.jsonl``)
locks down "parse + verbatim relay + keying 归一化" for inbound thinking blocks
WITHOUT recording real DeepSeek (the sensitive-content gate would scan the
response CoT and poison it — see design.md "待解决问题" decision). The thinking
text is pattern-free synthetic prose that avoids every ``core/redact.py`` rule
(no IPv4 / FQDN / ``/Users`` / ``.ssh`` / email / ``sk-`` / Bearer / JWT /
credential-assignment), so ``cassette_lint.py`` passes. The two turns differ on
their non-thinking content (turn1 user-only; turn2 user + assistant tool_use +
tool_result), so thinking-stripped keying never collides into a duplicate key.

Task 6.3: a mock-SDK smoke confirms a thinking-bearing response still
accumulates usage normally. This smoke does NOT itself exercise the 429
honor-retry-after / 5xx degraded paths — those stay covered by
``test_anthropic_api.py`` and we do not rewrite them. The reason the change
cannot affect them is structural, not demonstrated here: the thinking-block
parse happens AFTER any SDK exception would have fired (see
``anthropic_api.py`` — exception classification precedes ``model_validate``),
so retry/degrade classification is untouched. The smoke only confirms a
thinking-bearing happy-path response still tallies usage correctly.

CI runs these in the default replay mode (``-m 'not live'``); no API key,
no network. ``asyncio_mode = "auto"`` (pyproject) — no marker needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hostlens.agent.backend import MessageResponse
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.agent.backends.playback import CassetteMiss, PlaybackBackend

_CASSETTE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "cassettes"
    / "deepseek_thinking_tolerate_multiturn.jsonl"
)

# Must match the synthetic cassette's request key inputs verbatim
# (key = SHA256 over model + thinking-stripped messages + tools_count).
_MODEL = "deepseek-v4-thinking-synthetic"
_SYSTEM = "You are Hostlens, a server inspection agent."
_TOOLS: list[dict] = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]
_FIRST_MESSAGES: list[dict] = [{"role": "user", "content": "Check the weather in Paris."}]
_TOOL_RESULT_TEXT = "Sunny, 22C"


async def test_thinking_tolerate_multiturn_replay() -> None:
    """Replay the synthetic thinking cassette through ``PlaybackBackend``:

    - turn1 response parses ``[thinking, tool_use]`` (thinking block modeled,
      not crashed);
    - the assistant content is relayed back verbatim via ``model_dump()``
      (the thinking block carried into turn2 messages WITH its ``thinking`` /
      ``signature``);
    - keying 归一化 drops the thinking block before hashing, so turn2 still
      HITS the cassette (no ``CassetteMiss``) and parses ``[thinking, text]``.
    """

    backend = PlaybackBackend(cassette_path=_CASSETTE)

    # turn 1 — thinking block parsed, tool_use present.
    r1 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=_FIRST_MESSAGES,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    types1 = [b.type for b in r1.content]
    assert "thinking" in types1, f"turn1 must parse a thinking block: {types1}"
    thinking1 = next(b for b in r1.content if b.type == "thinking")
    assert thinking1.signature == "sig-synth-0001"
    tool_use = next((b for b in r1.content if b.type == "tool_use"), None)
    assert tool_use is not None, f"turn1 must carry tool_use: {types1}"

    # Relay assistant content VERBATIM (model_dump, no exclude_*). The thinking
    # block must survive into the turn2 messages byte-for-byte.
    relayed = [b.model_dump() for b in r1.content]
    relayed_thinking = next(b for b in relayed if b["type"] == "thinking")
    assert relayed_thinking == {
        "type": "thinking",
        "thinking": "evaluating the load metric trend before concluding",
        "signature": "sig-synth-0001",
    }

    second_messages = [
        *_FIRST_MESSAGES,
        {"role": "assistant", "content": relayed},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": _TOOL_RESULT_TEXT,
                }
            ],
        },
    ]

    # turn 2 — even though we send the thinking block back, keying 归一化 strips
    # it so this still HITS the cassette (would raise CassetteMiss otherwise).
    r2 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=second_messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    types2 = [b.type for b in r2.content]
    assert "thinking" in types2 and "text" in types2, f"turn2 shape: {types2}"


async def test_thinking_relay_keying_independent_of_signature_drift() -> None:
    """The thinking block's ``thinking`` / ``signature`` are non-deterministic
    per turn; keying 归一化 drops the whole block, so a relayed turn with a
    DIFFERENT signature still hits the same cassette record (no miss)."""

    backend = PlaybackBackend(cassette_path=_CASSETTE)
    r1 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=_FIRST_MESSAGES,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    tool_use = next(b for b in r1.content if b.type == "tool_use")

    # Forge a relay whose thinking text + signature differ from what was
    # recorded — keying must still drop the block and hit turn2.
    drifted_assistant = [
        {
            "type": "thinking",
            "thinking": "a totally different reasoning string this time",
            "signature": "sig-drifted-9999",
            "extra_provider_field": "ignored by keying",
        },
        {"type": "tool_use", "id": tool_use.id, "name": "get_weather", "input": {"city": "Paris"}},
    ]
    second_messages = [
        *_FIRST_MESSAGES,
        {"role": "assistant", "content": drifted_assistant},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use.id, "content": _TOOL_RESULT_TEXT}
            ],
        },
    ]

    try:
        r2 = await backend.messages_create(
            model=_MODEL,
            system=_SYSTEM,
            messages=second_messages,
            tools=_TOOLS,
            max_tokens=1024,
            timeout=60.0,
        )
    except CassetteMiss as exc:  # pragma: no cover - failure path
        pytest.fail(f"keying should ignore thinking drift but missed: {exc}")
    assert r2.content


# ---------------------------------------------------------------------------
# Task 6.3: thinking response still accumulates usage; no retry/degrade impact.
# ---------------------------------------------------------------------------

_FAKE_KEY = "sk-" + "ant-" + "abcdefghijklmn"  # pragma: allowlist secret - fake fixture


def _thinking_message_dict() -> dict[str, Any]:
    """An SDK ``Message.model_dump()`` shape that carries a thinking block plus
    a usable usage block, mirroring a thinking-on endpoint's response."""

    return {
        "id": "msg_thinking_usage",
        "model": "deepseek-v4-pro",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "weighing the metric reading", "signature": "sig-x"},
            {"type": "text", "text": "all healthy"},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 42,
            "output_tokens": 7,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 11,
        },
    }


class _FakeSDKMessage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


async def test_thinking_response_accumulates_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thinking-bearing response parses and surfaces usage exactly as a
    thinking-free one would — the union expansion does not disturb usage
    accounting (input/output/cache token fields all intact)."""

    backend = AnthropicAPIBackend(api_key=_FAKE_KEY)
    monkeypatch.setattr(
        backend._client.messages,
        "create",
        AsyncMock(return_value=_FakeSDKMessage(_thinking_message_dict())),
    )

    result = await backend.messages_create(
        model="deepseek-v4-pro",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=64,
        timeout=10.0,
    )

    assert isinstance(result, MessageResponse)
    assert result.content[0].type == "thinking"
    # Usage is accumulated normally despite the leading thinking block.
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7
    assert result.usage.cache_creation_input_tokens == 3
    assert result.usage.cache_read_input_tokens == 11
