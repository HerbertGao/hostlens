"""Tests for ``RemediationPlannerAgent`` + orchestration helpers (group C, §4).

Mirrors ``tests/agent/test_diagnostician_agent.py`` (same ``FakeBackend``
scripted-``tool_use`` discipline — CI deterministic, zero real API): swap
``correlate_findings`` for ``propose_remediation`` and "hypothesis" for "plan".

Covers:

- 4.1 finding + hypotheses → planner calls ``propose_remediation`` → harvest
  yields a ``RemediationPlan`` bound to the real id + stamped target.
- 4.2 emit-time P1a invariant violations (``high_requires_precheck`` /
  ``rollback_none_requires_high``) are rejected, fed back, loop never crashes.
- 4.3 dangling ``F9`` → handler hit-check raises ``ToolError`` (error envelope,
  never reaches harvest); harvest fails loud on a dangling label (defensive).
- 4.4 system prompt byte-stable across two input sets; no ``cache_control`` when
  ``prompt_caching=False``.
- 4.5 ``propose_remediation`` not on MCP; planner tool set all read-only +
  excludes ``correlate_findings`` / ``run_inspector``.
- 4.6 degraded terminal keeps already-harvested plans; no
  ``propose_remediation`` call → empty plan list + normal status; multi-label
  resolving to the same real id is NOT deduplicated.
- 4.7 dispatching ``propose_remediation`` never executes a command / touches a
  target.
- 5.4 offline-demo semantics: a /var/log-full finding flows through the planner
  to a printed ``RemediationPlan`` with zero command execution (P1↔P2 gate).

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
from hostlens.agent.diagnostician import SeededFinding
from hostlens.agent.loop import LoopResult, LoopUsage, ToolInvocation
from hostlens.agent.remediation_planner import (
    RemediationPlannerAgent,
    RemediationPlannerResult,
    harvest_plans,
    run_remediation_planning,
)
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import ConfigError
from hostlens.remediation.models import RemediationPlan
from hostlens.reporting.models import Finding, RootCauseHypothesis, compute_finding_id
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.remediation_planner_tools import register_remediation_planner_tools

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

_TARGET = "prod-web-01"


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


def _low_step(*, forward: str = "systemctl restart x") -> dict[str, Any]:
    """A valid low-risk step dict (rollback present, no precheck needed)."""
    return {
        "description": "restart the service",
        "precheck_cmd": None,
        "forward_cmd": forward,
        "rollback_cmd": "systemctl stop x",
        "verify_cmd": "systemctl is-active x",
        "risk_level": "low",
    }


def _propose_tool_use(
    *,
    block_id: str,
    finding_label: str,
    rationale: str = "fix it",
    steps: list[dict[str, Any]] | None = None,
    estimated_duration_seconds: int = 30,
) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="propose_remediation",
                input={
                    "finding_label": finding_label,
                    "rationale": rationale,
                    "estimated_duration_seconds": estimated_duration_seconds,
                    "steps": steps if steps is not None else [_low_step()],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _stamped(message: str, severity: str = "warning") -> Finding:
    fid = compute_finding_id("linux.disk", "1.0.0", message)
    return Finding(
        severity=cast(Any, severity),
        message=message,
        id=fid,
        inspector_name="linux.disk",
        inspector_version="1.0.0",
    )


def _hypotheses() -> list[RootCauseHypothesis]:
    return [
        RootCauseHypothesis(
            description="日志写满磁盘",
            confidence="high",
            supporting_findings=["irrelevant_real_id"],
            suggested_actions=["轮转日志"],
        )
    ]


def _propose_only_registry(finding_store: FindingStore) -> ToolRegistry:
    """A registry holding just ``propose_remediation`` (closure-bound store).

    Builds the spec via the real ``_build_propose_remediation_spec`` so handler
    behaviour (hit-check + error envelope) matches production.
    """
    from hostlens.tools.remediation_planner_tools import _build_propose_remediation_spec

    reg = ToolRegistry()
    reg.register(_build_propose_remediation_spec(finding_store))
    return reg


def _agent(
    backend: LLMBackend, registry: ToolRegistry, settings: Settings | None = None
) -> RemediationPlannerAgent:
    return RemediationPlannerAgent(backend, registry, settings or _settings(), make_ctx)


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


def _request_more_inspection_turn(*, block_id: str, inspector_name: str) -> MessageResponse:
    """One turn that calls ``request_more_inspection`` (appends a new finding)."""
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="request_more_inspection",
                input={"inspector_name": inspector_name},
            )
        ],
        stop_reason="tool_use",
    )


def _planner_registry_with_real_tools(
    finding_store: FindingStore, *, target_name: str
) -> tuple[ToolRegistry, Any]:
    """Build a real three-tool Planner registry + a matching ToolContext factory
    wired to a real ``LocalTarget`` and a finding-producing inspector.

    Mirrors ``_diagnostician_registry_with_real_tools`` in
    ``test_diagnostician_agent.py`` (clock-free inspector manifest whose finding
    rule fires on ``echo hello`` so ``request_more_inspection`` actually collects
    + appends a finding — no mocked dispatch), swapping
    ``register_diagnostician_tools`` for ``register_remediation_planner_tools``.
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
    register_remediation_planner_tools(
        registry, finding_store=finding_store, target_name=target_name, clock=None
    )

    def _ctx_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=cast(Any, _structlog.get_logger("test_planner_concurrent")),
            approval_service=NoopApprovalService(),
            cancel=_asyncio.Event(),
        )

    return registry, _ctx_factory


# ===========================================================================
# review-fix — appended-label propose reference (P1b cross-tool regression)
# ===========================================================================


async def test_appended_label_referenced_by_later_turn_propose_harvests_real_id() -> None:
    """Cross-tool review path: turn 1 ``request_more_inspection`` appends a NEW
    finding (F2) into the shared ``FindingStore``; turn 2 ``propose_remediation``
    references that appended label ``F2``.

    Proves the P1b-specific regression door: a label that did not exist when the
    planner started — produced mid-loop by ``request_more_inspection`` — is
    referenceable by a later-turn ``propose_remediation``, the shared-store wiring
    is correct, and ``harvest_plans`` resolves it to the REAL ``Finding.id`` of the
    appended finding (never the ordinal label). Runs through the real
    ``RemediationPlannerAgent.run`` + real dispatch (real ``LocalTarget`` + a real
    finding-producing inspector), no mocked dispatch.
    """
    store = FindingStore()
    f1 = _stamped("disk warming up")
    store.seed([f1])  # F1; request_more_inspection appends F2.
    registry, ctx_factory = _planner_registry_with_real_tools(store, target_name="local-host")

    backend = FakeBackend(
        responses=[
            # Turn 1: re-inspect → appends F2 (a real finding with a real id).
            _request_more_inspection_turn(block_id="tu_req", inspector_name="echo.finder"),
            # Turn 2: F2 now in the store → propose against the appended label.
            _propose_tool_use(block_id="tu_propose", finding_label="F2", rationale="fix F2"),
            _end_turn("planned against appended finding"),
        ]
    )

    agent = RemediationPlannerAgent(cast(LLMBackend, backend), registry, _settings(), ctx_factory)
    loop_result = await agent.run(
        "fix disk", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    # request_more_inspection appended F2 with a real id (resolvable in store).
    f2_real_id = store.resolve_label("F2")
    assert f2_real_id is not None
    assert f2_real_id != f1.id  # the appended finding is genuinely new

    # The propose against the appended label succeeded (ack, no error envelope).
    propose_inv = next(
        i for i in loop_result.tool_invocations if i.tool_name == "propose_remediation"
    )
    assert propose_inv.error is None
    assert propose_inv.output is not None

    # Harvest binds the appended label to its REAL id, never the ordinal label.
    plans = harvest_plans(loop_result, store, _TARGET)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.finding_id == f2_real_id
    assert plan.finding_id != "F2"
    assert len(plan.finding_id) == 16
    assert plan.target_name == _TARGET
    assert plan.rationale == "fix F2"


# ===========================================================================
# 4.1 — finding + hypotheses → propose_remediation → harvested RemediationPlan
# ===========================================================================


async def test_propose_then_harvest_binds_real_id_and_target() -> None:
    store = FindingStore()
    f1 = _stamped("/var/log 占用 95%")
    labels = store.seed([f1])  # F1
    assert labels == ["F1"]
    registry = _propose_only_registry(store)

    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_1", finding_label="F1", rationale="rotate logs"),
            _end_turn("plan proposed"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "fix disk", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    plans = harvest_plans(loop_result, store, _TARGET)
    assert len(plans) == 1
    plan = plans[0]
    assert isinstance(plan, RemediationPlan)
    # The label resolved to the REAL id (16-hex), never the ordinal label.
    assert plan.finding_id == f1.id
    assert plan.finding_id != "F1"
    assert len(plan.finding_id) == 16
    # target_name is stamped by the orchestration layer.
    assert plan.target_name == _TARGET
    assert plan.rationale == "rotate logs"
    assert len(plan.steps) == 1


async def test_findings_and_hypotheses_land_in_messages_never_in_system() -> None:
    store = FindingStore()
    f1 = _stamped("/var/log 占满")
    store.seed([f1])
    registry = _propose_only_registry(store)

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

    await _agent(cast(LLMBackend, _CapturingBackend()), registry).run(
        "为什么磁盘满了", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    system_text = captured["system"][0]["text"]
    assert "/var/log 占满" not in system_text  # finding NOT in system
    assert "日志写满磁盘" not in system_text  # hypothesis NOT in system
    assert _TARGET not in system_text  # target NOT in system
    first_user = captured["messages"][0]["content"]
    assert "F1" in first_user
    assert "/var/log 占满" in first_user
    assert "为什么磁盘满了" in first_user
    assert "日志写满磁盘" in first_user  # hypothesis IS in the first user message
    assert _TARGET in first_user  # target IS in the first user message


# ===========================================================================
# 4.2 — emit-time P1a invariant violations fed back, loop does not crash
# ===========================================================================


async def test_high_step_missing_precheck_rejected_and_fed_back() -> None:
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    registry = _propose_only_registry(store)

    bad_high_step = {
        "description": "wipe logs",
        "precheck_cmd": None,  # high without precheck → high_requires_precheck
        "forward_cmd": "rm -rf /var/log/*",
        "rollback_cmd": "echo cannot rollback",
        "verify_cmd": "ls /var/log",
        "risk_level": "high",
    }
    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_bad", finding_label="F1", steps=[bad_high_step]),
            _end_turn("model gave up after rejection"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "fix", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    inv = next(i for i in loop_result.tool_invocations if i.tool_name == "propose_remediation")
    assert inv.output is None
    assert inv.error is not None
    assert "high_requires_precheck" in inv.error["message"]
    # Loop survived the rejection and reached end_turn.
    assert loop_result.terminal_status == "ok"
    # Nothing harvestable from a rejected emit.
    assert harvest_plans(loop_result, store, _TARGET) == []


async def test_non_high_step_missing_rollback_rejected_and_fed_back() -> None:
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    registry = _propose_only_registry(store)

    bad_low_step = {
        "description": "restart",
        "precheck_cmd": None,
        "forward_cmd": "systemctl restart x",
        "rollback_cmd": None,  # low + rollback=None → rollback_none_requires_high
        "verify_cmd": "systemctl is-active x",
        "risk_level": "low",
    }
    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_bad", finding_label="F1", steps=[bad_low_step]),
            _end_turn("model gave up after rejection"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "fix", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    inv = next(i for i in loop_result.tool_invocations if i.tool_name == "propose_remediation")
    assert inv.output is None
    assert inv.error is not None
    assert "rollback_none_requires_high" in inv.error["message"]
    assert loop_result.terminal_status == "ok"
    assert harvest_plans(loop_result, store, _TARGET) == []


# ===========================================================================
# 4.3 — dangling label rejected in handler; harvest fails loud on dangling
# ===========================================================================


async def test_dangling_label_rejected_in_handler_never_reaches_harvest() -> None:
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])  # F1 exists; F9 never will.
    registry = _propose_only_registry(store)

    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_dangle", finding_label="F9"),
            _end_turn("model gave up after rejection"),
        ]
    )

    loop_result = await _agent(cast(LLMBackend, backend), registry).run(
        "fix", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    inv = next(i for i in loop_result.tool_invocations if i.tool_name == "propose_remediation")
    assert inv.output is None
    assert inv.error is not None
    assert inv.error["error_kind"] == "ToolError"
    assert "dangling_finding_label" in inv.error["message"]
    # The dangling reference is an error envelope — never a successful
    # invocation — so harvest produces nothing (it never reaches harvest).
    assert harvest_plans(loop_result, store, _TARGET) == []


def test_harvest_fails_loud_on_dangling_label_defensive() -> None:
    """Defensive (theoretically unreachable) path: a successful
    ``propose_remediation`` invocation whose label is absent from the store must
    make ``harvest_plans`` raise, NOT silently skip — mirrors
    ``harvest_hypotheses`` fail-loud (spec §场景:harvest 对悬空标签 fail-loud).
    """
    store = FindingStore()
    store.seed([_stamped("disk full")])  # F1 only; F9 absent.
    loop_result = _loop_result(
        invocations=[
            ToolInvocation(
                tool_name="propose_remediation",
                tool_use_id="tu_ghost",
                input={
                    "finding_label": "F9",  # absent from store
                    "rationale": "r",
                    "estimated_duration_seconds": 5,
                    "steps": [_low_step()],
                },
                output={"accepted": True, "echoed_label": "F9"},
            ),
        ]
    )
    with pytest.raises(ValueError, match="absent from the finding-store"):
        harvest_plans(loop_result, store, _TARGET)


# ===========================================================================
# 4.4 — prompt-cache testable invariants
# ===========================================================================


async def test_system_prompt_byte_stable_across_different_inputs() -> None:
    """Two runs with different findings/hypotheses/target → identical system
    block sent (the prompt-cache prerequisite — dynamic inputs live in messages).
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
    fa = _stamped("alpha")
    store_a.seed([fa])
    registry_a = _propose_only_registry(store_a)
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_a).run(
        "intent A", [SeededFinding(label="F1", finding=fa)], _hypotheses(), "target-a"
    )

    store_b = FindingStore()
    fb = _stamped("beta")
    store_b.seed([fb])
    registry_b = _propose_only_registry(store_b)
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_b).run(
        "totally different intent B", [SeededFinding(label="F1", finding=fb)], [], "target-b"
    )

    assert len(captured_systems) == 2
    assert captured_systems[0] == captured_systems[1]
    # Single-element text-block list (so the loop can inject cache_control).
    assert isinstance(captured_systems[0], list)
    assert len(captured_systems[0]) == 1
    assert captured_systems[0][0]["type"] == "text"


async def test_system_prompt_byte_stable_full_three_tool_registry() -> None:
    """Like ``test_system_prompt_byte_stable_across_different_inputs`` but over the
    PRODUCTION three-tool set (``register_remediation_planner_tools``:
    request_more_inspection + list_inspectors + propose_remediation) rather than
    the single-tool ``_propose_only_registry``.

    Covers the byte-stability of the real planner tool overview — including the
    planner ``request_more_inspection`` ``agent_description`` variant — so a drift
    in that production copy (which the single-tool test cannot see) breaks here.
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
    fa = _stamped("alpha")
    store_a.seed([fa])
    registry_a = ToolRegistry()
    register_remediation_planner_tools(registry_a, finding_store=store_a, target_name="target-a")
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_a).run(
        "intent A", [SeededFinding(label="F1", finding=fa)], _hypotheses(), "target-a"
    )

    store_b = FindingStore()
    fb = _stamped("beta")
    store_b.seed([fb])
    registry_b = ToolRegistry()
    register_remediation_planner_tools(registry_b, finding_store=store_b, target_name="target-b")
    await _agent(cast(LLMBackend, _CapturingBackend()), registry_b).run(
        "totally different intent B", [SeededFinding(label="F1", finding=fb)], [], "target-b"
    )

    assert len(captured_systems) == 2
    assert captured_systems[0] == captured_systems[1]
    # Single-element text-block list (so the loop can inject cache_control).
    assert isinstance(captured_systems[0], list)
    assert len(captured_systems[0]) == 1
    assert captured_systems[0][0]["type"] == "text"


async def test_no_cache_control_when_prompt_caching_disabled() -> None:
    store = FindingStore()
    f1 = _stamped("f")
    store.seed([f1])
    registry = _propose_only_registry(store)

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
        "fix", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    assert all("cache_control" not in b for b in captured["system"])
    for message in captured["messages"]:
        content = message["content"]
        if isinstance(content, list):
            assert all("cache_control" not in b for b in content)
    assert all("cache_control" not in t for t in captured["tools"])


# ===========================================================================
# 4.5 — propose_remediation not on MCP; planner tool set all read-only
# ===========================================================================


def test_propose_remediation_not_projected_to_mcp() -> None:
    from hostlens.mcp_server.tools_adapter import McpToolsAdapter

    store = FindingStore()
    registry = ToolRegistry()
    register_remediation_planner_tools(registry, finding_store=store, target_name=_TARGET)

    adapter = McpToolsAdapter(registry, make_ctx)
    mcp_tool_names = {tool.name for tool in adapter.list_for_mcp()}
    assert "propose_remediation" not in mcp_tool_names


def test_planner_tool_set_is_read_only_and_excludes_correlate_and_run_inspector() -> None:
    store = FindingStore()
    registry = ToolRegistry()
    register_remediation_planner_tools(registry, finding_store=store, target_name=_TARGET)

    specs = registry.list_for("agent")
    names = {spec.name for spec in specs}
    assert names == {"propose_remediation", "request_more_inspection", "list_inspectors"}
    # No write/destructive, no approval-gated tool.
    for spec in specs:
        assert spec.side_effects in {"none", "read"}
        assert spec.requires_approval is False
    # Explicit exclusions (planner produces plans, not hypotheses; never touches
    # the target name directly).
    assert "correlate_findings" not in names
    assert "run_inspector" not in names


def test_propose_remediation_not_in_default_tools() -> None:
    from hostlens.tools.default_tools import register_default_tools

    registry = ToolRegistry()
    register_default_tools(registry)
    names = {spec.name for spec in registry.list_for("agent")}
    assert "propose_remediation" not in names


# ===========================================================================
# 4.6 — degraded keeps plans; no call → empty; multi-label same id no dedup
# ===========================================================================


async def test_degraded_terminal_keeps_harvested_plans() -> None:
    """Planner loop hits the turn cap after one successful propose → the
    degraded status passes through but the harvested plan is preserved.
    """
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    registry = _propose_only_registry(store)

    # max_turns=1: one tool-use turn, then the cap fires (degraded_max_turns).
    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_1", finding_label="F1"),
            _propose_tool_use(block_id="tu_2", finding_label="F1"),
        ]
    )

    result = await run_remediation_planning(
        "ok",
        [SeededFinding(label="F1", finding=f1)],
        store,
        _TARGET,
        "fix disk",
        lambda: _agent(cast(LLMBackend, backend), registry, _settings(max_turns=1)),
        _hypotheses(),
    )

    assert isinstance(result, RemediationPlannerResult)
    assert result.status == "degraded_max_turns"
    # The plan harvested before the cap fired is preserved.
    assert len(result.plans) == 1
    assert result.plans[0].finding_id == f1.id


async def test_no_propose_call_yields_empty_plans_normal_status() -> None:
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    registry = _propose_only_registry(store)

    backend = FakeBackend(responses=[_end_turn("nothing to fix")])

    result = await run_remediation_planning(
        "ok",
        [SeededFinding(label="F1", finding=f1)],
        store,
        _TARGET,
        "fix disk",
        lambda: _agent(cast(LLMBackend, backend), registry),
        _hypotheses(),
    )

    assert result.plans == []
    assert result.status == "ok"
    assert result.planner_loop is not None


def test_harvest_multi_label_same_real_id_not_deduplicated() -> None:
    """Two labels whose findings differ only in severity resolve (via
    ``compute_finding_id`` excluding severity) to the SAME real id. Harvest must
    produce two plans sharing that ``finding_id`` — no dedup (spec §场景:多标签
    resolve 到同一真 id 不去重).
    """
    store = FindingStore()
    f_warn = _stamped("disk full", severity="warning")
    f_crit = _stamped("disk full", severity="critical")
    # Same (inspector, version, message) → same real id despite severity diff.
    assert f_warn.id == f_crit.id
    labels = store.seed([f_warn, f_crit])
    assert labels == ["F1", "F2"]

    loop_result = _loop_result(
        invocations=[
            ToolInvocation(
                tool_name="propose_remediation",
                tool_use_id="tu_1",
                input={
                    "finding_label": "F1",
                    "rationale": "rotate",
                    "estimated_duration_seconds": 10,
                    "steps": [_low_step(forward="logrotate -f /etc/logrotate.conf")],
                },
                output={"accepted": True, "echoed_label": "F1"},
            ),
            ToolInvocation(
                tool_name="propose_remediation",
                tool_use_id="tu_2",
                input={
                    "finding_label": "F2",
                    "rationale": "truncate",
                    "estimated_duration_seconds": 5,
                    "steps": [_low_step(forward="truncate -s0 /var/log/big.log")],
                },
                output={"accepted": True, "echoed_label": "F2"},
            ),
        ]
    )

    plans = harvest_plans(loop_result, store, _TARGET)
    assert len(plans) == 2  # not deduplicated
    assert plans[0].finding_id == plans[1].finding_id == f_warn.id
    assert plans[0].rationale == "rotate"
    assert plans[1].rationale == "truncate"


# ===========================================================================
# 4.6 (control flow) — skip planner when diagnosis not ok / no findings
# ===========================================================================


@pytest.mark.parametrize(
    ("diagnosis_status", "findings"),
    [
        ("degraded_max_turns", "with_findings"),
        ("ok", "empty"),
    ],
)
async def test_planner_skipped_makes_zero_factory_calls(
    diagnosis_status: str, findings: str
) -> None:
    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    seeded = [SeededFinding(label="F1", finding=f1)] if findings == "with_findings" else []

    factory_calls = {"n": 0}

    def _never_factory() -> RemediationPlannerAgent:
        factory_calls["n"] += 1
        raise AssertionError("planner_agent_factory must not be called on skip path")

    result = await run_remediation_planning(
        cast(Any, diagnosis_status),
        seeded,
        store,
        _TARGET,
        "fix disk",
        _never_factory,
        _hypotheses(),
    )

    assert factory_calls["n"] == 0
    assert result.plans == []
    assert result.planner_loop is None
    assert result.status == diagnosis_status


# ===========================================================================
# 4.7 — handler executes nothing
# ===========================================================================


async def test_propose_handler_executes_no_command_touches_no_target() -> None:
    """Dispatching ``propose_remediation`` must only hit-check + ack: it must
    never call ``target.exec`` / run any command (side_effects="none").

    A ``ToolContext`` whose ``TargetRegistry`` would raise on any target access
    proves no execution path is taken.
    """
    import asyncio as _asyncio

    import structlog as _structlog

    from hostlens.tools.base import NoopApprovalService, ToolContext

    store = FindingStore()
    f1 = _stamped("disk full")
    store.seed([f1])
    registry = _propose_only_registry(store)

    exec_calls = {"n": 0}

    class _ExplodingTargetRegistry:
        def get(self, name: str) -> Any:
            exec_calls["n"] += 1
            raise AssertionError("propose_remediation must not access any target")

        def list_entries(self) -> list[Any]:
            return []

    def _exploding_ctx() -> ToolContext:
        return ToolContext(
            target_registry=cast(Any, _ExplodingTargetRegistry()),
            inspector_registry=cast(Any, object()),
            config=Settings(),
            logger=cast(Any, _structlog.get_logger("test_no_exec")),
            approval_service=NoopApprovalService(),
            cancel=_asyncio.Event(),
        )

    backend = FakeBackend(
        responses=[
            _propose_tool_use(block_id="tu_1", finding_label="F1"),
            _end_turn("done"),
        ]
    )

    agent = RemediationPlannerAgent(
        cast(LLMBackend, backend), registry, _settings(), _exploding_ctx
    )
    loop_result = await agent.run(
        "fix", [SeededFinding(label="F1", finding=f1)], _hypotheses(), _TARGET
    )

    # The propose invocation succeeded (ack) without ever touching a target.
    inv = next(i for i in loop_result.tool_invocations if i.tool_name == "propose_remediation")
    assert inv.error is None
    assert inv.output is not None
    assert exec_calls["n"] == 0
    # And the plan still harvests cleanly.
    assert len(harvest_plans(loop_result, store, _TARGET)) == 1


# ===========================================================================
# Construction failure: missing prompt template (fail-fast)
# ===========================================================================


def test_missing_prompt_template_raises_config_error(tmp_path: Any) -> None:
    store = FindingStore()
    registry = _propose_only_registry(store)
    backend = FakeBackend(responses=[])
    missing = tmp_path / "nope.md"

    with pytest.raises(ConfigError) as exc_info:
        RemediationPlannerAgent(
            cast(LLMBackend, backend),
            registry,
            _settings(),
            make_ctx,
            prompt_path=str(missing),
        )
    assert exc_info.value.kind == "remediation_planner_prompt_missing"


# ===========================================================================
# 5.4 — offline demo semantics (P1↔P2 gate evidence): /var/log full → plan,
#       no command execution end to end.
# ===========================================================================


async def test_offline_demo_var_log_full_to_plan_no_execution() -> None:
    """The P1↔P2 gating evidence as an end-to-end test (FakeBackend, zero real
    API, zero command execution): a /var/log-full finding flows through the
    planner and yields a printable ``RemediationPlan`` while nothing executes.
    """
    store = FindingStore()
    f1 = _stamped("/var/log 文件系统使用率 98% (15G/15G)")
    store.seed([f1])
    registry = _propose_only_registry(store)

    high_step = {
        "description": "强制轮转并清理过期日志",
        "precheck_cmd": "df -h /var/log",  # high → precheck present
        "forward_cmd": "logrotate -f /etc/logrotate.conf && journalctl --vacuum-size=500M",
        "rollback_cmd": None,  # only high may omit rollback
        "verify_cmd": "df -h /var/log",
        "risk_level": "high",
    }
    backend = FakeBackend(
        responses=[
            _propose_tool_use(
                block_id="tu_demo",
                finding_label="F1",
                rationale="磁盘被日志写满, 轮转 + vacuum 释放空间",
                steps=[high_step],
                estimated_duration_seconds=60,
            ),
            _end_turn("已为 /var/log 占满给出修复方案"),
        ]
    )

    result = await run_remediation_planning(
        "ok",
        [SeededFinding(label="F1", finding=f1)],
        store,
        _TARGET,
        "为什么 /var/log 占满了",
        lambda: _agent(cast(LLMBackend, backend), registry),
        _hypotheses(),
    )

    assert result.status == "ok"
    assert len(result.plans) == 1
    plan = result.plans[0]
    # The plan is printable / serializable (what a demo would render).
    rendered = plan.model_dump_json(indent=2)
    assert plan.finding_id == f1.id
    assert plan.target_name == _TARGET
    assert "logrotate" in rendered
    assert plan.steps[0].risk_level == "high"
    # P1↔P2 gate: no step ever executed — the model only emitted DATA. The
    # forward_cmd is a string on the plan, never invoked.
    assert isinstance(plan.steps[0].forward_cmd, str)
