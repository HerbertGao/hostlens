"""Remediation Planner tool batch + `register_remediation_planner_tools`.

This module declares the **new** `propose_remediation` ToolSpec and the assembly
function `register_remediation_planner_tools`, which wires the restricted
three-tool registry the Remediation Planner loop runs against — structurally
isomorphic to `register_diagnostician_tools`, with `correlate_findings` swapped
for `propose_remediation` (design Decision 4):

- `propose_remediation` — the structured-output channel (closure-bound to the
  per-run `FindingStore`).
- `request_more_inspection` — read-only re-inspection to recheck a finding's
  current state before proposing (the "Agent, not static template" motive —
  design Decision 4); reused from `diagnostician_tools`.
- `list_inspectors` — the reused module-level spec.

Deliberately **excludes** `correlate_findings` (the planner produces plans, not
hypotheses) and `run_inspector` (its `RunInspectorInput` requires the model to
supply `target_name`, breaking D-3 "the model never touches the target name";
rechecking goes through `request_more_inspection`, whose `target` is closure-
bound). It is **not** part of `register_default_tools` — the planner tools carry
closure-bound per-run dependencies (design Decision 4 / Migration Plan).

Agent-surface read-only invariant (M9 invariant 1): all three tools have
`side_effects ∈ {"none", "read"}`; P1b registers no write/destructive/approval
tool. `propose_remediation`'s `side_effects="none"` is load-bearing — it emits
**data** (a proposed plan), it does not execute.

Per CLAUDE.md §4.10 / design.md §D-3, `@tool` is a pure spec factory: decoration
mutates no module-level registry — assembly is explicit, threaded through
`register_remediation_planner_tools` with closure-bound per-run dependencies,
mirroring `register_diagnostician_tools`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel

from hostlens.core.exceptions import ToolError
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.default_tools import list_inspectors
from hostlens.tools.diagnostician_tools import build_request_more_inspection_spec
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.inspector_result_collector import InspectorResultCollector
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.propose_remediation import (
    ProposeRemediationInput,
    ProposeRemediationOutput,
)

__all__ = [
    "register_remediation_planner_tools",
]


# `@tool` requires the broad `(BaseModel, ToolContext) -> Awaitable[BaseModel]`
# handler shape (Callable is contravariant in its argument types), so the
# concrete-typed closure below is cast back at decoration time exactly as
# `diagnostician_tools._BroadHandler` does. Runtime correctness is enforced by
# ToolSpec's validators + the registry's isinstance gates, not by static types.
_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


# The planner has no `correlate_findings` tool: reusing `request_more_inspection`'s
# default `agent_description` verbatim would leak that non-existent tool name into
# the planner prompt (design Decision 4). This variant swaps the trailing mention
# for `propose_remediation` so the model is never told about a tool it lacks.
_PLANNER_REQUEST_MORE_INSPECTION_DESCRIPTION = (
    "Re-run one inspector against the target you are planning remediation for "
    "when you need to recheck a finding's current state before proposing a fix. "
    "Returns the inspector status plus any new findings, each with a fresh "
    "ordinal label you may reference from a LATER turn's propose_remediation."
)


def _build_propose_remediation_spec(finding_store: FindingStore) -> ToolSpec:
    """Build the `propose_remediation` ToolSpec around a per-run `FindingStore`.

    The handler is a structured-output channel only (design D-2 / Decision 2): it
    does **no** execution and records **no** real ids. It hit-checks
    ``finding_label`` against ``finding_store`` — a dangling label raises a
    structured ``ToolError`` so the agent adapter wraps it into an error envelope
    the loop feeds back for self-correction (design D-8); it never crashes and
    never silently accepts, and it never touches a target or runs a command
    (``side_effects="none"``, agent-surface read-only invariant). The label →
    real-id resolution and ``target_name`` stamping happen in the orchestration-
    layer harvest (group C), not here — the output is a bare ack that echoes only
    the accepted label.

    ``steps`` is validated as ``list[RemediationStep]`` at emit time by the agent
    adapter's ``model_validate``, so every P1a invariant is enforced at the emit
    boundary (design Decision 2).
    """

    async def propose_remediation_handler(
        args: ProposeRemediationInput, ctx: ToolContext
    ) -> ProposeRemediationOutput:
        del ctx  # all dependencies come from the closure, none from ctx
        if not finding_store.contains(args.finding_label):
            raise ToolError(
                "dangling_finding_label: finding_label references a label "
                f"not present in this run's finding set: {args.finding_label!r}. "
                "Only reference a label shown in the findings list or returned by "
                "a previous turn's request_more_inspection."
            )
        return ProposeRemediationOutput(
            accepted=True,
            echoed_label=args.finding_label,
        )

    return tool(
        name="propose_remediation",
        version="1.0.0",
        input_schema=ProposeRemediationInput,
        output_schema=ProposeRemediationOutput,
        agent_description=(
            "Record one remediation plan for one finding. Reference the finding "
            'by its ordinal label (e.g. "F1") shown in the findings list. Carry '
            "the rationale, an estimated duration in seconds, and an ordered list "
            "of steps (each with precheck/forward/rollback/verify commands and a "
            "risk level). Call this once per finding you propose to fix. This only "
            "records the plan — it executes nothing."
        ),
        mcp_description=(
            "Record one remediation plan (finding label / rationale / estimated "
            "duration / ordered steps). Output is a bare acknowledgement."
        ),
        cli_help=None,
        surfaces={"agent"},
        side_effects="none",
        requires_approval=False,
        sensitive_output=False,
        timeout=5.0,
    )(cast(_BroadHandler, propose_remediation_handler))


def register_remediation_planner_tools(
    registry: ToolRegistry,
    *,
    finding_store: FindingStore,
    target_name: str,
    clock: Callable[[], datetime] | None = None,
    collector: InspectorResultCollector | None = None,
) -> None:
    """Register the restricted Remediation Planner tool batch into ``registry``.

    Registers exactly three tools (design Decision 4), structurally isomorphic to
    ``register_diagnostician_tools`` with ``correlate_findings`` swapped for
    ``propose_remediation``:

    - ``propose_remediation`` — the structured-output channel (closure-bound to
      ``finding_store``).
    - ``request_more_inspection`` — read-only re-inspection (closure-bound to
      ``finding_store`` + the fixed ``target_name`` + the optional ``clock`` /
      ``collector``), reused from ``diagnostician_tools`` with a planner
      ``agent_description`` variant that mentions ``propose_remediation`` instead
      of the absent ``correlate_findings``.
    - ``list_inspectors`` — the reused module-level spec, so the planner can
      discover which inspectors are available to recheck against.

    **Excludes** ``correlate_findings`` (the planner produces plans, not
    hypotheses) and ``run_inspector`` (its input requires a model-supplied
    ``target_name``, breaking D-3). ``clock`` / ``collector`` mirror
    ``register_diagnostician_tools`` and are threaded to
    ``request_more_inspection``'s ``InspectorRunner``. Non-idempotent: a duplicate
    call on the same registry raises ``ToolError`` (``ToolRegistry.register``
    rejects dupes).
    """
    registry.register(_build_propose_remediation_spec(finding_store))
    registry.register(
        build_request_more_inspection_spec(
            finding_store,
            target_name,
            clock,
            collector,
            agent_description=_PLANNER_REQUEST_MORE_INSPECTION_DESCRIPTION,
        )
    )
    registry.register(list_inspectors)
