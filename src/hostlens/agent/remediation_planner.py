"""Remediation Planner Agent — proposes (does NOT execute) controlled fixes.

The Diagnostician (M3) consumes the Planner's stamped findings and produces
`list[RootCauseHypothesis]` + a diagnostic narrative. This module introduces the
**third** `AgentLoop`-backed Agent (M9 P1b, design Decision 1): it consumes the
Diagnostician's canonical findings (stable ids) + root-cause hypotheses + the
run's target name and produces `list[RemediationPlan]` (P1a-validated, **not
executed**), aggregated into `RemediationPlannerResult`.

It is **almost a transliteration** of `diagnostician.py`: swap "produce a
hypothesis" for "produce a plan" and `correlate_findings` for
`propose_remediation`. The two deliberate divergences (design Decision 5):

- the agent's first user message also carries the root-cause **hypotheses** (the
  Diagnostician's first message carried only findings), and
- there is **no** `reconcile_status`: P1b is a single loop with no second loop to
  reconcile, so `RemediationPlannerResult.status` is the planner loop's
  `terminal_status` **passed through verbatim**.

This module hosts the `RemediationPlannerAgent` class (assembler over a single
`AgentLoop`, mirroring `DiagnosticianAgent`) plus two orchestration-layer
helpers: `harvest_plans` (turns successful `propose_remediation` invocations into
`RemediationPlan` objects with real ids + stamped target) and
`run_remediation_planning` (the control-flow seam that only launches the planner
when diagnosis succeeded and produced findings).
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from hostlens.agent.diagnostician import SeededFinding, _render_findings_block
from hostlens.agent.loop import AgentLoop, LoopResult, _TerminalStatus
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ConfigError
from hostlens.remediation.models import RemediationPlan
from hostlens.tools.schemas.propose_remediation import ProposeRemediationInput

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopObserver
    from hostlens.core.config import Settings
    from hostlens.reporting.models import RootCauseHypothesis
    from hostlens.tools.base import ToolContext
    from hostlens.tools.finding_store import FindingStore
    from hostlens.tools.registry import ToolRegistry

__all__ = [
    "RemediationPlannerAgent",
    "RemediationPlannerResult",
    "harvest_plans",
    "run_remediation_planning",
]


# Placeholder in ``remediation_planner_system.md`` replaced with the rendered
# tool overview. A single ``str.replace`` keeps the Agent layer free of a
# template engine, mirroring ``diagnostician.py`` (design Decision 6).
_TOOL_OVERVIEW_PLACEHOLDER = "{tool_overview}"

# Package + resource name of the external prompt template (CLAUDE.md §7).
_PROMPT_PACKAGE = "hostlens.agent.prompts"
_PROMPT_RESOURCE = "remediation_planner_system.md"


class RemediationPlannerResult(BaseModel):
    """Aggregated result of one remediation-planning path (design Decision 5).

    Field semantics:

    - ``plans`` — the `RemediationPlan` objects harvested from successful
      `propose_remediation` invocations; each carries a **real** `Finding.id`
      and the run's stamped `target_name`. **May be empty** (the model emitted
      no `propose_remediation`, or planning was skipped upstream) — an empty
      list is a normal outcome, not a crash (spec §需求:无方案产出不视为崩溃).
    - ``status`` — the planner loop's `terminal_status` **passed through
      verbatim** (design Decision 5). P1b is a single loop with no second loop
      to reconcile, so there is deliberately **no** `reconcile_status` mapping
      here. When the planner is skipped upstream (diagnosis not ok / no
      findings), the orchestration layer stamps the status directly.
    - ``planner_loop`` — planner loop telemetry; ``None`` **iff** the planner
      stage was skipped (diagnosis not ok / findings empty). Any path where the
      planner loop actually ran carries a non-`None` `LoopResult`.
    """

    model_config = ConfigDict(frozen=True)

    plans: list[RemediationPlan]
    status: _TerminalStatus
    planner_loop: LoopResult | None


# ``from __future__ import annotations`` defers ``status``'s ``_TerminalStatus``
# annotation to a string; rebuild now (the name is in module scope) so the model
# is fully defined at import, never lazily at first construction.
RemediationPlannerResult.model_rebuild()


def _render_hypotheses_block(hypotheses: list[RootCauseHypothesis]) -> str:
    """Render the root-cause hypotheses as part of the planner's first message.

    Each hypothesis is one line carrying its confidence, the ordinal labels of
    its supporting findings (NOT the real ids — the hypotheses arrive with
    `supporting_findings` already resolved to real `Finding.id`, but the planner
    references findings by label, so this block renders the description +
    confidence for context and keeps id mention out), and its description. This
    block lands in messages (never in the byte-stable system prompt — design
    Decision 6).
    """
    if not hypotheses:
        return "Diagnostician 没有给出任何根因假设。"
    lines: list[str] = []
    for idx, h in enumerate(hypotheses, start=1):
        actions = "; ".join(h.suggested_actions) if h.suggested_actions else "-"
        lines.append(
            f"- [H{idx}] confidence={h.confidence} :: {h.description} (建议方向: {actions})"
        )
    return "\n".join(lines)


class RemediationPlannerAgent:
    """Assembles a system prompt + restricted tool set + backend into one loop.

    Structurally identical to ``DiagnosticianAgent`` (design Decision 1): it
    wraps a single ``AgentLoop`` and renders the external
    ``remediation_planner_system.md`` system prompt at construction. The backend
    reaches ONLY the loop — never the ``context_factory``'s ``ToolContext``
    (ADR-008 / CLAUDE.md §7). A missing/unreadable template fails loud
    (``ConfigError``) at construction, never at run time.
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
        # injection, silently disabling prompt caching (design Decision 6,
        # mirroring diagnostician.py).
        rendered = self._render_system_prompt(registry, prompt_path)
        system: list[dict[str, Any]] = [{"type": "text", "text": rendered}]

        adapter = ToolsAdapter(registry, context_factory)
        self._loop = AgentLoop(backend, adapter, settings, system=system)

    @staticmethod
    def _render_system_prompt(
        registry: ToolRegistry,
        prompt_path: str | None,
    ) -> str:
        """Load the prompt md and substitute the tool-overview placeholder.

        A missing/unreadable template fails loud (``ConfigError``) at
        construction rather than silently degrading to an empty prompt (design
        Decision 1 / spec §场景:系统提示模板缺失构造期失败).
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
                "remediation planner prompt template not found",
                kind="remediation_planner_prompt_missing",
                original=exc,
            ) from exc

        # ``list_for("agent")`` is sorted by spec.name ascending, so the overview
        # is byte-stable across runs for a fixed tool set — the prompt-caching
        # prerequisite (CLAUDE.md §4.8 / design Decision 6).
        overview = "\n".join(
            f"- {spec.name}: {spec.agent_description}" for spec in registry.list_for("agent")
        )
        return template.replace(_TOOL_OVERVIEW_PLACEHOLDER, overview)

    async def run(
        self,
        intent: str,
        findings: list[SeededFinding],
        hypotheses: list[RootCauseHypothesis],
        target_name: str,
        *,
        observer: LoopObserver | None = None,
    ) -> LoopResult:
        """Drive the planning loop over ``intent`` + labeled findings + hypotheses.

        ``AgentLoop.run`` takes a single ``intent: str`` and builds the first
        user message itself; its signature is NOT changed here. So the intent,
        the labeled-findings block, the hypotheses block, and the ``target_name``
        are concatenated into that one string — putting all per-run dynamic
        inputs into the loop's messages, NEVER into the byte-stable system
        prompt (design Decision 6 / spec §需求:finding/假设/target 必须进
        messages、禁止进 system).

        Returns the raw ``LoopResult``; the orchestration layer harvests plans
        from it (``harvest_plans``). ``observer`` is passed straight through to
        ``AgentLoop.run`` — the planner never interprets ``LoopEvent``.
        """
        findings_block = _render_findings_block(findings)
        hypotheses_block = _render_hypotheses_block(hypotheses)
        combined = (
            f"{intent}\n\n"
            f"## 本次作用的 target\n\n{target_name}\n\n"
            f"## 已确认的 findings (按序号标签)\n\n{findings_block}\n\n"
            f"## Diagnostician 的根因假设\n\n{hypotheses_block}"
        )
        return await self._loop.run(combined, observer=observer)


# ---------------------------------------------------------------------------
# Orchestration-layer helpers (design Decision 5 harvest / control flow)
# ---------------------------------------------------------------------------

_PROPOSE_REMEDIATION_NAME = "propose_remediation"


def harvest_plans(
    loop: LoopResult,
    finding_store: FindingStore,
    target_name: str,
) -> list[RemediationPlan]:
    """Harvest `RemediationPlan` objects from the planner loop (design Decision 5).

    Iterates the successful ``propose_remediation`` invocations (``output is not
    None``), reads ``finding_label`` / ``rationale`` / ``estimated_duration_
    seconds`` / ``steps`` from ``inv.input``, resolves the ordinal label to a
    **real** ``Finding.id`` via ``finding_store.resolve_label``, and stamps the
    run's ``target_name`` — assembling a P1a ``RemediationPlan``.

    The label → real-id resolution happens HERE (orchestration layer), not in
    the handler: the handler only hit-checks (a dangling label never reaches a
    successful invocation — it becomes an error envelope), so every label on a
    successful invocation is guaranteed present in the store. A ``None`` resolve
    would therefore be a programming error, so it **fails loud** rather than
    silently dropping the reference (mirrors ``harvest_hypotheses`` — this is the
    OPPOSITE of "record-invalid-and-skip", which would be the architecture if the
    handler did not hit-check; this proposal does not take it).

    Plan construction is certain to succeed here: ``steps`` were already
    validated at emit time (``ToolsAdapter.dispatch`` ``model_validate`` enforced
    every P1a invariant), and ``finding_id`` / ``target_name`` are stamped
    non-blank by the orchestration layer — there is no P1a validation-failure
    path. Multiple labels resolving to the same real id are **not** deduplicated
    (inherits the Diagnostician same-id-no-overwrite semantics, design Decision
    3): two plans may legitimately share a ``finding_id``.
    """
    plans: list[RemediationPlan] = []
    for inv in loop.tool_invocations:
        if inv.tool_name != _PROPOSE_REMEDIATION_NAME or inv.output is None:
            continue
        # inv.input is the model's raw args; validate it back into the typed
        # schema so field access is checked (the schema is self-consistent —
        # a validation failure here is a code bug, fail loud).
        args = ProposeRemediationInput.model_validate(inv.input)
        real_id = finding_store.resolve_label(args.finding_label)
        if real_id is None:
            raise ValueError(
                f"harvest_plans: label {args.finding_label!r} on a successful "
                "propose_remediation invocation is absent from the finding-store; "
                "the handler hit-check should have rejected it"
            )
        plans.append(
            RemediationPlan(
                finding_id=real_id,
                target_name=target_name,
                rationale=args.rationale,
                steps=list(args.steps),
                estimated_duration_seconds=args.estimated_duration_seconds,
            )
        )
    return plans


async def run_remediation_planning(
    diagnosis_status: _TerminalStatus,
    findings: list[SeededFinding],
    finding_store: FindingStore,
    target_name: str,
    intent: str,
    planner_agent_factory: Callable[[], RemediationPlannerAgent],
    hypotheses: list[RootCauseHypothesis],
    *,
    observer: LoopObserver | None = None,
) -> RemediationPlannerResult:
    """Run (or skip) remediation planning and assemble the result (Decision 5).

    Control-flow seam (so the "don't plan unless diagnosis succeeded with
    findings" rule is unit-testable, not buried in the CLI): the planner loop is
    launched **only when ``diagnosis_status == "ok"`` AND ``findings`` is
    non-empty** — a mechanically-decidable predicate mirroring ``run_diagnosis``,
    with **no** severity/"worth-fixing" heuristic (that is a P2 concern; this
    proposal's non-goal). Otherwise no agent is built, no LLM call is made,
    ``plans`` is empty, ``planner_loop`` is ``None``, and ``status`` is the
    diagnosis status passed through.

    ``planner_agent_factory`` builds the ``RemediationPlannerAgent`` lazily: it
    is called **only** on the launch path (right before the loop runs), so the
    skip path constructs no agent / registry at all (zero factory calls).

    ``findings`` are the diagnosis-phase canonical findings already stamped and
    seeded into ``finding_store`` by the caller, paired with the labels ``seed``
    returned (the store stays the single label authority). They drive the planner
    loop's first user message; ``finding_store`` is shared so
    ``request_more_inspection`` additions land in the same store the harvest
    reads (design Decision 4). ``hypotheses`` are rendered into the same first
    message for planning context.
    """
    if diagnosis_status != "ok" or not findings:
        # Skip planning entirely (do NOT build/call the agent).
        return RemediationPlannerResult(
            plans=[],
            status=diagnosis_status,
            planner_loop=None,
        )

    planner_agent = planner_agent_factory()
    planner_loop = await planner_agent.run(
        intent, findings, hypotheses, target_name, observer=observer
    )
    plans = harvest_plans(planner_loop, finding_store, target_name)
    return RemediationPlannerResult(
        plans=plans,
        status=planner_loop.terminal_status,
        planner_loop=planner_loop,
    )
