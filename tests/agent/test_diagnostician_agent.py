"""Tests for ``DiagnosticianAgent`` + orchestration helpers (group C, §4).

Covers:

- 4.1 prompt discipline degraded failure mode: an authored same-turn forward
  reference (``request_more_inspection`` + a ``correlate_findings`` that cites the
  not-yet-returned label) is rejected by the handler, fed back, and — when the
  model never converges — terminates ``degraded_max_turns`` with empty
  hypotheses. (Whether a *real* model obeys the "split补查 and reference across
  turns" instruction is a live smoke observation, NOT a CI assertion — see
  tasks 4.1(b).)
- 4.2 ``DiagnosticianAgent.run`` puts findings in messages, never in system;
  observer pass-through; backend reaches only the loop.
- 4.3 ``harvest_hypotheses`` resolves labels → real ids at harvest time.
- 4.4 ``reconcile_status`` exhaustive mapping + ``run_diagnosis`` zero-invocation
  on Planner degrade.
- 4.5 prompt-cache testable invariants: system byte-stable across runs;
  single-element text-block list; no ``cache_control`` when
  ``prompt_caching=False``.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed.
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
from hostlens.agent.diagnostician import (
    DiagnosticianAgent,
    DiagnosticianResult,
    SeededFinding,
    harvest_hypotheses,
    reconcile_status,
    run_diagnosis,
)
from hostlens.agent.events import (
    LoopEvent,
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.loop import LoopResult, LoopUsage, ToolInvocation
from hostlens.agent.planner import PlannerResult
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import ConfigError
from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.correlate_findings import CorrelateFindingsInput

from ._helpers import make_ctx

_DEFAULT_CAPS = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _correlate_tool_use(
    *,
    block_id: str,
    labels: list[str],
    desc: str = "hypo",
    confidence: str = "medium",
    suggested_actions: list[str] | None = None,
) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="correlate_findings",
                input={
                    "description": desc,
                    "confidence": confidence,
                    "supporting_findings": labels,
                    "suggested_actions": suggested_actions or [],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _has_cjk(text: str) -> bool:
    """True iff ``text`` contains at least one CJK Unified Ideograph.

    Used to assert root-cause narratives are Simplified Chinese (spec §需求:
    诊断根因叙述 必须用简体中文) without depending on a full language detector.
    """
    return any("一" <= ch <= "鿿" for ch in text)


def _stamped(message: str, severity: str = "warning") -> Finding:
    fid = compute_finding_id("linux.load", "1.0.0", message)
    return Finding(
        severity=cast(Any, severity),
        message=message,
        id=fid,
        inspector_name="linux.load",
        inspector_version="1.0.0",
    )


def _correlate_only_registry(finding_store: FindingStore) -> ToolRegistry:
    """A registry holding just ``correlate_findings`` (closure-bound store).

    Builds the spec via the real ``_build_correlate_findings_spec`` so handler
    behaviour (hit-check + error envelope) matches production.
    """
    from hostlens.tools.diagnostician_tools import _build_correlate_findings_spec

    reg = ToolRegistry()
    reg.register(_build_correlate_findings_spec(finding_store))
    return reg


def _agent(
    backend: LLMBackend, registry: ToolRegistry, settings: Settings | None = None
) -> DiagnosticianAgent:
    return DiagnosticianAgent(backend, registry, settings or _settings(), make_ctx)


def _loop_result(
    terminal: str = "ok", *, invocations: list[ToolInvocation] | None = None
) -> LoopResult:
    return LoopResult(
        final_text="n",
        tool_invocations=invocations or [],
        turns=1,
        terminal_status=cast(Any, terminal),
        usage_totals=LoopUsage(),
        stop_reason="end_turn",
    )


def _planner_result(terminal: str = "ok", intent: str = "why slow") -> PlannerResult:
    return PlannerResult(
        narrative="planner narrative",
        findings=[Finding(severity="warning", message="raw")],
        loop_result=_loop_result(terminal),
        intent=intent,
    )


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[LoopEvent] = []

    def on_event(self, event: LoopEvent) -> None:
        self.events.append(event)


# ===========================================================================
# 4.1 — prompt discipline non-convergence is a measurable degraded failure mode
# ===========================================================================


async def test_persistent_dangling_label_never_converges_degrades_max_turns() -> None:
    """Authored persistent non-convergence: every turn the model cites a label
    (``F9``) that is never in the store.

    The handler rejects the dangling ``correlate_findings`` (error envelope fed
    back), the loop keeps spending turns, and at the turn cap it terminates
    ``degraded_max_turns`` with NO harvestable hypotheses (the failing
    invocations carry ``error``, not ``output``). This is the testable verdict of
    a model that obstinately references a non-existent label every turn and never
    converges (the "persistent dangling reference" failure mode); the separate
    same-turn forward-reference race is covered by
    ``test_same_turn_forward_reference_dangles_then_converges_next_turn``.
    """
    store = FindingStore()
    store.seed([_stamped("seed finding")])  # F1 exists; F9 never will.
    registry = _correlate_only_registry(store)

    # Two turns, each citing the non-existent label "F9" → dangling every turn.
    backend = FakeBackend(
        responses=[
            _correlate_tool_use(block_id="tu_1", labels=["F9"]),
            _correlate_tool_use(block_id="tu_2", labels=["F9"]),
            _end_turn("never reached"),  # guard fires before this is consumed
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry, _settings(max_turns=2)).run(
        "diagnose", [SeededFinding(label="F1", finding=_stamped("seed finding"))]
    )

    assert loop_result.terminal_status == "degraded_max_turns"
    # Both correlate invocations were rejected (dangling) → error set, no output.
    correlate_invs = [
        inv for inv in loop_result.tool_invocations if inv.tool_name == "correlate_findings"
    ]
    assert len(correlate_invs) == 2
    assert all(inv.output is None and inv.error is not None for inv in correlate_invs)
    # Harvest yields nothing — hypotheses are all lost on this degraded path.
    assert harvest_hypotheses(loop_result, store) == []


def _request_then_correlate_turn(
    *,
    req_block_id: str,
    corr_block_id: str,
    inspector_name: str,
    labels: list[str],
    desc: str = "hypo",
) -> MessageResponse:
    """One turn whose content holds TWO tool_use blocks dispatched concurrently:
    a ``request_more_inspection`` (will append a new finding) AND a
    ``correlate_findings`` that references a label that block has not returned
    yet (the same-turn forward reference)."""
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=req_block_id,
                name="request_more_inspection",
                input={"inspector_name": inspector_name},
            ),
            ToolUseBlock(
                type="tool_use",
                id=corr_block_id,
                name="correlate_findings",
                input={
                    "description": desc,
                    "confidence": "medium",
                    "supporting_findings": labels,
                    "suggested_actions": [],
                },
            ),
        ],
        stop_reason="tool_use",
    )


def _diagnostician_registry_with_real_tools(
    finding_store: FindingStore, *, target_name: str
) -> tuple[ToolRegistry, Any]:
    """Build a real three-tool Diagnostician registry + a matching ToolContext
    factory wired to a real ``LocalTarget`` and a finding-producing inspector.

    Mirrors the construction in ``tests/tools/test_diagnostician_tools.py``: a
    clock-free inspector manifest whose finding rule fires on ``echo hello`` so
    ``request_more_inspection`` actually collects + appends a finding (no mocked
    dispatch).
    """
    import asyncio as _asyncio

    import structlog as _structlog

    from hostlens.inspectors.registry import build_registry_from_search_paths
    from hostlens.inspectors.schema import (
        CollectSpec,
        FindingRule,
        InspectorManifest,
        ParseSpec,
    )
    from hostlens.targets.config import LocalEntry
    from hostlens.targets.local import LocalTarget
    from hostlens.targets.registry import TargetRegistry
    from hostlens.tools.base import NoopApprovalService, ToolContext
    from hostlens.tools.diagnostician_tools import register_diagnostician_tools

    inspector_registry = build_registry_from_search_paths([], settings=Settings()).registry
    manifest = InspectorManifest.model_construct(
        name="echo.finder",
        version="9.9.9",
        description="emits one finding",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        parameters=None,
        secrets=[],
        collect=CollectSpec(command="echo hello", timeout_seconds=5),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[FindingRule(when="raw != ''", severity="warning", message="saw output: {raw}")],
    )
    inspector_registry.register(manifest, source_path=None)

    target_registry = TargetRegistry()
    target_registry.register(
        cast("Any", LocalTarget(name=target_name)),
        LocalEntry(name=target_name, type="local", enabled=True),
    )

    registry = ToolRegistry()
    register_diagnostician_tools(
        registry, finding_store=finding_store, target_name=target_name, clock=None
    )

    def _ctx_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=cast(Any, _structlog.get_logger("test_diag_concurrent")),
            approval_service=NoopApprovalService(),
            cancel=_asyncio.Event(),
        )

    return registry, _ctx_factory


async def test_same_turn_forward_reference_dangles_then_converges_next_turn() -> None:
    """The genuine D-8 same-turn race: in ONE turn the model emits a
    ``request_more_inspection`` (which appends F2) AND a ``correlate_findings``
    citing the not-yet-returned ``F2``.

    Under the loop's parallel dispatch the ``correlate_findings`` handler's
    ``contains("F2")`` hit-check resolves BEFORE ``request_more_inspection`` has
    appended F2, so the same-turn correlate is rejected (error envelope). On the
    NEXT turn — F2 now in the store — the re-issued correlate converges into one
    harvestable hypothesis whose ``supporting_findings`` carries F2's real id.

    Runs through the real ``DiagnosticianAgent.run`` + real ``ToolsAdapter``
    dispatch (no mocked dispatch), with a real ``LocalTarget`` + a real
    finding-producing inspector.
    """
    store = FindingStore()
    store.seed([_stamped("seed finding")])  # F1; request_more_inspection appends F2.
    registry, ctx_factory = _diagnostician_registry_with_real_tools(store, target_name="local-host")

    backend = FakeBackend(
        responses=[
            # Turn 1: same-turn forward reference — correlate cites F2 before
            # request_more_inspection has returned it.
            _request_then_correlate_turn(
                req_block_id="tu_req",
                corr_block_id="tu_corr_early",
                inspector_name="echo.finder",
                labels=["F2"],
                desc="cascade",
            ),
            # Turn 2: F2 is now in the store → correlate converges.
            _correlate_tool_use(block_id="tu_corr_ok", labels=["F2"], desc="cascade"),
            _end_turn("diagnosis converged"),
        ]
    )

    agent = DiagnosticianAgent(cast(LLMBackend, backend), registry, _settings(), ctx_factory)
    loop_result = await agent.run(
        "diagnose", [SeededFinding(label="F1", finding=_stamped("seed finding"))]
    )

    # The same-turn correlate dangled (F2 not yet appended at hit-check time).
    early = next(
        inv
        for inv in loop_result.tool_invocations
        if inv.tool_name == "correlate_findings" and inv.tool_use_id == "tu_corr_early"
    )
    assert early.output is None
    assert early.error is not None
    assert early.error["error_kind"] == "ToolError"
    assert "dangling_finding_label" in early.error["message"]

    # request_more_inspection appended F2 with a real id (resolvable in store).
    f2_real_id = store.resolve_label("F2")
    assert f2_real_id is not None

    # The next-turn correlate converged → exactly one harvestable hypothesis,
    # carrying F2's resolved real id.
    hypotheses = harvest_hypotheses(loop_result, store)
    assert len(hypotheses) == 1
    assert hypotheses[0].supporting_findings == [f2_real_id]


# ===========================================================================
# 4.2 — findings in messages not system; observer pass-through
# ===========================================================================


async def test_findings_land_in_messages_never_in_system() -> None:
    store = FindingStore()
    registry = _correlate_only_registry(store)

    captured: dict[str, Any] = {}

    class _CapturingBackend:
        name = "capturing"
        capabilities = _DEFAULT_CAPS

        async def messages_create(
            self,
            *,
            model: str,
            system: list[dict[str, Any]] | str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            max_tokens: int,
            timeout: float,
        ) -> MessageResponse:
            captured["system"] = system
            captured["messages"] = messages
            return _end_turn("done")

    labeled = [SeededFinding(label="F1", finding=_stamped("disk almost full"))]
    await _agent(cast(LLMBackend, _CapturingBackend()), registry).run("why slow", labeled)

    system_text = captured["system"][0]["text"]
    assert "disk almost full" not in system_text  # finding NOT in system
    # The finding label + message ARE in the first user message.
    first_user = captured["messages"][0]["content"]
    assert "F1" in first_user
    assert "disk almost full" in first_user
    assert "why slow" in first_user


async def test_observer_passed_through_to_loop() -> None:
    store = FindingStore()
    store.seed([_stamped("f")])
    registry = _correlate_only_registry(store)
    backend = FakeBackend(
        responses=[
            _correlate_tool_use(block_id="tu_1", labels=["F1"]),
            _end_turn("diagnosis done"),
        ]
    )
    rec = _RecordingObserver()

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "diagnose", [SeededFinding(label="F1", finding=_stamped("f"))], observer=rec
    )

    assert [type(e) for e in rec.events] == [
        TurnStarted,
        ModelResponded,
        ToolStarted,
        ToolCompleted,
        TurnStarted,
        ModelResponded,
        RunFinalized,
    ]
    final = rec.events[-1]
    assert isinstance(final, RunFinalized)
    assert final.terminal_status == loop_result.terminal_status


# ===========================================================================
# 4.3 — harvest resolves labels → real ids
# ===========================================================================


async def test_harvest_two_hypotheses_with_resolved_real_ids() -> None:
    store = FindingStore()
    f1 = _stamped("high load")
    f2 = _stamped("oom killer fired")
    labels = store.seed([f1, f2])  # F1, F2
    assert labels == ["F1", "F2"]
    registry = _correlate_only_registry(store)

    backend = FakeBackend(
        responses=[
            _correlate_tool_use(block_id="tu_1", labels=["F1"], desc="load spike"),
            _correlate_tool_use(block_id="tu_2", labels=["F1", "F2"], desc="cascade"),
            _end_turn("two hypotheses recorded"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "diagnose",
        [SeededFinding(label="F1", finding=f1), SeededFinding(label="F2", finding=f2)],
    )

    hypotheses = harvest_hypotheses(loop_result, store)
    assert len(hypotheses) == 2
    assert hypotheses[0].description == "load spike"
    assert hypotheses[1].description == "cascade"
    # supporting_findings are REAL ids resolved at harvest, not labels.
    assert hypotheses[0].supporting_findings == [f1.id]
    assert hypotheses[1].supporting_findings == [f1.id, f2.id]
    # Explicitly assert they are real 16-hex ids, never the ordinal labels.
    label_set = {"F1", "F2"}
    for h in hypotheses:
        for ref in h.supporting_findings:
            assert ref not in label_set
            assert len(ref) == 16 and all(c in "0123456789abcdef" for c in ref)


async def test_chinese_root_cause_narrative_flows_through_to_hypothesis() -> None:
    """A Chinese ``description`` + ``suggested_actions`` produced by the model
    (replayed via ``FakeBackend``) survives the full plumbing and lands on the
    harvested ``RootCauseHypothesis`` as Simplified Chinese, while ``confidence``
    stays a valid enum value (spec §需求:诊断根因叙述 必须用简体中文).

    This exercises the system-prompt-含中文约束 + 中文输出 plumbing offline (no
    real API). The Playback cassette key excludes the system prompt, so switching
    ``diagnostician.md`` to carry the Chinese constraint never invalidates the
    existing planner/demo/diagnostician cassettes — this test pins that the
    pipeline carries Chinese narratives intact end to end.
    """
    store = FindingStore()
    f1 = _stamped("磁盘空间不足")
    store.seed([f1])  # F1
    registry = _correlate_only_registry(store)

    description = "根因:磁盘写满导致 nginx worker 无法落盘日志,进而拒绝新连接。"
    suggested_actions = [
        "清理 /var/log 下的历史日志并配置 logrotate。",
        "扩容数据盘或迁移大文件到对象存储。",
    ]
    backend = FakeBackend(
        responses=[
            _correlate_tool_use(
                block_id="tu_zh",
                labels=["F1"],
                desc=description,
                confidence="high",
                suggested_actions=suggested_actions,
            ),
            _end_turn("诊断完成:磁盘耗尽是根因。"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "为什么服务变慢", [SeededFinding(label="F1", finding=f1)]
    )

    hypotheses = harvest_hypotheses(loop_result, store)
    assert len(hypotheses) == 1
    hypo = hypotheses[0]

    # description is Simplified Chinese (carries CJK), preserved verbatim.
    assert hypo.description == description
    assert _has_cjk(hypo.description)

    # Every suggested_action is Chinese, preserved verbatim and order-stable.
    assert hypo.suggested_actions == suggested_actions
    assert all(_has_cjk(action) for action in hypo.suggested_actions)

    # confidence stays a legal enum value (NOT localized).
    assert hypo.confidence == "high"
    assert hypo.confidence in {"low", "medium", "high"}
    # supporting_findings stays a real 16-hex id, not a label.
    assert hypo.supporting_findings == [f1.id]


def test_diagnostician_prompt_mandates_simplified_chinese_narrative() -> None:
    """The shipped ``diagnostician.md`` system prompt MUST carry the explicit
    Simplified-Chinese constraint on ``description`` / ``suggested_actions``
    (spec §需求: Diagnostician 系统提示 必须显式约束输出语言为简体中文).

    Asserting on the byte-stable prompt constant keeps the language constraint a
    fixed part of the cached system prompt — not a dynamic per-report injection.
    """
    store = FindingStore()
    registry = _correlate_only_registry(store)
    backend = FakeBackend(responses=[])
    agent = _agent(cast(LLMBackend, backend), registry)

    system = agent._loop._system
    assert isinstance(system, list)
    system_text = system[0]["text"]
    assert "简体中文" in system_text
    # The constraint names the two free-text fields it governs.
    assert "description" in system_text
    assert "suggested_actions" in system_text


def test_harvest_skips_error_invocations() -> None:
    store = FindingStore()
    f1 = _stamped("f")
    store.seed([f1])
    loop_result = _loop_result(
        invocations=[
            ToolInvocation(
                tool_name="correlate_findings",
                tool_use_id="tu_err",
                input={
                    "description": "d",
                    "confidence": "low",
                    "supporting_findings": ["F1"],
                    "suggested_actions": [],
                },
                error={"is_error": True, "error_kind": "ToolError", "message": "dangling"},
            ),
        ]
    )
    assert harvest_hypotheses(loop_result, store) == []


# ===========================================================================
# 4.4 — reconcile exhaustive + run_diagnosis zero-invocation on degrade
# ===========================================================================

_PLANNER_OK_DIAG_CASES = [
    ("ok", "ok"),
    ("degraded_rate_limited", "degraded_rate_limited"),
    ("degraded_token_budget", "degraded_token_budget"),
    ("degraded_max_turns", "degraded_max_turns"),
    ("degraded_no_planner", "degraded_no_planner"),
    ("empty_response", "empty_response"),
    ("failed_api_unavailable", "degraded_no_planner"),
]


@pytest.mark.parametrize(("diag_status", "expected"), _PLANNER_OK_DIAG_CASES)
def test_reconcile_planner_ok_covers_all_seven_diag_values(diag_status: str, expected: str) -> None:
    result = reconcile_status("ok", cast(Any, diag_status))
    assert result.value == expected


_PLANNER_DEGRADED_CASES = [
    "degraded_rate_limited",
    "degraded_token_budget",
    "degraded_max_turns",
    "degraded_no_planner",
    "empty_response",
]


@pytest.mark.parametrize("planner_status", _PLANNER_DEGRADED_CASES)
def test_reconcile_planner_degraded_passes_through(planner_status: str) -> None:
    # Diagnosis skipped → diag_status None → planner value passes through.
    result = reconcile_status(cast(Any, planner_status), None)
    assert result.value == planner_status


def test_reconcile_planner_failed_api_unavailable_raises() -> None:
    with pytest.raises(ValueError, match="no DiagnosticianResult"):
        reconcile_status("failed_api_unavailable", None)


def test_reconcile_never_produces_partial() -> None:
    produced = {reconcile_status("ok", cast(Any, d)).value for _, d in _PLANNER_OK_DIAG_CASES}
    produced |= {reconcile_status(cast(Any, p), None).value for p in _PLANNER_DEGRADED_CASES}
    assert "partial" not in produced


@pytest.mark.parametrize("planner_status", _PLANNER_DEGRADED_CASES)
async def test_run_diagnosis_planner_degraded_zero_agent_invocation(planner_status: str) -> None:
    """Planner degraded → the agent FACTORY is never called (no agent is even
    constructed, let alone run) for every one of the five degraded values."""
    factory_calls = {"n": 0}

    def _never_factory() -> DiagnosticianAgent:
        factory_calls["n"] += 1
        raise AssertionError("diagnostician_agent_factory must not be called on Planner degrade")

    planner = _planner_result(planner_status)
    store = FindingStore()
    f1 = _stamped("kept finding")
    store.seed([f1])

    result = await run_diagnosis(
        planner,
        [SeededFinding(label="F1", finding=f1)],
        store,
        _never_factory,
    )

    assert factory_calls["n"] == 0
    assert result.diagnostician_loop is None
    # Status passes through verbatim.
    assert result.status.value == planner_status
    assert result.hypotheses == []
    # Planner findings are kept.
    assert [f.message for f in result.findings] == ["kept finding"]


async def test_run_diagnosis_planner_ok_runs_and_harvests() -> None:
    planner = _planner_result("ok")
    store = FindingStore()
    f1 = _stamped("seed")
    store.seed([f1])
    registry = _correlate_only_registry(store)
    backend = FakeBackend(
        responses=[
            _correlate_tool_use(block_id="tu_1", labels=["F1"], desc="root cause"),
            _end_turn("diagnosis complete"),
        ]
    )
    agent = _agent(cast(LLMBackend, backend), registry)

    result = await run_diagnosis(
        planner, [SeededFinding(label="F1", finding=f1)], store, lambda: agent
    )

    assert isinstance(result, DiagnosticianResult)
    assert result.diagnostician_loop is not None
    assert result.status.value == "ok"
    assert result.narrative == "diagnosis complete"
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].supporting_findings == [f1.id]
    # Canonical findings come from the store snapshot.
    assert [f.message for f in result.findings] == ["seed"]


# ===========================================================================
# 4.5 — prompt-cache testable invariants
# ===========================================================================


async def test_system_prompt_byte_stable_and_single_text_block_list() -> None:
    store = FindingStore()
    registry = _correlate_only_registry(store)
    backend = FakeBackend(responses=[])

    a1 = DiagnosticianAgent(cast(LLMBackend, backend), registry, _settings(), make_ctx)
    a2 = DiagnosticianAgent(cast(LLMBackend, backend), registry, _settings(), make_ctx)

    sys1 = a1._loop._system
    sys2 = a2._loop._system

    assert isinstance(sys1, list)
    assert len(sys1) == 1
    assert sys1[0]["type"] == "text"
    assert isinstance(sys1[0]["text"], str)
    assert sys1[0]["text"]
    # Byte-stable across constructions (the findings differ per-run but never
    # touch the system prompt).
    assert sys1 == sys2


async def test_system_block_byte_identical_across_runs_with_different_inputs() -> None:
    """Two runs with different intent/findings → identical system block sent.

    This is the prompt-cache prerequisite from the consumer's angle: the system
    block the backend receives must not drift when the dynamic findings/intent
    change (those belong in messages — design D-10).
    """
    captured_systems: list[Any] = []

    class _CapturingBackend:
        name = "capturing"
        capabilities = _DEFAULT_CAPS

        async def messages_create(
            self,
            *,
            model: str,
            system: list[dict[str, Any]] | str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            max_tokens: int,
            timeout: float,
        ) -> MessageResponse:
            captured_systems.append(system)
            return _end_turn("done")

    store_a = FindingStore()
    store_a.seed([_stamped("alpha")])
    registry_a = _correlate_only_registry(store_a)
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_a).run(
        "intent A", [SeededFinding(label="F1", finding=_stamped("alpha"))]
    )

    store_b = FindingStore()
    store_b.seed([_stamped("beta")])
    registry_b = _correlate_only_registry(store_b)
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_b).run(
        "totally different intent B", [SeededFinding(label="F1", finding=_stamped("beta"))]
    )

    assert len(captured_systems) == 2
    # Same system bytes despite different intent + findings (findings stayed in
    # messages, system carries only the byte-stable rendered prompt + tools).
    assert captured_systems[0] == captured_systems[1]


async def test_no_cache_control_when_prompt_caching_disabled() -> None:
    store = FindingStore()
    store.seed([_stamped("f")])
    registry = _correlate_only_registry(store)

    no_cache_caps = BackendCapabilities(
        prompt_caching=False,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )

    captured: dict[str, Any] = {}

    class _NoCacheBackend:
        name = "nocache"
        capabilities = no_cache_caps

        async def messages_create(
            self,
            *,
            model: str,
            system: list[dict[str, Any]] | str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            max_tokens: int,
            timeout: float,
        ) -> MessageResponse:
            captured["system"] = system
            captured["messages"] = messages
            captured["tools"] = tools
            return _end_turn("done")

    await _agent(cast(LLMBackend, _NoCacheBackend()), registry).run(
        "diagnose", [SeededFinding(label="F1", finding=_stamped("f"))]
    )

    # No cache_control anywhere — the loop must not inject it when the backend
    # declares prompt_caching=False (CLAUDE.md §4.8).
    assert all("cache_control" not in b for b in captured["system"])
    for message in captured["messages"]:
        content = message["content"]
        if isinstance(content, list):
            assert all("cache_control" not in b for b in content)
    assert all("cache_control" not in t for t in captured["tools"])


# ---------------------------------------------------------------------------
# Construction failure: missing prompt template
# ---------------------------------------------------------------------------


def test_missing_prompt_template_raises_config_error(tmp_path: Any) -> None:
    store = FindingStore()
    registry = _correlate_only_registry(store)
    backend = FakeBackend(responses=[])
    missing = tmp_path / "nope.md"

    with pytest.raises(ConfigError) as exc_info:
        DiagnosticianAgent(
            cast(LLMBackend, backend),
            registry,
            _settings(),
            make_ctx,
            prompt_path=str(missing),
        )
    assert exc_info.value.kind == "diagnostician_prompt_missing"


def test_correlate_findings_input_schema_alignment() -> None:
    """Sanity: harvest reads exactly the CorrelateFindingsInput field shape."""
    inp = CorrelateFindingsInput(
        description="d", confidence="high", supporting_findings=["F1"], suggested_actions=["a"]
    )
    assert inp.description == "d"
    assert inp.confidence == "high"
