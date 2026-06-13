"""Agent-loop ⇄ prompt-cache consistency for a ``prompt_caching=False`` backend.

Change ``add-openrouter-backend-config`` lets a non-Claude endpoint (DeepSeek /
Qwen via OpenRouter) declare ``prompt_caching=False`` so the Agent loop stops
injecting ``cache_control`` (CLAUDE.md §4.8 / spec scenario
「prompt_caching=False 实例注入」+「prompt_caching=False 时注入 cache_control 触发 violation」).

The loop's injection branch already keys on ``capabilities.prompt_caching``
(``loop.py`` ``_inject_cache_control`` / ``_roll_message_cache_breakpoint``).
This module does NOT re-prove the structural placement of breakpoints (that is
``test_cache_strategy.py``'s job); it proves the two facts this change adds:

1. **Loop ⇄ gate consistency, end-to-end (4.1/4.2).** When a ``prompt_caching=
   False`` backend that *actually runs* ``check_capability_consistency`` (the
   production ``FakeBackend``, unlike the recording backends that bypass the
   gate) is driven through ``AgentLoop.run()``, the run completes WITHOUT a
   ``BackendCapabilityViolation`` — i.e. the loop genuinely emits zero
   ``cache_control``, so "not caching" on a non-Claude endpoint is intentional
   and the cache-hit-rate metric is no longer distorted. The
   ``prompt_caching=True`` companion still runs clean (injection is benign on
   the Claude path).

2. **Gate fires for a non-Claude instance (4.3).** A ``prompt_caching=False``
   ``FakeBackend`` fed a ``cache_control``-laden payload in any of the three
   wire sites (``system`` / ``messages`` / ``tools``) raises
   ``BackendCapabilityViolation`` rather than silently stripping it — the
   change-specific framing of the existing capability gate, asserted against an
   instance that declares the capability ``False``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from hostlens.agent.backend import (
    BackendCapabilities,
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.loop import AgentLoop
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import BackendCapabilityViolation
from hostlens.tools.registry import ToolRegistry

from ._helpers import ctx_factory, make_spec, ok_handler

# A non-Claude (OpenRouter / DeepSeek / Qwen) capability profile: identical to
# the Anthropic default except ``prompt_caching`` is False, exactly as
# ``AnthropicAPIBackend(prompt_caching=False)`` injects it.
_NON_CLAUDE_CAPS = BackendCapabilities(
    prompt_caching=False,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)

_CLAUDE_CAPS = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)

_SYSTEM_BLOCK: list[dict[str, Any]] = [{"type": "text", "text": "you inspect hosts"}]


def _settings() -> Settings:
    return Settings(agent=AgentSettings())


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="non-claude-test",
        role="assistant",
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _tool_then_end(*, tool_turns: int) -> list[MessageResponse]:
    """N tool_use turns (each calls ``probe``) then a terminal end_turn."""
    events: list[MessageResponse] = [
        _msg(
            content=[ToolUseBlock(type="tool_use", id=f"toolu_{i}", name="probe", input={})],
            stop_reason="tool_use",
        )
        for i in range(tool_turns)
    ]
    events.append(_msg(content=[TextBlock(type="text", text="done")], stop_reason="end_turn"))
    return events


def _adapter_with(*specs: Any) -> ToolsAdapter:
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return ToolsAdapter(reg, ctx_factory())


def _loop(backend: object, adapter: ToolsAdapter) -> AgentLoop:
    return AgentLoop(cast(LLMBackend, backend), adapter, _settings(), system=_SYSTEM_BLOCK)


# ---------------------------------------------------------------------------
# 4.1 / 4.2 — loop ⇄ gate consistency end-to-end on a non-Claude backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_with_caching_disabled_backend_never_trips_gate() -> None:
    """``prompt_caching=False`` backend run through ``AgentLoop`` completes
    without ``BackendCapabilityViolation``.

    The production ``FakeBackend`` runs ``check_capability_consistency`` on
    every ``messages_create`` (unlike the recording backends in
    ``test_cache_strategy.py``). So if the loop injected even one
    ``cache_control`` block while ``prompt_caching=False``, this run would raise
    — proving the loop genuinely emits none, hence "no cache" on a non-Claude
    endpoint is intentional and the cache-hit metric is not distorted (4.2).
    """
    spec = make_spec(name="probe", handler=ok_handler)
    backend = FakeBackend(responses=_tool_then_end(tool_turns=2), capabilities=_NON_CLAUDE_CAPS)
    loop = _loop(backend, _adapter_with(spec))

    result = await loop.run("inspect host")

    assert result.final_text == "done"


@pytest.mark.asyncio
async def test_loop_with_caching_enabled_backend_runs_clean() -> None:
    """``prompt_caching=True`` companion: injection is benign on the Claude
    path — the same run with caching enabled also completes (the gate only
    fires when the *declared* capability is False)."""
    spec = make_spec(name="probe", handler=ok_handler)
    backend = FakeBackend(responses=_tool_then_end(tool_turns=2), capabilities=_CLAUDE_CAPS)
    loop = _loop(backend, _adapter_with(spec))

    result = await loop.run("inspect host")

    assert result.final_text == "done"


# ---------------------------------------------------------------------------
# 4.3 — gate fires for a non-Claude (prompt_caching=False) instance
# ---------------------------------------------------------------------------


def _noop_response() -> MessageResponse:
    return MessageResponse(
        id="msg_noop",
        model="non-claude-test",
        role="assistant",
        content=[TextBlock(type="text", text="noop")],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("system", "messages", "tools", "expected_feature"),
    [
        pytest.param(
            [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
            [],
            [],
            "cache_control_in_system_block",
            id="system",
        ),
        pytest.param(
            "plain",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
                    ],
                }
            ],
            [],
            "cache_control_in_messages_block",
            id="messages",
        ),
        pytest.param(
            "plain",
            [],
            [{"name": "x", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
            "cache_control_in_tools_array",
            id="tools",
        ),
    ],
)
async def test_non_claude_instance_cache_control_trips_gate(
    system: list[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    expected_feature: str,
) -> None:
    """A ``prompt_caching=False`` instance fed a stray ``cache_control`` block
    in any wire site raises ``BackendCapabilityViolation`` (never silently
    strips). This is the change's instance-scoped framing: the capability is
    False because of how ``AnthropicAPIBackend(prompt_caching=False)`` /
    OpenRouter is configured, and the gate must hold regardless of which
    surface carries the stray marker."""
    backend = FakeBackend(responses=[_noop_response()], capabilities=_NON_CLAUDE_CAPS)
    with pytest.raises(BackendCapabilityViolation) as exc_info:
        await backend.messages_create(
            model="deepseek/deepseek-v4-pro",
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=10,
            timeout=10.0,
        )
    assert exc_info.value.capability == "prompt_caching"
    assert exc_info.value.attempted_feature == expected_feature
