"""Collector closure-injection tests (tasks 1.2 / 1.3).

Asserts that injecting a per-run `InspectorResultCollector` into
`register_default_tools` / `register_diagnostician_tools` makes the
`run_inspector` / `request_more_inspection` handlers append the **complete**
`InspectorResult` the runner returns (real status / version / duration), for
both ok and non-ok runs, while leaving the wire projection unchanged and
behaviour byte-identical when no collector is injected.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import InspectorRegistry, build_registry_from_search_paths
from hostlens.inspectors.schema import CollectSpec, FindingRule, InspectorManifest, ParseSpec
from hostlens.targets.base import Capability, ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.diagnostician_tools import register_diagnostician_tools
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.inspector_result_collector import InspectorResultCollector
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.request_more_inspection import RequestMoreInspectionInput
from hostlens.tools.schemas.run_inspector import RunInspectorInput

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)


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
        logger=structlog.get_logger("test_collector_injection"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _finding_manifest(name: str = "echo.finder", version: str = "9.9.9") -> InspectorManifest:
    return InspectorManifest.model_construct(
        name=name,
        version=version,
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
        findings=[FindingRule(when="raw != ''", severity="warning", message="saw: {raw}")],
    )


# ---------------------------------------------------------------------------
# 1.2 — register_default_tools(collector=...) → run_inspector handler appends
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_run_inspector_collector_captures_complete_ok_result() -> None:
    inspector_registry = _make_inspector_registry()
    inspector_registry.register(_finding_manifest(), source_path=None)
    target_registry = _make_target_registry_with_local("local-host")

    collector = InspectorResultCollector()
    reg = ToolRegistry()
    register_default_tools(reg, collector=collector)
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await reg.dispatch(
            "run_inspector",
            RunInspectorInput(target_name="local-host", inspector_name="echo.finder"),
            ctx,
        )

    wire = asyncio.run(go())

    # Wire projection unchanged: no status / version on the output surface.
    assert wire.target_name == "local-host"
    assert wire.inspector_name == "echo.finder"
    assert not hasattr(wire, "status")
    assert not hasattr(wire, "version")

    # Collector holds the COMPLETE InspectorResult (real status/version/duration).
    snap = collector.snapshot()
    assert len(snap) == 1
    result = snap[0]
    assert result.status == "ok"
    assert result.version == "9.9.9"
    assert result.name == "echo.finder"
    assert result.duration_seconds >= 0.0
    assert [f.message for f in result.findings] == ["saw: hello\n"]


def test_run_inspector_collector_captures_non_ok_result() -> None:
    """A target whose exec raises TargetError → target_unreachable; the non-ok
    InspectorResult (real status/version) must still land in the collector."""
    from hostlens.core.exceptions import TargetError

    inspector_registry = _make_inspector_registry()
    inspector_registry.register(
        _finding_manifest(name="probe.free", version="2.0.0"), source_path=None
    )

    class _UnreachableTarget:
        type = "local"
        name = "broken"

        def __init__(self) -> None:
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

    collector = InspectorResultCollector()
    reg = ToolRegistry()
    register_default_tools(reg, collector=collector)
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await reg.dispatch(
            "run_inspector",
            RunInspectorInput(target_name="broken", inspector_name="probe.free"),
            ctx,
        )

    wire = asyncio.run(go())
    assert wire.findings == []  # wire still shows empty findings for non-ok

    snap = collector.snapshot()
    assert len(snap) == 1
    result = snap[0]
    assert result.status == "target_unreachable"
    assert result.version == "2.0.0"
    assert result.error is not None


@_POSIX_ONLY
def test_run_inspector_without_collector_behaviour_unchanged() -> None:
    """No collector injected → handler does not collect; wire output identical."""
    inspector_registry = _make_inspector_registry()
    inspector_registry.register(_finding_manifest(), source_path=None)
    target_registry = _make_target_registry_with_local("local-host")

    reg = ToolRegistry()
    register_default_tools(reg)  # no collector
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await reg.dispatch(
            "run_inspector",
            RunInspectorInput(target_name="local-host", inspector_name="echo.finder"),
            ctx,
        )

    wire = asyncio.run(go())
    assert wire.target_name == "local-host"
    assert wire.inspector_name == "echo.finder"
    assert [f.message for f in wire.findings] == ["saw: hello\n"]


# ---------------------------------------------------------------------------
# 1.3 — register_diagnostician_tools(collector=...) → request_more_inspection
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_request_more_inspection_appends_supplement_into_same_collector() -> None:
    inspector_registry = _make_inspector_registry()
    inspector_registry.register(_finding_manifest(), source_path=None)
    target_registry = _make_target_registry_with_local("local-host")

    # The same collector instance the Planner phase would have fed.
    collector = InspectorResultCollector()
    store = FindingStore()
    reg = ToolRegistry()
    register_diagnostician_tools(
        reg,
        finding_store=store,
        target_name="local-host",
        collector=collector,
    )
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> Any:
        return await reg.dispatch(
            "request_more_inspection",
            RequestMoreInspectionInput(inspector_name="echo.finder"),
            ctx,
        )

    out = asyncio.run(go())
    assert out.status == "ok"

    # The supplementary InspectorResult landed in the collector with real fields.
    snap = collector.snapshot()
    assert len(snap) == 1
    result = snap[0]
    assert result.name == "echo.finder"
    assert result.version == "9.9.9"
    assert result.status == "ok"
    assert [f.message for f in result.findings] == ["saw: hello\n"]
