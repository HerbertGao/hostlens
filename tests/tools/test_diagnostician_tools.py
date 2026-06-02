"""Tests for the Diagnostician tool layer (group B: tasks 2.2 / 2.3 / 3.1-3.3).

Covers:

- ``stamp_planner_findings`` (2.2): re-grouping `run_inspector` invocations,
  version reverse-lookup, stable id stamping with correct name/version.
- ``stamp_planner_findings`` fail-loud (2.3): an unloaded inspector makes the
  helper bubble `inspector_not_found` (CLI exit-2 wrapping is group D's job).
- ``correlate_findings`` ToolSpec (3.1): hit → accepted; dangling label → error
  envelope through the agent adapter (ToolError → wrapped, no crash).
- ``request_more_inspection`` ToolSpec (3.2): new finding lands in the store and
  is resolvable; non-ok status surfaced; unknown inspector / unknown target →
  ToolError; parameters transparently passed to `InspectorRunner.run`.
- ``register_diagnostician_tools`` (3.3): registry has the three-tool set and no
  `list_targets`.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

import pytest
import structlog

from hostlens.agent.loop import LoopResult, LoopUsage, ToolInvocation
from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError, TargetError, ToolError
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.schema import CollectSpec, InspectorManifest, ParseSpec
from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.diagnostician_tools import (
    register_diagnostician_tools,
    stamp_planner_findings,
)
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.correlate_findings import CorrelateFindingsInput
from hostlens.tools.schemas.request_more_inspection import RequestMoreInspectionInput

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _make_target_registry_with_local(name: str = "local-host") -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=name, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=name))
    registry.register(target, entry)
    return registry


def _ctx(
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> ToolContext:
    return ToolContext(
        target_registry=target_registry,
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_diagnostician_tools"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _run_inspector_invocation(
    inspector_name: str, messages: list[str], *, tool_use_id: str
) -> ToolInvocation:
    """Build a `run_inspector` ToolInvocation whose `output` mirrors the
    wire-stripped `RunInspectorOutput` (no id / inspector identity)."""
    return ToolInvocation(
        tool_name="run_inspector",
        tool_use_id=tool_use_id,
        input={"target_name": "local-host", "inspector_name": inspector_name},
        output={
            "target_name": "local-host",
            "inspector_name": inspector_name,
            "findings": [
                {"severity": "warning", "message": m, "evidence": [], "tags": []} for m in messages
            ],
        },
    )


def _loop_result(invocations: list[ToolInvocation]) -> LoopResult:
    return LoopResult(
        final_text="",
        tool_invocations=invocations,
        turns=len(invocations),
        terminal_status="ok",
        usage_totals=LoopUsage(),
        stop_reason="end_turn",
    )


def _register_manifest(
    registry: InspectorRegistry,
    *,
    name: str,
    version: str = "1.0.0",
    command: str = "echo hello",
    parameters: dict[str, Any] | None = None,
) -> None:
    manifest = InspectorManifest.model_construct(
        name=name,
        version=version,
        description="probe-free manifest",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        parameters=parameters,
        secrets=[],
        collect=CollectSpec(command=command, timeout_seconds=5),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[],
    )
    registry.register(manifest, source_path=None)


# ---------------------------------------------------------------------------
# 2.2 — stamp_planner_findings: re-group + reverse-lookup version + stamp id
# ---------------------------------------------------------------------------


def test_stamp_planner_findings_two_inspectors_stable_ids() -> None:
    inspector_registry = _make_inspector_registry()
    _register_manifest(inspector_registry, name="alpha.probe", version="2.1.0")
    _register_manifest(inspector_registry, name="beta.probe", version="3.0.0")

    loop_result = _loop_result(
        [
            _run_inspector_invocation("alpha.probe", ["disk full"], tool_use_id="t1"),
            _run_inspector_invocation("beta.probe", ["high load", "swap thrash"], tool_use_id="t2"),
        ]
    )

    stamped = stamp_planner_findings(loop_result, inspector_registry)
    assert len(stamped) == 3

    first = stamped[0]
    assert first.inspector_name == "alpha.probe"
    assert first.inspector_version == "2.1.0"
    assert first.id == compute_finding_id("alpha.probe", "2.1.0", "disk full")

    second = stamped[1]
    assert second.inspector_name == "beta.probe"
    assert second.inspector_version == "3.0.0"
    assert second.id == compute_finding_id("beta.probe", "3.0.0", "high load")

    third = stamped[2]
    assert third.id == compute_finding_id("beta.probe", "3.0.0", "swap thrash")


def test_stamp_planner_findings_ignores_non_run_inspector_and_error_invocations() -> None:
    inspector_registry = _make_inspector_registry()
    _register_manifest(inspector_registry, name="alpha.probe")

    error_inv = ToolInvocation(
        tool_name="list_inspectors",
        tool_use_id="t0",
        input={},
        error={"is_error": True, "error_kind": "X", "message": "y"},
    )
    failed_run = ToolInvocation(
        tool_name="run_inspector",
        tool_use_id="t1",
        input={"target_name": "local-host", "inspector_name": "alpha.probe"},
        error={"is_error": True, "error_kind": "ToolError", "message": "boom"},
    )
    ok_run = _run_inspector_invocation("alpha.probe", ["only one"], tool_use_id="t2")

    stamped = stamp_planner_findings(
        _loop_result([error_inv, failed_run, ok_run]), inspector_registry
    )
    assert [f.message for f in stamped] == ["only one"]


# ---------------------------------------------------------------------------
# 2.3 — stamp_planner_findings fail-loud on unloaded inspector
# ---------------------------------------------------------------------------


def test_stamp_planner_findings_fail_loud_when_inspector_unloaded() -> None:
    """If the inspector was unloaded/renamed after the Planner ran, the helper
    must bubble `inspector_not_found` rather than silently skip the group.

    (The CLI-boundary `internal: ... → exit 2` wrapping is group D's concern;
    here we only assert the helper raises and does not swallow.)"""
    inspector_registry = _make_inspector_registry()  # never registers gone.probe
    loop_result = _loop_result(
        [_run_inspector_invocation("gone.probe", ["orphan"], tool_use_id="t1")]
    )

    with pytest.raises(InspectorError) as exc_info:
        stamp_planner_findings(loop_result, inspector_registry)
    assert exc_info.value.kind == "inspector_not_found"


# ---------------------------------------------------------------------------
# 3.1 — correlate_findings: hit → accepted; dangling → error envelope
# ---------------------------------------------------------------------------


def _stamped(message: str) -> Finding:
    fid = compute_finding_id("alpha.probe", "1.0.0", message)
    return Finding(
        severity="warning",
        message=message,
        id=fid,
        inspector_name="alpha.probe",
        inspector_version="1.0.0",
    )


def test_correlate_findings_hit_returns_accepted() -> None:
    store = FindingStore()
    store.seed([_stamped("disk full"), _stamped("high load")])  # F1, F2

    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="local-host")
    ctx = _ctx(TargetRegistry(), _make_inspector_registry())

    async def go() -> Any:
        return await tool_registry.dispatch(
            "correlate_findings",
            CorrelateFindingsInput(
                description="disk pressure causes load",
                confidence="high",
                supporting_findings=["F1", "F2"],
                suggested_actions=["free disk"],
            ),
            ctx,
        )

    out = asyncio.run(go())
    assert out.accepted is True
    assert out.echoed_labels == ["F1", "F2"]


def test_correlate_findings_dangling_label_becomes_error_envelope() -> None:
    """A dangling label makes the handler raise ToolError; the agent adapter
    must wrap it into a self-correctable error envelope (not crash)."""
    from hostlens.agent.tools_adapter import ToolsAdapter

    store = FindingStore()
    store.seed([_stamped("disk full")])  # only F1 exists

    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="local-host")
    ctx = _ctx(TargetRegistry(), _make_inspector_registry())
    adapter = ToolsAdapter(tool_registry, lambda: ctx)

    async def go() -> dict[str, Any]:
        return await adapter.dispatch(
            "correlate_findings",
            {
                "description": "spurious",
                "confidence": "low",
                "supporting_findings": ["F1", "F9"],
                "suggested_actions": [],
            },
        )

    envelope = asyncio.run(go())
    assert envelope["is_error"] is True
    assert envelope["error_kind"] == "ToolError"
    assert "dangling_finding_label" in envelope["message"]
    assert "F9" in envelope["message"]


# ---------------------------------------------------------------------------
# 3.2 — request_more_inspection handler
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_request_more_inspection_new_finding_lands_in_store_and_resolves() -> None:
    """Use a real local target + a manifest whose finding rule fires, so a new
    finding is collected, stamped, labeled, appended, and later resolvable."""
    inspector_registry = _make_inspector_registry()
    target_registry = _make_target_registry_with_local("local-host")

    from hostlens.inspectors.schema import FindingRule

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
        findings=[
            FindingRule(
                when="raw != ''",
                severity="warning",
                message="saw output: {raw}",
            )
        ],
    )
    inspector_registry.register(manifest, source_path=None)

    store = FindingStore()
    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="local-host")
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await tool_registry.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="echo.finder"),
            ctx,
        )

    out = asyncio.run(go())
    assert out.status == "ok"
    assert len(out.findings) == 1
    labeled = out.findings[0]
    assert labeled.label == "F1"
    assert labeled.finding.id == compute_finding_id("echo.finder", "9.9.9", "saw output: hello\n")
    # The new finding is in the per-run store and resolvable by its label.
    assert store.resolve_label("F1") == labeled.finding.id
    assert store.snapshot()[0].message == "saw output: hello\n"


def test_request_more_inspection_non_ok_status_surfaced() -> None:
    """A target whose exec raises TargetError yields status=target_unreachable
    with empty findings — surfaced verbatim, not swallowed."""
    inspector_registry = _make_inspector_registry()
    _register_manifest(inspector_registry, name="probe.free")

    class _UnreachableTarget:
        type = "local"
        name = "broken"

        def __init__(self) -> None:
            from hostlens.targets.base import Capability

            self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}

        async def exec(self, cmd, *, timeout, env=None):  # type: ignore[no-untyped-def]
            raise TargetError(kind="ssh_connection_lost", target=self.name)

        async def read_file(self, path):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    target_registry = TargetRegistry()
    target_registry.register(
        cast("ExecutionTarget", _UnreachableTarget()),
        LocalEntry(name="broken", type="local", enabled=True),
    )

    store = FindingStore()
    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="broken")
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await tool_registry.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="probe.free"),
            ctx,
        )

    out = asyncio.run(go())
    assert out.status == "target_unreachable"
    assert out.findings == []
    assert store.snapshot() == []


def test_request_more_inspection_unknown_inspector_raises_tool_error() -> None:
    inspector_registry = _make_inspector_registry()
    target_registry = TargetRegistry()
    store = FindingStore()
    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="local-host")
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await tool_registry.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="does.not.exist"),
            ctx,
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(go())
    assert "inspector_not_found" in str(exc_info.value)


def test_request_more_inspection_unknown_target_raises_tool_error() -> None:
    """The closure-fixed target_name is not in the registry → ToolError."""
    inspector_registry = _make_inspector_registry()
    _register_manifest(inspector_registry, name="probe.free")
    target_registry = TargetRegistry()  # empty — "missing-host" not registered
    store = FindingStore()
    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="missing-host")
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await tool_registry.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="probe.free"),
            ctx,
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(go())
    assert "target_not_found" in str(exc_info.value)
    assert "missing-host" in str(exc_info.value)


def test_request_more_inspection_parameters_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`parameters` must be transparently forwarded to InspectorRunner.run."""
    inspector_registry = _make_inspector_registry()
    _register_manifest(
        inspector_registry,
        name="param.probe",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string", "pattern": "^[a-z]+$"}},
            "additionalProperties": False,
        },
    )
    target_registry = _make_target_registry_with_local("local-host")
    store = FindingStore()
    tool_registry = ToolRegistry()
    register_diagnostician_tools(tool_registry, finding_store=store, target_name="local-host")
    ctx = _ctx(target_registry, inspector_registry)

    import hostlens.inspectors.runner as runner_mod

    captured: dict[str, Any] = {}
    real_run = runner_mod.InspectorRunner.run

    async def spy_run(self, manifest, target, parameters=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["parameters"] = parameters
        return await real_run(self, manifest, target, parameters, **kwargs)

    monkeypatch.setattr(runner_mod.InspectorRunner, "run", spy_run)

    async def go() -> Any:
        return await tool_registry.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="param.probe", parameters={"key": "abc"}),
            ctx,
        )

    asyncio.run(go())

    assert captured["parameters"] == {"key": "abc"}


# ---------------------------------------------------------------------------
# 3.3 — register_diagnostician_tools: three-tool set, no list_targets
# ---------------------------------------------------------------------------


def test_register_diagnostician_tools_registers_three_no_list_targets() -> None:
    store = FindingStore()
    reg = ToolRegistry()
    register_diagnostician_tools(reg, finding_store=store, target_name="local-host")
    assert sorted(reg.names()) == [
        "correlate_findings",
        "list_inspectors",
        "request_more_inspection",
    ]
    assert "list_targets" not in reg.names()
