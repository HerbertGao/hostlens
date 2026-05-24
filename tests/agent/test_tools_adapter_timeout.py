"""Tests for `ToolsAdapter.dispatch` timeout path per spec
§需求:ToolsAdapter.dispatch §场景:handler 超时被 asyncio.wait_for 取消.

When `spec.timeout` is set and the handler exceeds it, `asyncio.wait_for`
raises `asyncio.TimeoutError`. The adapter MUST wrap that into the
`tool_error` envelope (TimeoutError is neither `ToolPolicyViolation` nor
`KeyError`) and return — NOT raise. The cancel must be effective: total
wall time stays well under the handler's nominal sleep duration.
"""

from __future__ import annotations

import asyncio
import time

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import EmptyOutput, ctx_factory, make_ctx, make_spec

# Module-level marker set by `_slow_handler` when it observes the
# `asyncio.CancelledError` raised by `asyncio.wait_for` on timeout. The
# main test asserts this Event becomes set, which is the only way to
# prove cancellation actually propagated into the handler frame (vs.
# `wait_for` returning early while the handler kept sleeping in the
# background).
_cancel_observed: asyncio.Event | None = None


async def _slow_handler(args: object, ctx: ToolContext) -> EmptyOutput:
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        if _cancel_observed is not None:
            _cancel_observed.set()
        raise
    return EmptyOutput()


def test_dispatch_timeout_returns_tool_error_envelope() -> None:
    global _cancel_observed

    reg = ToolRegistry()
    reg.register(make_spec(name="slow_tool", timeout=0.5, handler=_slow_handler))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> tuple[dict[str, object], bool]:
        # Allocate the Event inside the running loop so it binds to the
        # correct event loop instance.
        global _cancel_observed
        _cancel_observed = asyncio.Event()
        result = await adapter.dispatch("slow_tool", {}, make_ctx())
        return result, _cancel_observed.is_set()

    start = time.monotonic()
    result, cancel_observed = asyncio.run(go())
    elapsed = time.monotonic() - start

    assert result["is_error"] is True
    assert result["error_kind"] == "TimeoutError"
    assert result["tool_name"] == "slow_tool"
    assert "message" in result
    assert "cause" in result
    # asyncio.wait_for must have actually cancelled the sleep; should be
    # nowhere near the 5s nominal sleep.
    assert elapsed < 2.0, f"asyncio.wait_for did not cancel; elapsed={elapsed:.2f}s"
    # And the handler frame must have actually received the CancelledError
    # — proving `wait_for` did not just return early while the handler
    # continued sleeping in the background.
    assert cancel_observed, "handler did not observe asyncio.CancelledError"
