from __future__ import annotations

import os

import pytest

from hostlens.agent.backend import MessageResponse
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


def _assert_no_thinking(resp: MessageResponse) -> list[dict]:
    types = [block.get("type") for block in resp.content]
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

    tool_use = next((b for b in content if b.get("type") == "tool_use"), None)
    if tool_use is None:
        # Endpoint chose not to call the tool; first turn already proved
        # thinking-free, nothing more to verify.
        return

    messages.append({"role": "assistant", "content": content})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
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
