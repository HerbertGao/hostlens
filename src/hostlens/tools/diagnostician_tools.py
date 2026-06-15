"""Diagnostician tool batch + `register_diagnostician_tools` assembly point.

This module declares the two **new** Diagnostician ToolSpecs
(`correlate_findings` / `request_more_inspection`) and the assembly function
`register_diagnostician_tools`, which wires the restricted three-tool registry
the Diagnostician loop runs against (`correlate_findings` +
`request_more_inspection` + the reused `list_inspectors`; **never**
`list_targets` — design D-6 / §7 minimal capability).

Id stamping for the Planner-phase findings is done by the orchestration-layer
seed helper `cli._intent._seed_findings_from_snapshot`, which reads the per-run
`InspectorResultCollector` snapshot directly (each `InspectorResult` natively
carries its `name` / `version` / `findings`, so there is no registry re-lookup).

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

from hostlens.core.exceptions import InspectorError, ToolError
from hostlens.inspectors.runner import InspectorRunner
from hostlens.reporting.models import compute_finding_id
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.default_tools import list_inspectors
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.inspector_result_collector import InspectorResultCollector
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

__all__ = [
    "register_diagnostician_tools",
    "register_narrate_only_diagnostician_tools",
]


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


_DEFAULT_REQUEST_MORE_INSPECTION_DESCRIPTION = (
    "Re-run one inspector against the target under diagnosis when the "
    "evidence in hand is insufficient. Returns the inspector status plus "
    "any new findings, each with a fresh ordinal label you may reference "
    "from a LATER turn's correlate_findings."
)


def build_request_more_inspection_spec(
    finding_store: FindingStore,
    target_name: str,
    clock: Callable[[], datetime] | None,
    collector: InspectorResultCollector | None,
    *,
    agent_description: str = _DEFAULT_REQUEST_MORE_INSPECTION_DESCRIPTION,
) -> ToolSpec:
    """Build the `request_more_inspection` ToolSpec around per-run dependencies.

    ``agent_description`` defaults to the Diagnostician copy (which mentions
    ``correlate_findings``); the Remediation Planner assembly passes a variant
    that mentions ``propose_remediation`` instead, so the planner prompt is never
    told about a tool it lacks (design Decision 4).

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

        # Collect the complete InspectorResult (side channel, not wire) so the
        # supplementary collection lands in the same per-run collector the
        # Planner phase fed — the orchestration layer snapshots Planner + supplement
        # together after the diagnosis loop. ok and non-ok alike (real status/version).
        if collector is not None:
            collector.append(result)

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
        agent_description=agent_description,
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
    collector: InspectorResultCollector | None = None,
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
    passes ``None`` → real UTC). ``collector`` mirrors
    ``register_default_tools(collector=...)``: when supplied (the ``--intent``
    path shares the **same** per-run ``InspectorResultCollector`` as the Planner
    phase), ``request_more_inspection`` appends each supplementary
    ``InspectorResult`` so the orchestration layer snapshots Planner + supplement
    together after the diagnosis loop. Non-idempotent: a duplicate call on the same
    registry raises ``ToolError`` (``ToolRegistry.register`` rejects dupes).
    """
    registry.register(_build_correlate_findings_spec(finding_store))
    registry.register(
        build_request_more_inspection_spec(finding_store, target_name, clock, collector)
    )
    registry.register(list_inspectors)


def register_narrate_only_diagnostician_tools(
    registry: ToolRegistry,
    *,
    finding_store: FindingStore,
) -> None:
    """Register the **narrate-only** Diagnostician tool batch into ``registry``.

    Registers exactly **one** tool — ``correlate_findings`` (closure-bound to
    ``finding_store``, reusing the same ``_build_correlate_findings_spec``
    factory as the full assembly so the structured-output channel is byte-for-
    byte identical). It deliberately does **not** register
    ``request_more_inspection`` / ``list_inspectors`` / ``list_targets``: the
    deterministic inspection mode (`mode=deterministic`) fixes coverage in the
    collection phase (run a fixed inspector set per target), so the diagnosis
    phase must **only** narrate root causes over the already-collected results.
    Withholding ``request_more_inspection`` and ``list_inspectors`` makes the
    "no re-inspection, no roaming, token-bounded" contract structural — the LLM
    simply has no tool to re-run an inspector or discover one to add (spec
    diagnostician-agent §需求:诊断师装配必须支持 narrate-only 变体).

    This is a separate assembly entry point rather than a boolean flag on
    ``register_diagnostician_tools``: the full assembly's ``target_name`` /
    ``clock`` / ``collector`` parameters only exist to wire
    ``request_more_inspection``, which narrate-only never registers — folding
    the two paths into one signature would force the caller to pass dependencies
    the narrate-only path has no use for.

    Non-idempotent: a duplicate call on the same registry raises ``ToolError``
    (``ToolRegistry.register`` rejects dupes).
    """
    registry.register(_build_correlate_findings_spec(finding_store))
