"""Observer-event tests for ``hostlens.agent.loop.AgentLoop`` (add-intent-cli §5.1).

The loop's typed ``LoopEvent`` stream is the M2.7 UI observation surface
(design D-1/D-2/D-3). These tests pin the spec's event-ordering and fail-loud
contracts using a plain in-process ``RecordingObserver`` (append events, never
raise) over scripted ``FakeBackend`` / ``_ScriptedBackend`` responses — no Rich,
no API quota.

Key contracts pinned here:
  * single-tool turn emits the full ordered sequence;
  * ``ToolCompleted.invocation`` is the SAME record as in ``LoopResult``;
  * parallel multi-tool turns guarantee only a partial order (per-block
    Started→Completed, correlate by ``tool_use_id`` — never assume cross-block
    total order);
  * hallucinated tool names still emit a paired Started/Completed (error set);
  * ``observer=None`` is a true no-op (no regression);
  * the loop is fail-loud: a raising observer propagates (no defensive
    try/except), and a fail-loud tool path (``ToolPolicyViolation``) emits
    ``ToolStarted`` but NO paired ``ToolCompleted`` and NO ``RunFinalized``.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed; no
``@pytest.mark.live`` (every backend is fake).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from hostlens.agent.backend import (
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.events import (
    LoopEvent,
    LoopObserver,
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.loop import AgentLoop
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import EmptyOutput, ctx_factory, make_spec, ok_handler

# ---------------------------------------------------------------------------
# Observers + builders
# ---------------------------------------------------------------------------


class RecordingObserver:
    """Appends every event; never raises. Structurally a ``LoopObserver``."""

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []

    def on_event(self, event: LoopEvent) -> None:
        self.events.append(event)


class RaisingObserver:
    """``on_event`` always raises — used to assert the loop never swallows it."""

    def on_event(self, event: LoopEvent) -> None:
        raise RuntimeError("boom")


def _settings(**agent_kwargs: Any) -> Settings:
    return Settings(agent=AgentSettings(**agent_kwargs))


def _msg(
    *,
    content: list[Any],
    stop_reason: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def _tool_use(*, block_id: str, name: str, tool_input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=block_id, name=name, input=tool_input)


def _adapter_with(*specs: Any) -> ToolsAdapter:
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return ToolsAdapter(reg, ctx_factory())


def _empty_adapter() -> ToolsAdapter:
    return ToolsAdapter(ToolRegistry(), ctx_factory())


def _loop(
    backend: FakeBackend,
    adapter: ToolsAdapter,
    settings: Settings,
) -> AgentLoop:
    return AgentLoop(cast(LLMBackend, backend), adapter, settings)


async def _ok_tool_handler(args: Any, ctx: ToolContext) -> EmptyOutput:
    return EmptyOutput()


# ---------------------------------------------------------------------------
# Structural Protocol sanity (RecordingObserver IS a LoopObserver)
# ---------------------------------------------------------------------------


def test_recording_observer_satisfies_protocol() -> None:
    assert isinstance(RecordingObserver(), LoopObserver)


# ---------------------------------------------------------------------------
# ① single tool: full ordered event sequence + terminal_status agreement
# ---------------------------------------------------------------------------


async def test_single_tool_full_ordered_event_sequence() -> None:
    spec = make_spec(name="list_inspectors", handler=ok_handler)
    backend = FakeBackend(
        responses=[
            _msg(
                content=[_tool_use(block_id="toolu_1", name="list_inspectors", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("all good")], stop_reason="end_turn"),
        ]
    )
    obs = RecordingObserver()
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("list inspectors", observer=obs)

    types = [type(e) for e in obs.events]
    assert types == [
        TurnStarted,
        ModelResponded,
        ToolStarted,
        ToolCompleted,
        TurnStarted,
        ModelResponded,
        RunFinalized,
    ]
    final = obs.events[-1]
    assert isinstance(final, RunFinalized)
    assert final.terminal_status == result.terminal_status
    assert final.terminal_status == "ok"
    # Turn numbering: first turn is the tool_use turn, second is the end_turn.
    started = [e for e in obs.events if isinstance(e, TurnStarted)]
    assert [e.turn for e in started] == [1, 2]


# ---------------------------------------------------------------------------
# ② ToolCompleted.invocation is the SAME record as in LoopResult
# ---------------------------------------------------------------------------


async def test_tool_completed_invocation_matches_loop_result() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = FakeBackend(
        responses=[
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    obs = RecordingObserver()
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("x", observer=obs)

    completed = [e for e in obs.events if isinstance(e, ToolCompleted)]
    assert len(completed) == 1
    assert len(result.tool_invocations) == 1
    # Identity, not just equality: the event carries the loop's own record.
    assert completed[0].invocation is result.tool_invocations[0]
    assert completed[0].turn == 1


# ---------------------------------------------------------------------------
# ③ parallel multi-tool: partial order, correlate by tool_use_id
# ---------------------------------------------------------------------------


async def test_parallel_multi_tool_partial_order_by_id() -> None:
    spec = make_spec(name="t", handler=_ok_tool_handler)
    backend = FakeBackend(
        responses=[
            _msg(
                content=[
                    _tool_use(block_id="toolu_a", name="t", tool_input={}),
                    _tool_use(block_id="toolu_b", name="t", tool_input={}),
                ],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("finished")], stop_reason="end_turn"),
        ]
    )
    obs = RecordingObserver()
    loop = _loop(backend, _adapter_with(spec), _settings())

    await loop.run("two tools", observer=obs)

    started = [e for e in obs.events if isinstance(e, ToolStarted)]
    completed = [e for e in obs.events if isinstance(e, ToolCompleted)]
    assert {e.tool_use_id for e in started} == {"toolu_a", "toolu_b"}
    assert {e.invocation.tool_use_id for e in completed} == {"toolu_a", "toolu_b"}

    # Per-block (partial) order: each block's Started precedes its Completed.
    # Do NOT assume any cross-block total order (they interleave under gather).
    for tool_use_id in ("toolu_a", "toolu_b"):
        start_idx = next(
            i
            for i, e in enumerate(obs.events)
            if isinstance(e, ToolStarted) and e.tool_use_id == tool_use_id
        )
        complete_idx = next(
            i
            for i, e in enumerate(obs.events)
            if isinstance(e, ToolCompleted) and e.invocation.tool_use_id == tool_use_id
        )
        assert start_idx < complete_idx

    # Turn-level order still holds: the turn's TurnStarted precedes all its
    # tool events, and the next turn's TurnStarted follows them.
    turn_starts = [i for i, e in enumerate(obs.events) if isinstance(e, TurnStarted)]
    first_tool_evt = next(
        i for i, e in enumerate(obs.events) if isinstance(e, ToolStarted | ToolCompleted)
    )
    assert turn_starts[0] < first_tool_evt < turn_starts[1]


# ---------------------------------------------------------------------------
# ④ hallucinated tool name still emits a paired Started/Completed (error set)
# ---------------------------------------------------------------------------


async def test_hallucinated_tool_name_emits_paired_events() -> None:
    real = make_spec(name="real_tool", handler=ok_handler)
    backend = FakeBackend(
        responses=[
            _msg(
                content=[_tool_use(block_id="toolu_1", name="ghost.tool", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    obs = RecordingObserver()
    loop = _loop(backend, _adapter_with(real), _settings())

    await loop.run("call ghost", observer=obs)

    started = [e for e in obs.events if isinstance(e, ToolStarted)]
    completed = [e for e in obs.events if isinstance(e, ToolCompleted)]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].tool_name == "ghost.tool"
    assert started[0].tool_use_id == "toolu_1"
    # Even though the loop never reaches dispatch, the block still produces an
    # error invocation, so ToolCompleted is emitted with error non-empty.
    inv = completed[0].invocation
    assert inv.tool_use_id == "toolu_1"
    assert inv.error is not None
    assert inv.output is None


# ---------------------------------------------------------------------------
# ⑤ observer=None is a true no-op (no regression vs not passing observer)
# ---------------------------------------------------------------------------


def _build_backend() -> FakeBackend:
    return FakeBackend(
        responses=[
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )


async def test_observer_none_no_regression() -> None:
    spec = make_spec(name="t", handler=ok_handler)

    # observer=None explicitly.
    loop_a = _loop(_build_backend(), _adapter_with(spec), _settings())
    result_none = await loop_a.run("x", observer=None)

    # observer kwarg omitted entirely.
    loop_b = _loop(_build_backend(), _adapter_with(spec), _settings())
    result_default = await loop_b.run("x")

    # Same condensed result regardless of how the no-op observer is supplied.
    assert result_none.model_dump() == result_default.model_dump()
    assert result_none.terminal_status == "ok"
    assert len(result_none.tool_invocations) == 1


# ---------------------------------------------------------------------------
# ⑥ loop is fail-loud: a raising observer propagates (no defensive try/except)
# ---------------------------------------------------------------------------


async def test_raising_observer_propagates() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = FakeBackend(responses=[_msg(content=[_text("done")], stop_reason="end_turn")])
    loop = _loop(backend, _adapter_with(spec), _settings())

    # The very first event (TurnStarted) triggers the raise; the loop must let
    # it propagate verbatim rather than wrap it in a defensive try/except.
    with pytest.raises(RuntimeError, match="boom"):
        await loop.run("x", observer=RaisingObserver())


# ---------------------------------------------------------------------------
# ⑦ fail-loud tool path: ToolStarted but NO ToolCompleted, NO RunFinalized
# ---------------------------------------------------------------------------


async def test_fail_loud_tool_path_no_tool_completed_no_finalize() -> None:
    # side_effects="write" is advertised to the agent (surfaces includes
    # "agent"), so the loop reaches dispatch which the M2 policy gate rejects
    # with ToolPolicyViolation. The loop does NOT catch it: ToolStarted was
    # already emitted, but no paired ToolCompleted and no RunFinalized.
    spec = make_spec(name="danger_write", side_effects="write", handler=ok_handler)
    backend = FakeBackend(
        responses=[
            _msg(
                content=[_tool_use(block_id="toolu_1", name="danger_write", tool_input={})],
                stop_reason="tool_use",
            ),
        ]
    )
    obs = RecordingObserver()
    loop = _loop(backend, _adapter_with(spec), _settings())

    with pytest.raises(ToolPolicyViolation):
        await loop.run("trigger policy", observer=obs)

    started = [e for e in obs.events if isinstance(e, ToolStarted)]
    completed = [e for e in obs.events if isinstance(e, ToolCompleted)]
    finalized = [e for e in obs.events if isinstance(e, RunFinalized)]
    assert len(started) == 1
    assert started[0].tool_name == "danger_write"
    # The fail-loud path produces neither a paired ToolCompleted nor a finalize.
    assert completed == []
    assert finalized == []
