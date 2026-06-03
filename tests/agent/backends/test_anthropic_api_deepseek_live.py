from __future__ import annotations

import os

import pytest
from conftest import _resolve_llm_mode

from hostlens.agent.backend import ContentBlock, MessageResponse
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

pytestmark = pytest.mark.live


_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

_ALLOWED_BLOCK_TYPES = {"text", "tool_use"}


def _deepseek_models() -> list[str]:
    raw = os.environ.get("HOSTLENS_DEEPSEEK_MODELS")
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return ["deepseek-chat", "deepseek-reasoner"]


def _assert_no_thinking(resp: MessageResponse) -> list[ContentBlock]:
    types = [block.type for block in resp.content]
    assert types, "response had no content blocks"
    assert "thinking" not in types, f"unexpected thinking block: {types}"
    for btype in types:
        assert btype in _ALLOWED_BLOCK_TYPES, f"unexpected block type {btype}: {types}"
    return list(resp.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _deepseek_models())
async def test_deepseek_multiturn_tool_loop_thinking_free(model: str) -> None:
    token = os.environ.get("HOSTLENS_DEEPSEEK_TOKEN")
    base_url = os.environ.get("HOSTLENS_DEEPSEEK_BASE_URL")
    if not token or not base_url:
        pytest.skip("HOSTLENS_DEEPSEEK_TOKEN / HOSTLENS_DEEPSEEK_BASE_URL not set")

    backend = AnthropicAPIBackend(
        api_key=token,
        base_url=base_url,
        disable_thinking=True,
    )

    messages: list = [{"role": "user", "content": "What's the weather in Paris?"}]
    resp = await backend.messages_create(
        model=model,
        system="You are Hostlens, a server inspection agent.",
        messages=messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    content = _assert_no_thinking(resp)

    tool_use = next((b for b in content if b.type == "tool_use"), None)
    if tool_use is None:
        # Endpoint chose not to call the tool; first turn already proved
        # thinking-free, nothing more to verify.
        return

    messages.append({"role": "assistant", "content": [block.model_dump() for block in content]})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "Sunny, 22C",
                }
            ],
        }
    )

    # The continuation turn is where DeepSeek historically returned 400 / forced
    # a thinking block; with disable_thinking it must succeed thinking-free.
    resp2 = await backend.messages_create(
        model=model,
        system="You are Hostlens, a server inspection agent.",
        messages=messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    _assert_no_thinking(resp2)


@pytest.mark.asyncio
async def test_anthropic_disable_thinking_is_harmless() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    backend = AnthropicAPIBackend(api_key=api_key, disable_thinking=True)
    resp = await backend.messages_create(
        model="claude-3-5-haiku-20241022",
        system="You are a test.",
        messages=[{"role": "user", "content": "Say OK"}],
        tools=[],
        max_tokens=64,
        timeout=30.0,
    )
    assert resp.content
    assert resp.stop_reason in {"end_turn", "max_tokens"}


# ---------------------------------------------------------------------------
# tolerate-inbound-thinking task 6.1: thinking-ON multiturn relay against real
# DeepSeek v4. This is the human gate run BEFORE wiring DeepSeek / upgrading the
# anthropic SDK or DeepSeek models — NOT a CI-enforced check. CI defaults to
# `-m 'not live'` so these are skipped; the marker + env two-axis alignment
# below prevents a marker-selected run from silently exercising a PlaybackBackend
# (a false green) when HOSTLENS_LLM_MODE != live.
# ---------------------------------------------------------------------------

# Per design.md "验证数据" the thinking-on gate MUST use the v4 ids the probe
# used (deepseek-v4-pro / deepseek-v4-flash), NOT the legacy
# deepseek-chat / deepseek-reasoner above — the thinking schema is verified
# against these.
_THINKING_MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _THINKING_MODELS)
async def test_deepseek_thinking_on_multiturn_tool_loop(model: str) -> None:
    """thinking-ON (no disable_thinking) multiturn tool loop against real
    DeepSeek v4: turn1 must be ``[thinking, tool_use]``, relaying the thinking
    block back verbatim must NOT 400 on turn2, and ``ThinkingBlock.signature``
    must be present (design.md D-5: DeepSeek echoes the message id as
    signature, never validates it on relay)."""

    # Two-axis alignment: the ``live`` marker alone does not guarantee a real
    # backend — without HOSTLENS_LLM_MODE=live the cassette fixture would hand
    # back a PlaybackBackend and this "live" test would assert against replayed
    # bytes (false green). Skip unless the env axis also says live.
    if _resolve_llm_mode() != "live":
        pytest.skip("HOSTLENS_LLM_MODE != live; thinking-on gate needs a real endpoint")

    token = os.environ.get("HOSTLENS_DEEPSEEK_TOKEN")
    base_url = os.environ.get("HOSTLENS_DEEPSEEK_BASE_URL")
    if not token or not base_url:
        pytest.skip("HOSTLENS_DEEPSEEK_TOKEN / HOSTLENS_DEEPSEEK_BASE_URL not set")

    # thinking-ON: do NOT set disable_thinking (default False). This is the
    # path that used to crash on the thinking block before this change.
    backend = AnthropicAPIBackend(api_key=token, base_url=base_url)

    messages: list = [{"role": "user", "content": "What's the weather in Paris?"}]
    resp = await backend.messages_create(
        model=model,
        system="You are Hostlens, a server inspection agent.",
        messages=messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    content = list(resp.content)
    types = [block.type for block in content]
    assert "thinking" in types, f"thinking-on turn1 should carry a thinking block: {types}"

    thinking_block = next(b for b in content if b.type == "thinking")
    assert thinking_block.signature, "ThinkingBlock.signature must be populated"

    tool_use = next((b for b in content if b.type == "tool_use"), None)
    assert tool_use is not None, f"thinking-on turn1 should also call the tool: {types}"

    # Relay the thinking block back verbatim (model_dump, no exclude_*) alongside
    # the tool_use, then send the tool_result. This is the turn that historically
    # 400'd when we dropped the thinking block.
    messages.append({"role": "assistant", "content": [block.model_dump() for block in content]})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "Sunny, 22C",
                }
            ],
        }
    )

    resp2 = await backend.messages_create(
        model=model,
        system="You are Hostlens, a server inspection agent.",
        messages=messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    # turn2 must NOT 400 (BackendError would have been raised by messages_create
    # before reaching here); a well-formed response with content is the proof.
    assert resp2.content, "thinking-on turn2 returned no content (relay rejected?)"
