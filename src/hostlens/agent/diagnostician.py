"""Diagnostician Agent — cross-signal correlation + root-cause hypotheses.

The Planner (M2.4) condenses an intent into a flat `PlannerResult`
(findings + narrative) but stops short of correlating signals or judging
"why". This module introduces the **second** `AgentLoop`-backed Agent
(design D-1): it consumes the Planner's stamped findings and produces
`list[RootCauseHypothesis]` + a diagnostic narrative, aggregated into
`DiagnosticianResult`.

This module also hosts the `DiagnosticianAgent` class (assembler over a second
`AgentLoop`, mirroring `PlannerAgent`) plus three orchestration-layer helpers
the CLI (group D) calls: `harvest_hypotheses` (turns successful
`correlate_findings` invocations into `RootCauseHypothesis` objects with real
ids), `reconcile_status` (maps the two loops' terminal statuses onto a single
`ReportStatus` per design D-5), and `run_diagnosis` (the control-flow seam that
only launches the Diagnostician when the Planner succeeded).
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from hostlens.agent.loop import AgentLoop, LoopResult
from hostlens.agent.planner import PlannerResult
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ConfigError
from hostlens.reporting.models import Finding, ReportStatus, RootCauseHypothesis
from hostlens.tools.schemas.correlate_findings import CorrelateFindingsInput

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopObserver
    from hostlens.agent.loop import _TerminalStatus
    from hostlens.core.config import Settings
    from hostlens.tools.base import ToolContext
    from hostlens.tools.finding_store import FindingStore
    from hostlens.tools.registry import ToolRegistry

__all__ = [
    "DiagnosticianAgent",
    "DiagnosticianResult",
    "SeededFinding",
    "harvest_hypotheses",
    "reconcile_status",
    "run_diagnosis",
]


# Placeholder in ``diagnostician.md`` replaced with the rendered tool overview.
# A single ``str.replace`` keeps the Agent layer free of a template engine,
# mirroring ``planner.py`` (design D-2).
_TOOL_OVERVIEW_PLACEHOLDER = "{tool_overview}"

# Package + resource name of the external prompt template (CLAUDE.md §7).
_PROMPT_PACKAGE = "hostlens.agent.prompts"
_PROMPT_RESOURCE = "diagnostician.md"


class DiagnosticianResult(BaseModel):
    """Aggregated result of one diagnosis path (design D-7).

    Deliberately NOT a `reporting.models.Report`: a faithful `ReportMeta`
    needs `InspectorResult.status` / `duration_seconds` / `version`, which the
    `run_inspector` wire projection has already dropped, so assembling a Report
    here would force fabrication (design D-4, Scope-Core).

    Field semantics:

    - ``narrative`` — the diagnosis loop's `final_text`; **may be the empty
      string** on degraded paths (loop only carries `final_text` on `end_turn`
      / `max_tokens`; renderers must tolerate empty and must not assume non-empty
      on degrade — design context).
    - ``findings`` — the **canonical** set: the `FindingStore` snapshot after
      the diagnosis loop ends (Planner stamped findings PLUS every successful
      `request_more_inspection` addition), all carrying a stable `id`. Any id
      referenced by `hypotheses[*].supporting_findings` must be findable here.
    - ``hypotheses`` — harvested from `correlate_findings` invocations;
      `supporting_findings` already resolved to **real** `Finding.id`.
    - ``status`` — the reconciled `ReportStatus` (D-5). This path never produces
      `partial` (that value is only derived from `InspectorResult`s).
    - ``planner_result`` — kept verbatim; its nested `findings` are the
      **unstamped originals** (debug/json fidelity only, **not authoritative** —
      downstream reads the top-level `findings`).
    - ``diagnostician_loop`` — diagnosis loop telemetry; `None` **iff** the
      diagnosis stage was skipped (Planner degraded). Any path where the
      diagnosis loop actually ran (including its own `failed_api_unavailable`
      reconciled to `degraded_no_planner`) carries a non-`None` `LoopResult`.
    """

    model_config = ConfigDict(frozen=True)

    narrative: str
    findings: list[Finding]
    hypotheses: list[RootCauseHypothesis]
    status: ReportStatus
    planner_result: PlannerResult
    diagnostician_loop: LoopResult | None


class SeededFinding(BaseModel):
    """A Planner finding paired with its per-run ordinal label (`F1` / `F2` …).

    The orchestration layer seeds the `FindingStore` with the stamped Planner
    findings (obtaining one label per finding) and passes the resulting
    `(label, finding)` pairs to `DiagnosticianAgent.run`, which renders them as
    the diagnosis loop's first user message. Keeping the pairing explicit (vs.
    re-deriving labels inside `run`) lets the store stay the single label
    authority — `run` never invents a label (design D-9).

    Named ``SeededFinding`` (not ``LabeledFinding``) to disambiguate from
    ``tools.schemas.request_more_inspection.LabeledFinding`` — the latter is that
    tool's output schema; this one is the editorial seed pair the orchestration
    layer hands to ``DiagnosticianAgent.run``.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    finding: Finding


def _render_findings_block(labeled: list[SeededFinding]) -> str:
    """Render the labeled findings as the diagnosis loop's first user content.

    Each finding is one line carrying its label, severity, message, inspector,
    tags, and evidence COUNT (not the evidence bodies — design D-12). This block
    is appended to the intent and handed to ``AgentLoop.run`` as the single
    ``intent`` string, so the findings land in messages (never in the
    byte-stable system prompt — design D-10 / spec §需求).
    """
    if not labeled:
        return "本次巡检没有产生任何 finding。"
    lines: list[str] = []
    for item in labeled:
        f = item.finding
        inspector = f.inspector_name if f.inspector_name is not None else "unknown"
        tags = ",".join(f.tags) if f.tags else "-"
        lines.append(
            f"- [{item.label}] severity={f.severity} inspector={inspector} "
            f"tags={tags} evidence={len(f.evidence)} :: {f.message}"
        )
    return "\n".join(lines)


class DiagnosticianAgent:
    """Assembles a system prompt + restricted tool set + backend into one loop.

    Structurally identical to ``PlannerAgent`` (design D-1): it wraps a single
    ``AgentLoop`` and renders the external ``diagnostician.md`` system prompt at
    construction. The backend reaches ONLY the loop — never the
    ``context_factory``'s ``ToolContext`` (ADR-008 / CLAUDE.md §7). The diagnosis
    loop reuses ``settings.agent``'s budget values, but its token/turn counting
    is independent of the Planner's (each ``run`` starts a fresh ``LoopUsage``;
    design D-11).
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        settings: Settings,
        context_factory: Callable[[], ToolContext],
        *,
        prompt_path: str | None = None,
    ) -> None:
        # The rendered prompt MUST be a single-element text block list — a bare
        # str makes ``AgentLoop._inject_cache_control`` skip cache_control
        # injection, silently disabling prompt caching (design D-10, mirroring
        # planner.py).
        rendered = self._render_system_prompt(registry, prompt_path)
        system: list[dict[str, Any]] = [{"type": "text", "text": rendered}]

        adapter = ToolsAdapter(registry, context_factory)
        self._loop = AgentLoop(backend, adapter, settings, system=system)

    @staticmethod
    def _render_system_prompt(
        registry: ToolRegistry,
        prompt_path: str | None,
    ) -> str:
        """Load ``diagnostician.md`` and substitute the tool-overview placeholder.

        A missing/unreadable template fails loud (``ConfigError``) at
        construction rather than silently degrading to an empty prompt (design
        D-1 / spec §场景:系统提示模板缺失构造期失败).
        """
        try:
            if prompt_path is not None:
                from pathlib import Path

                template = Path(prompt_path).read_text(encoding="utf-8")
            else:
                template = (
                    files(_PROMPT_PACKAGE).joinpath(_PROMPT_RESOURCE).read_text(encoding="utf-8")
                )
        except (FileNotFoundError, OSError) as exc:
            raise ConfigError(
                "diagnostician prompt template not found",
                kind="diagnostician_prompt_missing",
                original=exc,
            ) from exc

        # ``list_for("agent")`` is sorted by spec.name ascending, so the overview
        # is byte-stable across runs for a fixed tool set — the prompt-caching
        # prerequisite (CLAUDE.md §4.8 / design D-10).
        overview = "\n".join(
            f"- {spec.name}: {spec.agent_description}" for spec in registry.list_for("agent")
        )
        return template.replace(_TOOL_OVERVIEW_PLACEHOLDER, overview)

    async def run(
        self,
        intent: str,
        findings: list[SeededFinding],
        *,
        observer: LoopObserver | None = None,
    ) -> LoopResult:
        """Drive the diagnosis loop over ``intent`` + labeled ``findings``.

        ``AgentLoop.run`` takes a single ``intent: str`` and builds the first
        user message itself; its signature is NOT changed here. So the intent and
        the labeled-findings block are concatenated into that one string —
        putting the dynamic findings into the loop's messages, NEVER into the
        byte-stable system prompt (design D-10 / spec §需求:findings 列表必须进
        messages、禁止进 system).

        Returns the raw ``LoopResult``; the orchestration layer harvests
        hypotheses from it (``harvest_hypotheses``) and reconciles its status
        (``reconcile_status``). ``observer`` is passed straight through to
        ``AgentLoop.run`` — the Diagnostician never interprets ``LoopEvent``.
        """
        findings_block = _render_findings_block(findings)
        combined = f"{intent}\n\n## 已采集的 findings (按序号标签)\n\n{findings_block}"
        return await self._loop.run(combined, observer=observer)


# ---------------------------------------------------------------------------
# Orchestration-layer helpers (design D-2 harvest / D-5 reconcile / control flow)
# ---------------------------------------------------------------------------

_CORRELATE_FINDINGS_NAME = "correlate_findings"


def harvest_hypotheses(
    diagnostician_loop: LoopResult,
    finding_store: FindingStore,
) -> list[RootCauseHypothesis]:
    """Harvest `RootCauseHypothesis` objects from the diagnosis loop (design D-2).

    Iterates the successful ``correlate_findings`` invocations (``output is not
    None``), reads ``description`` / ``confidence`` / ``suggested_actions`` /
    ``supporting_findings`` (ordinal labels) from ``inv.input``, and resolves the
    labels to **real** ``Finding.id`` values via ``finding_store.resolve_label``.

    The label → real-id resolution happens HERE (orchestration layer), not in the
    handler: the handler only hit-checks (a dangling label never reaches a
    successful invocation — it becomes an error envelope), so every label on a
    successful invocation is guaranteed present in the store. A ``None`` resolve
    would therefore be a programming error, so it fails loud rather than silently
    dropping the reference.
    """
    hypotheses: list[RootCauseHypothesis] = []
    for inv in diagnostician_loop.tool_invocations:
        if inv.tool_name != _CORRELATE_FINDINGS_NAME or inv.output is None:
            continue
        # inv.input is the model's raw args; validate it back into the typed
        # schema so field access is checked (the schema is self-consistent —
        # a validation failure here is a code bug, fail loud).
        args = CorrelateFindingsInput.model_validate(inv.input)
        real_ids: list[str] = []
        for label in args.supporting_findings:
            real_id = finding_store.resolve_label(label)
            if real_id is None:
                raise ValueError(
                    f"harvest_hypotheses: label {label!r} on a successful "
                    "correlate_findings invocation is absent from the finding-store; "
                    "the handler hit-check should have rejected it"
                )
            real_ids.append(real_id)
        hypotheses.append(
            RootCauseHypothesis(
                description=args.description,
                confidence=args.confidence,
                supporting_findings=real_ids,
                suggested_actions=list(args.suggested_actions),
            )
        )
    return hypotheses


# Diagnostician terminal statuses that map to a same-named ``ReportStatus`` when
# the Planner succeeded (design D-5, Planner=ok row).
_SAME_NAME_DIAG_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "degraded_rate_limited",
        "degraded_token_budget",
        "degraded_max_turns",
        "degraded_no_planner",
        "empty_response",
    }
)

# Planner degraded statuses that skip diagnosis and pass through verbatim
# (design D-5, Planner-degraded rows). All have a same-named ``ReportStatus``.
_PLANNER_PASSTHROUGH_STATUSES: frozenset[str] = frozenset(
    {
        "degraded_rate_limited",
        "degraded_token_budget",
        "degraded_max_turns",
        "degraded_no_planner",
        "empty_response",
    }
)


def reconcile_status(
    planner_status: _TerminalStatus,
    diagnostician_status: _TerminalStatus | None,
) -> ReportStatus:
    """Reconcile the two loops' terminal statuses into one ``ReportStatus`` (D-5).

    Pure mapping function. ``diagnostician_status`` is ``None`` when the diagnosis
    stage was skipped (the Planner degraded). This path never produces ``partial``
    (no ``InspectorResult`` source).

    Contract — caller MUST NOT pass ``planner_status="failed_api_unavailable"``:
    that case yields NO ``DiagnosticianResult`` at all (no corresponding
    ``ReportStatus``; the CLI no-result path handles it). Passing it here raises
    ``ValueError`` rather than fabricating a status.
    """
    if planner_status == "failed_api_unavailable":
        raise ValueError(
            "reconcile_status: planner_status=failed_api_unavailable yields no "
            "DiagnosticianResult; the CLI no-result path handles it (design D-5)"
        )

    if planner_status != "ok":
        # Planner degraded → diagnosis skipped, status passes through verbatim.
        if planner_status in _PLANNER_PASSTHROUGH_STATUSES:
            return ReportStatus(planner_status)
        # Unreachable: _TerminalStatus minus {ok, failed_api_unavailable} is
        # exactly the passthrough set; an else would defend an impossible branch.
        raise ValueError(f"reconcile_status: unexpected planner_status {planner_status!r}")

    # Planner ok → take the Diagnostician's mapped value.
    if diagnostician_status is None:
        raise ValueError(
            "reconcile_status: planner_status=ok requires a diagnostician_status "
            "(diagnosis must have run when the Planner succeeded)"
        )
    if diagnostician_status == "failed_api_unavailable":
        # Diagnostician unreachable before any tool call. Planner findings are in
        # hand — never discard them over the Diagnostician's network blip.
        return ReportStatus.DEGRADED_NO_PLANNER
    if diagnostician_status in _SAME_NAME_DIAG_STATUSES:
        return ReportStatus(diagnostician_status)
    # Unreachable: the diagnostician _TerminalStatus set is the same-name set
    # plus failed_api_unavailable, both handled above.
    raise ValueError(f"reconcile_status: unexpected diagnostician_status {diagnostician_status!r}")


async def run_diagnosis(
    planner_result: PlannerResult,
    seeded_findings: list[SeededFinding],
    finding_store: FindingStore,
    diagnostician_agent_factory: Callable[[], DiagnosticianAgent],
    *,
    observer: LoopObserver | None = None,
) -> DiagnosticianResult:
    """Run (or skip) diagnosis and assemble the ``DiagnosticianResult`` (D-5).

    Control-flow seam (so the "Planner degraded → don't launch diagnosis" rule is
    unit-testable, not buried in the CLI): when the Planner degraded, the
    ``DiagnosticianAgent`` is NEVER invoked (``diagnostician_loop=None``), the
    status passes through, and the Planner's already-harvested findings are kept.
    Otherwise diagnosis runs, hypotheses are harvested, and status is reconciled.

    Caller MUST NOT pass a Planner result with
    ``terminal_status="failed_api_unavailable"``: that yields no result and is
    handled by the CLI no-result path (``reconcile_status`` raises if it leaks
    here). ``seeded_findings`` are the Planner findings already stamped and seeded
    into ``finding_store`` by the caller, paired with the labels ``seed`` returned
    (the store stays the single label authority). They drive the diagnosis loop's
    first user message; ``finding_store`` is shared so ``request_more_inspection``
    additions land in the same store the harvest and snapshot read.

    ``diagnostician_agent_factory`` builds the ``DiagnosticianAgent`` lazily: it is
    called **only** on the Planner-ok path (right before the loop runs), so the
    degraded path constructs no agent / registry at all (zero factory calls). This
    keeps the "don't launch diagnosis when the Planner degraded" decision the sole
    authority for whether the agent is even assembled.
    """
    planner_status = planner_result.loop_result.terminal_status

    if planner_status != "ok":
        # Planner degraded: skip diagnosis entirely (do NOT build/call the agent).
        return DiagnosticianResult(
            narrative="",
            findings=[item.finding for item in seeded_findings],
            hypotheses=[],
            status=reconcile_status(planner_status, None),
            planner_result=planner_result,
            diagnostician_loop=None,
        )

    diagnostician_agent = diagnostician_agent_factory()
    diagnostician_loop = await diagnostician_agent.run(
        planner_result.intent, seeded_findings, observer=observer
    )
    hypotheses = harvest_hypotheses(diagnostician_loop, finding_store)
    return DiagnosticianResult(
        narrative=diagnostician_loop.final_text,
        findings=finding_store.snapshot(),
        hypotheses=hypotheses,
        status=reconcile_status(planner_status, diagnostician_loop.terminal_status),
        planner_result=planner_result,
        diagnostician_loop=diagnostician_loop,
    )
