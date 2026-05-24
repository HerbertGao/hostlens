"""Tests for `ToolsAdapter` ToolContext factory injection per spec
§需求:ToolsAdapter 必须接受 ToolContext 工厂注入.

Two scenarios:
1. The factory is invoked on every `dispatch` call when no explicit `ctx`
   is passed — never shared across turns. Verified via a counter wrapper.
2. The adapter must accept an empty `ToolRegistry`: `list_for_agent()`
   returns `[]`, and `dispatch(...)` raises `KeyError` (from
   `ToolRegistry.get`) — the adapter does NOT capture that lookup error.
"""

from __future__ import annotations

import asyncio

import pytest

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import make_ctx, make_spec


def test_context_factory_is_called_once_per_dispatch_without_explicit_ctx() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="ok_tool"))

    calls = {"count": 0}

    def counting_factory() -> ToolContext:
        calls["count"] += 1
        return make_ctx()

    adapter = ToolsAdapter(reg, counting_factory)

    async def go() -> None:
        # No explicit ctx → adapter must invoke factory each time.
        await adapter.dispatch("ok_tool", {})
        await adapter.dispatch("ok_tool", {})

    asyncio.run(go())

    assert calls["count"] == 2


def test_context_factory_is_not_called_when_explicit_ctx_passed() -> None:
    """Sanity: when caller passes ctx explicitly, the factory must stay
    untouched (verifies adapter doesn't redundantly build contexts)."""
    reg = ToolRegistry()
    reg.register(make_spec(name="ok_tool"))

    calls = {"count": 0}

    def counting_factory() -> ToolContext:
        calls["count"] += 1
        return make_ctx()

    adapter = ToolsAdapter(reg, counting_factory)

    async def go() -> None:
        await adapter.dispatch("ok_tool", {}, make_ctx())

    asyncio.run(go())

    assert calls["count"] == 0


def test_empty_registry_is_allowed_and_list_for_agent_returns_empty() -> None:
    reg = ToolRegistry()  # empty
    adapter = ToolsAdapter(reg, lambda: make_ctx())

    assert adapter.list_for_agent() == []


def test_empty_registry_dispatch_raises_key_error() -> None:
    reg = ToolRegistry()  # empty
    adapter = ToolsAdapter(reg, lambda: make_ctx())

    async def go() -> None:
        with pytest.raises(KeyError):
            await adapter.dispatch("does_not_exist", {})

    asyncio.run(go())
