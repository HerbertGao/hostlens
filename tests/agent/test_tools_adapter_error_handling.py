"""Tests for `ToolsAdapter.dispatch` exception wrapping per spec
§需求:handler 异常必须包装成 tool_error 返回结构化字段.

Three scenarios:
1. Generic handler exception (e.g. `ValueError`) is wrapped into a
   `tool_error` envelope and returned.
2. `ToolPolicyViolation` raised by the handler propagates unwrapped.
3. tool_error envelope `message` / `cause` fields never leak sensitive
   substrings (paths, IPs, credentials, identity assignments, emails),
   but `error_kind` and `tool_name` are preserved verbatim (both
   constrained to safe value domains).
"""

from __future__ import annotations

import asyncio

import pytest

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import EmptyInput, EmptyOutput, ctx_factory, make_ctx, make_spec


async def _value_error_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise ValueError("invalid arg")


async def _policy_violation_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise ToolPolicyViolation(
        tool_name="handler_raised_tool",
        surface="agent",
        violated_field="target_constraints",
        reason="target_constraint_violated",
    )


async def _cancelled_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise asyncio.CancelledError("simulated Ctrl-C")


async def _leaky_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise ConnectionError(
        "connect to /Users/alice/.ssh/id_rsa failed via user=admin host=10.0.0.5 token=Bearer xyz"
    )


def test_handler_value_error_is_wrapped_as_tool_error() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="value_error_tool", handler=_value_error_handler))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> dict[str, object]:
        return await adapter.dispatch("value_error_tool", {}, make_ctx())

    result = asyncio.run(go())

    assert result["is_error"] is True
    assert result["error_kind"] == "ValueError"
    assert result["tool_name"] == "value_error_tool"
    assert "invalid arg" in str(result["message"])
    assert "cause" in result


def test_handler_tool_policy_violation_propagates_unwrapped() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="policy_tool", handler=_policy_violation_handler))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await adapter.dispatch("policy_tool", {}, make_ctx())
        err = ei.value
        assert err.tool_name == "handler_raised_tool"
        assert err.violated_field == "target_constraints"
        assert err.reason == "target_constraint_violated"

    asyncio.run(go())


def test_handler_cancelled_error_propagates_unwrapped() -> None:
    """asyncio.CancelledError must propagate to the caller, not be swallowed
    by the broad `except Exception` as a tool_error envelope — otherwise the
    Agent loop cannot stop when the user hits Ctrl-C.
    """
    reg = ToolRegistry()
    reg.register(make_spec(name="cancelled_tool", handler=_cancelled_handler))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(asyncio.CancelledError):
            await adapter.dispatch("cancelled_tool", {}, make_ctx())

    asyncio.run(go())


def test_tool_error_envelope_does_not_leak_sensitive_substrings() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="leaky_tool", handler=_leaky_handler))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> dict[str, object]:
        return await adapter.dispatch("leaky_tool", {}, make_ctx())

    result = asyncio.run(go())

    # error_kind and tool_name preserved (both come from constrained domains).
    assert result["error_kind"] == "ConnectionError"
    assert result["tool_name"] == "leaky_tool"

    message = str(result["message"])
    cause = str(result["cause"])

    forbidden = ["/Users/", ".ssh", "id_rsa", "admin", "10.0.0.5", "Bearer xyz"]
    for needle in forbidden:
        assert needle not in message, f"message leaked {needle!r}: {message!r}"
        assert needle not in cause, f"cause leaked {needle!r}: {cause!r}"
