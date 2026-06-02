"""Diagnostician tool batch + `register_diagnostician_tools` assembly point.

This module declares the two **new** Diagnostician ToolSpecs
(`correlate_findings` / `request_more_inspection`) and the assembly function
`register_diagnostician_tools`, which wires the restricted three-tool registry
the Diagnostician loop runs against (`correlate_findings` +
`request_more_inspection` + the reused `list_inspectors`; **never**
`list_targets` — design D-6 / §7 minimal capability).

It also provides `stamp_planner_findings`, the assembly-layer helper that
re-stamps the Planner's findings with stable ids by re-grouping the
`run_inspector` tool invocations (design D-3). The Planner's flattened
`PlannerResult.findings` lost their grouping, but each successful
`run_inspector` invocation's `output` still carries its `inspector_name` and
that group's findings — so this helper re-groups from `tool_invocations`,
looks the version up via `InspectorRegistry.get(name)`, and calls
`compute_finding_id` (forbidden to use the already-flattened
`PlannerResult.findings` as the grouping source).

Per CLAUDE.md §4.10 / design.md §D-3, `@tool` is a pure spec factory:
decoration mutates no module-level registry — assembly is explicit, threaded
through `register_diagnostician_tools` with closure-bound per-run dependencies
(the `FindingStore`, the fixed `target_name`, and the optional `clock`), exactly
mirroring `register_default_tools`'s `build_run_inspector_spec` closure
precedent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel

from hostlens.agent.loop import LoopResult
from hostlens.core.exceptions import InspectorError, ToolError
from hostlens.inspectors.registry import InspectorRegistry
from hostlens.inspectors.runner import InspectorRunner
from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.default_tools import list_inspectors, run_inspector
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.correlate_findings import (
    CorrelateFindingsInput,
    CorrelateFindingsOutput,
)
from hostlens.tools.schemas.request_more_inspection import (
    LabeledFinding,
    RequestMoreInspectionInput,
    RequestMoreInspectionOutput,
)
from hostlens.tools.schemas.run_inspector import RunInspectorOutput

__all__ = [
    "register_diagnostician_tools",
    "stamp_planner_findings",
]


# ---------------------------------------------------------------------------
# Assembly-layer id stamping (design D-3)
# ---------------------------------------------------------------------------


def stamp_planner_findings(
    loop_result: LoopResult,
    inspector_registry: InspectorRegistry,
) -> list[Finding]:
    """Re-stamp the Planner's findings with stable ids (design D-3).

    Re-groups from ``loop_result.tool_invocations`` — the successful
    ``run_inspector`` invocations, each carrying its ``inspector_name`` plus
    that group's findings on ``inv.output`` — and **never** from the already
    flattened ``PlannerResult.findings`` (which lost the grouping). For each
    group the inspector's ``version`` is looked up via
    ``InspectorRegistry.get(name).version`` (the only available source: the
    ``run_inspector`` wire projection strips version off the output), and
    each finding is stamped with ``compute_finding_id`` plus its
    ``inspector_name`` / ``inspector_version`` via ``model_copy``.

    ``inspector_registry`` must be the **same instance** the Planner ran
    against (the orchestration layer holds it and shares it with both the
    context factory and this helper) to avoid a TOCTOU version skew.

    Fail-loud (design D-3): if ``InspectorRegistry.get(name)`` raises
    ``inspector_not_found`` (the inspector was unloaded / renamed after the
    Planner ran), this helper lets it propagate — it must NOT silently skip
    the group, because skipping would make any hypothesis that references one
    of those findings reference a vanished id. The CLI boundary (group D) wraps
    the bubbled exception into ``internal: ... → exit 2``.
    """
    stamped: list[Finding] = []
    for inv in loop_result.tool_invocations:
        if inv.tool_name != run_inspector.name or inv.output is None:
            continue
        output = RunInspectorOutput.model_validate(inv.output)
        # `get` raises InspectorError(kind="inspector_not_found") on a missing
        # name; we deliberately do NOT catch it — fail-loud per design D-3.
        manifest = inspector_registry.get(output.inspector_name)
        version = manifest.version
        for finding in output.findings:
            stamped.append(
                finding.model_copy(
                    update={
                        "inspector_name": output.inspector_name,
                        "inspector_version": version,
                        "id": compute_finding_id(output.inspector_name, version, finding.message),
                    }
                )
            )
    return stamped


# ---------------------------------------------------------------------------
# Handlers (closure-bound per-run dependencies at assembly time)
# ---------------------------------------------------------------------------
#
# `@tool` requires the broad `(BaseModel, ToolContext) -> Awaitable[BaseModel]`
# handler shape (Callable is contravariant in its argument types), so the
# concrete-typed closures below are cast back at decoration time exactly as
# `default_tools._BroadHandler` does. Runtime correctness is enforced by
# ToolSpec's validators + the registry's isinstance gates, not by static types.
_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


def _build_correlate_findings_spec(finding_store: FindingStore) -> ToolSpec:
    """Build the `correlate_findings` ToolSpec around a per-run `FindingStore`.

    The handler is a structured-output channel only (design D-2): it does **no**
    correlation/reasoning and does **not** record real ids. It resolves the
    ``supporting_findings`` ordinal labels against ``finding_store`` solely to
    hit-check them — any dangling label (including a same-turn forward reference
    not yet returned by a ``request_more_inspection`` tool_result) raises a
    structured ``ToolError`` so the agent adapter wraps it into an error
    envelope the loop feeds back for self-correction (design D-8); it never
    crashes and never silently accepts. The real labels → ids resolution happens
    in the orchestration-layer harvest (group C), not here — the output is a
    bare ack that echoes only the accepted labels.
    """

    async def correlate_findings_handler(
        args: CorrelateFindingsInput, ctx: ToolContext
    ) -> CorrelateFindingsOutput:
        del ctx  # all dependencies come from the closure, none from ctx
        dangling = [
            label for label in args.supporting_findings if not finding_store.contains(label)
        ]
        if dangling:
            raise ToolError(
                "dangling_finding_label: supporting_findings reference label(s) "
                f"not present in this run's finding set: {sorted(dangling)}. "
                "Only reference labels shown in the findings list or returned by "
                "a previous turn's request_more_inspection."
            )
        return CorrelateFindingsOutput(
            accepted=True,
            echoed_labels=list(args.supporting_findings),
        )

    return tool(
        name="correlate_findings",
        version="1.0.0",
        input_schema=CorrelateFindingsInput,
        output_schema=CorrelateFindingsOutput,
        agent_description=(
            "Record one root-cause hypothesis correlating the findings you have. "
            'Reference findings by their ordinal labels (e.g. ["F1", "F3"]) '
            "shown in the findings list. Call this once per distinct hypothesis."
        ),
        mcp_description=(
            "Record one root-cause hypothesis (description / confidence / "
            "supporting finding labels / suggested actions). Output is a bare "
            "acknowledgement."
        ),
        cli_help=None,
        surfaces={"agent"},
        side_effects="none",
        sensitive_output=False,
        timeout=5.0,
    )(cast(_BroadHandler, correlate_findings_handler))


def _build_request_more_inspection_spec(
    finding_store: FindingStore,
    target_name: str,
    clock: Callable[[], datetime] | None,
) -> ToolSpec:
    """Build the `request_more_inspection` ToolSpec around per-run dependencies.

    The handler is **newly written** (it cannot call `run_inspector_handler`,
    whose `RunInspectorOutput` strips ids), but replicates that handler's full
    orchestration over the same `InspectorRunner` engine (design D-9):

    1. ``ctx.inspector_registry.get(inspector_name)`` → manifest
       (``inspector_not_found`` → structured ``ToolError``).
    2. ``ctx.target_registry.get(target_name)`` → ``ExecutionTarget`` object
       (the closure-fixed ``target_name``; unknown ``KeyError`` → ``ToolError``).
    3. clock injection (mirrors `register_default_tools(clock=...)`).
    4. ``InspectorRunner.run(manifest, target, parameters=..., allow_privileged
       =False, cancel=ctx.cancel)`` — ``parameters`` transparently passed.
    5. version comes **directly from** ``InspectorResult.version`` (the runner
       filled it; never re-look-it-up against the registry).
    6. stamp each finding's id, allocate a fresh unique label in
       ``finding_store``, append, return ``status`` + labeled findings.

    A non-ok inspector status is surfaced verbatim on ``output.status`` (with
    empty findings) so the model can tell a real failure from "ran, found
    nothing" (design risk mitigation).
    """

    async def request_more_inspection_handler(
        args: RequestMoreInspectionInput, ctx: ToolContext
    ) -> RequestMoreInspectionOutput:
        # 1. Lookup inspector manifest.
        try:
            manifest = ctx.inspector_registry.get(args.inspector_name)
        except InspectorError as exc:
            if exc.kind == "inspector_not_found":
                raise ToolError(
                    f"inspector_not_found: inspector_name={args.inspector_name!r} "
                    "is not registered in inspector_registry"
                ) from exc
            raise

        # 2. Resolve the closure-fixed target name into an ExecutionTarget.
        try:
            target = ctx.target_registry.get(target_name)
        except KeyError as exc:
            raise ToolError(
                f"target_not_found: target_name={target_name!r} "
                "is not registered in target_registry"
            ) from exc

        # 3. Build runner (clock injection mirrors run_inspector_handler).
        if clock is None:
            runner = InspectorRunner(
                ctx.target_registry,
                settings=ctx.config,
                logger=ctx.logger,
            )
        else:
            runner = InspectorRunner(
                ctx.target_registry,
                settings=ctx.config,
                logger=ctx.logger,
                clock=clock,
            )

        # 4. Run — parameters transparently passed; agent surface never opts in
        #    to privilege (allow_privileged=False).
        result = await runner.run(
            manifest,
            target,
            parameters=dict(args.parameters) if args.parameters else None,
            allow_privileged=False,
            cancel=ctx.cancel,
        )

        # 5-6. Stamp ids using InspectorResult.version (NOT a registry re-lookup),
        #      allocate fresh labels, append to the per-run finding-store.
        labeled: list[LabeledFinding] = []
        for finding in result.findings:
            stamped = finding.model_copy(
                update={
                    "inspector_name": result.name,
                    "inspector_version": result.version,
                    "id": compute_finding_id(result.name, result.version, finding.message),
                }
            )
            label = finding_store.append(stamped)
            labeled.append(LabeledFinding(label=label, finding=stamped))

        return RequestMoreInspectionOutput(status=result.status, findings=labeled)

    return tool(
        name="request_more_inspection",
        version="1.0.0",
        input_schema=RequestMoreInspectionInput,
        output_schema=RequestMoreInspectionOutput,
        agent_description=(
            "Re-run one inspector against the target under diagnosis when the "
            "evidence in hand is insufficient. Returns the inspector status plus "
            "any new findings, each with a fresh ordinal label you may reference "
            "from a LATER turn's correlate_findings."
        ),
        mcp_description=(
            "Run one read-only inspector against the diagnosis target. Output may "
            "contain process / port / connection metadata."
        ),
        cli_help=None,
        surfaces={"agent"},
        side_effects="read",
        sensitive_output=True,
        timeout=30.0,
    )(cast(_BroadHandler, request_more_inspection_handler))


# ---------------------------------------------------------------------------
# Explicit assembly
# ---------------------------------------------------------------------------


def register_diagnostician_tools(
    registry: ToolRegistry,
    *,
    finding_store: FindingStore,
    target_name: str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Register the restricted Diagnostician tool batch into ``registry``.

    Registers exactly three tools (design D-6):

    - ``correlate_findings`` — the structured-output channel (closure-bound to
      ``finding_store``).
    - ``request_more_inspection`` — supplementary collection (closure-bound to
      ``finding_store`` + the fixed ``target_name`` + the optional ``clock``).
    - ``list_inspectors`` — the reused module-level spec, so the Diagnostician
      can discover which inspectors are available to supplement.

    **Never** ``list_targets``: the Diagnostician is constrained to the single
    target the Planner already ran against (§7 minimal capability).

    ``clock`` mirrors ``register_default_tools(clock=...)`` and is threaded to
    ``request_more_inspection``'s ``InspectorRunner`` (the ``--intent`` path
    passes ``None`` → real UTC). Non-idempotent: a duplicate call on the same
    registry raises ``ToolError`` (``ToolRegistry.register`` rejects dupes).
    """
    registry.register(_build_correlate_findings_spec(finding_store))
    registry.register(_build_request_more_inspection_spec(finding_store, target_name, clock))
    registry.register(list_inspectors)
