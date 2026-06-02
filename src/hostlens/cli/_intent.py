"""``hostlens inspect --intent`` helpers — Planner assembly, Rich Live observer,
and ``PlannerResult`` rendering.

Spec: ``openspec/changes/add-intent-cli/specs/inspect-cli-command/spec.md``.

The Agent layer (``agent/``) stays Rich-free so it remains a pure, readable
demonstration of the hand-written loop (CLAUDE.md §4.1). Rich only enters at the
CLI boundary, so ``RichLiveObserver`` — the live progress sink that implements
``LoopObserver`` — lives here, not in ``agent/``.

Three concerns, three helpers:

- ``RichLiveObserver`` — renders the per-turn / per-tool progress tree to
  **stderr** (so stdout stays a clean report stream). ``on_event`` is wrapped in
  a blanket ``try/except`` that swallows rendering errors (degrading to silence)
  because the loop calls observers with no defensive try/except (design D-2/D-7):
  isolating a Rich glitch is the observer's own responsibility, and a render
  failure must never fail-loud the whole inspection.
- ``build_planner`` — wires ``create_backend`` + a default-tools ``ToolRegistry``
  + a ``ToolContext`` factory into a ``PlannerAgent``. The backend reaches only
  the ``PlannerAgent`` (→ ``AgentLoop``), never the ``ToolContext`` (ADR-008).
  Retained for ``demo run`` / cassette recording (Planner-only flows).
- ``run_intent_diagnosis`` — the ``--intent`` orchestration seam: calls
  ``create_backend`` ONCE, runs the Planner, id-stamps + seeds its findings,
  then runs the Diagnostician (reusing the same backend + a restricted
  diagnostician ``ToolRegistry``). Returns ``None`` on the Planner
  ``failed_api_unavailable`` no-result path.
- ``render_planner_result`` — projects a ``PlannerResult`` to md / json (still
  used by ``demo run``).
- ``render_diagnostician_result`` — projects a ``DiagnosticianResult`` to
  md / json for the ``--intent`` path.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.tree import Tree

from hostlens.agent.backend import create_backend
from hostlens.agent.diagnostician import (
    DiagnosticianAgent,
    DiagnosticianResult,
    SeededFinding,
    run_diagnosis,
)
from hostlens.agent.events import (
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.core.redact import redact_text
from hostlens.reporting._redact import redact_diagnostician_result_for_render
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.diagnostician_tools import (
    register_diagnostician_tools,
    stamp_planner_findings,
)
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopEvent, LoopObserver
    from hostlens.core.config import Settings
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.targets.registry import TargetRegistry

__all__ = [
    "RichLiveObserver",
    "build_planner",
    "render_diagnostician_result",
    "render_planner_result",
    "run_intent_diagnosis",
]


# --------------------------------------------------------------------------- #
# RichLiveObserver
# --------------------------------------------------------------------------- #


class RichLiveObserver:
    """Live progress sink implementing ``LoopObserver`` (design D-7).

    Maintains a Rich ``Tree`` of turns → tool calls (ok / err) refreshed
    incrementally via ``Live`` bound to a **stderr** console, so the rendered
    report on stdout is never contaminated. Under a non-TTY stderr (e.g.
    pytest's ``CliRunner`` or a pipe) Rich auto-degrades to plain line output;
    we never force a TTY.

    ``on_event`` swallows every exception (design D-2/D-7): the loop emits
    events with no defensive try/except, so isolating a render glitch is this
    observer's own boundary responsibility — it must never raise back into the
    loop.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console if console is not None else Console(stderr=True)
        self._tree = Tree("agent run")
        self._live: Live | None = None
        # Per-turn Tree nodes, keyed by 1-based turn, so a tool node can attach
        # under the turn that dispatched it even with out-of-order parallel
        # ToolStarted events (loop guarantees turn-level order, not a total order).
        self._turn_nodes: dict[int, Tree] = {}
        self._tool_nodes: dict[str, Tree] = {}

    def on_event(self, event: LoopEvent) -> None:
        # design D-2/D-7: the loop emits events with no defensive try/except, so
        # a Rich render glitch must never fail-loud the inspection. Degrade to
        # silence — progress is non-essential UI.
        with contextlib.suppress(Exception):
            self._handle(event)

    def _handle(self, event: LoopEvent) -> None:
        match event:
            case TurnStarted(turn=turn):
                self._ensure_live()
                node = self._tree.add(f"turn {turn}")
                self._turn_nodes[turn] = node
                self._refresh()
            case ModelResponded(turn=turn, stop_reason=stop_reason, text=text):
                parent = self._turn_nodes.get(turn, self._tree)
                summary = f"model: stop_reason={stop_reason}"
                if text:
                    # Redact before previewing: the model narrative may restate a
                    # secret-bearing finding, and this is the only stderr surface
                    # that echoes free model text (tool output is never printed).
                    summary = f"{summary} — {_one_line(redact_text(text))}"
                parent.add(summary)
                self._refresh()
            case ToolStarted(turn=turn, tool_name=tool_name, tool_use_id=tool_use_id):
                parent = self._turn_nodes.get(turn, self._tree)
                # Redact the tool name: the loop emits ToolStarted *before* the
                # white-list check, so a model-hallucinated name (model-controlled
                # free text) reaches stderr. No-op for legitimate identifiers.
                node = parent.add(f"tool {redact_text(tool_name)} … running")
                self._tool_nodes[tool_use_id] = node
                self._refresh()
            case ToolCompleted(invocation=invocation):
                started_node = self._tool_nodes.get(invocation.tool_use_id)
                outcome = "err" if invocation.error is not None else "ok"
                label = f"tool {redact_text(invocation.tool_name)} … {outcome}"
                if started_node is not None:
                    started_node.label = label
                else:
                    self._tree.add(label)
                self._refresh()
            case RunFinalized(terminal_status=terminal_status, turns=turns):
                self._tree.add(f"finalized: {terminal_status} ({turns} turns)")
                self._refresh()
                self._stop()

    def _ensure_live(self) -> None:
        # Lazy start on the first event so a no-op run never opens a Live region.
        if self._live is None:
            self._live = Live(self._tree, console=self._console, refresh_per_second=8)
            self._live.start()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._tree)

    def close(self) -> None:
        # fail-loud loop paths (ToolPolicyViolation 等) and CLI exception paths
        # never emit RunFinalized, so the CLI calls this in a finally to ensure
        # the Live region is torn down. Idempotent: _stop no-ops when Live is
        # already stopped or never started.
        self._stop()

    def _stop(self) -> None:
        # Best-effort teardown: clear ``_live`` FIRST so the observer is left in
        # a stopped state even if ``Live.stop()`` raises, then suppress the stop
        # error. ``close()`` runs in the CLI's ``finally``; a raising teardown
        # would mask the original planner exception per Python finally semantics.
        live, self._live = self._live, None
        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()


def _one_line(text: str, *, limit: int = 120) -> str:
    """Collapse ``text`` to a single trimmed line for the progress tree."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# PlannerAgent assembly
# --------------------------------------------------------------------------- #


def build_planner(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> PlannerAgent:
    """Assemble a ``PlannerAgent`` for the ``--intent`` path (design D-5).

    ``create_backend`` raises ``ConfigError`` when no backend is configured;
    the caller maps that to exit 3. The ``context_factory`` builds a fresh
    ``ToolContext`` (with a fresh ``asyncio.Event`` cancel token) per dispatch.
    The backend is handed only to ``PlannerAgent`` — never into the
    ``ToolContext`` — so a tool handler can never reach the LLM (ADR-008).
    """
    backend = create_backend(settings)

    registry = ToolRegistry()
    register_default_tools(registry)

    context_factory = _make_context_factory(settings, target_registry, inspector_registry, logger)

    return PlannerAgent(backend, registry, settings, context_factory)


def _make_context_factory(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> Callable[[], ToolContext]:
    """Build a ``ToolContext`` factory closure (shared by Planner + Diagnostician).

    Each call produces a fresh ``ToolContext`` with its own ``asyncio.Event``
    cancel token. The backend is deliberately NOT a parameter here — it is never
    threaded into a ``ToolContext`` (ADR-008), so a tool handler can never reach
    the LLM. The Planner and Diagnostician share the SAME ``inspector_registry``
    instance so id stamping reads the same versions the Planner ran against
    (design D-3, no TOCTOU skew).
    """

    def context_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return context_factory


# --------------------------------------------------------------------------- #
# Planner → Diagnostician orchestration
# --------------------------------------------------------------------------- #


async def run_intent_diagnosis(
    settings: Settings,
    target: str,
    intent: str,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
    *,
    observer: LoopObserver | None = None,
) -> DiagnosticianResult | None:
    """Assemble + run Planner → id-stamp → Diagnostician (design D-5, spec §需求).

    ``create_backend(settings)`` is called **exactly once** here; the SAME
    backend instance reaches both the ``PlannerAgent`` and the
    ``DiagnosticianAgent`` (the only configuration failure point is before the
    Planner runs — backend not configured raises ``ConfigError`` at assembly).
    The backend is handed only to the two agents' loops, never into any
    ``ToolContext`` (ADR-008).

    Both stages share the SAME ``inspector_registry`` instance — the Planner
    runs against it, and ``stamp_planner_findings`` re-looks-up versions against
    it (design D-3).

    Returns ``None`` for the **no-result** path: when the Planner finalizes
    ``failed_api_unavailable`` there is no ``DiagnosticianResult`` to produce
    (``reconcile_status`` would raise), and the CLI maps ``None`` to the
    no-result degradation (stderr note + empty stdout + exit 2). Every other
    path returns a ``DiagnosticianResult`` (diagnosis ran, or was skipped on a
    Planner degrade — ``diagnostician_loop=None`` in the latter case).

    ``observer`` is passed straight through to BOTH agent runs, so the Planner
    and Diagnostician progress trees both stream to the CLI's stderr sink.
    """
    backend: LLMBackend = create_backend(settings)
    context_factory = _make_context_factory(settings, target_registry, inspector_registry, logger)

    planner_registry = ToolRegistry()
    register_default_tools(planner_registry)
    planner = PlannerAgent(backend, planner_registry, settings, context_factory)

    planner_result = await planner.run(intent, observer=observer)

    if planner_result.loop_result.terminal_status == "failed_api_unavailable":
        # No-result path: the Planner never reached the API. There is nothing to
        # diagnose and reconcile_status would raise on this status — return None
        # so the CLI emits a one-line degrade note with empty stdout (exit 2).
        return None

    # id-stamp the Planner's findings (fail-loud if an inspector was unloaded —
    # the CLI boundary wraps the bubbled InspectorError as ``internal: ...``).
    stamped = stamp_planner_findings(planner_result.loop_result, inspector_registry)

    store = FindingStore()
    labels = store.seed(stamped)
    seeded = [
        SeededFinding(label=label, finding=finding)
        for label, finding in zip(labels, stamped, strict=True)
    ]

    def _make_diag_agent() -> DiagnosticianAgent:
        # Built lazily by run_diagnosis only on the Planner-ok path, so a Planner
        # degrade constructs no registry / agent at all (zero factory calls).
        diag_registry = ToolRegistry()
        register_diagnostician_tools(
            diag_registry, finding_store=store, target_name=target, clock=None
        )
        return DiagnosticianAgent(backend, diag_registry, settings, context_factory)

    return await run_diagnosis(planner_result, seeded, store, _make_diag_agent, observer=observer)


# --------------------------------------------------------------------------- #
# PlannerResult rendering
# --------------------------------------------------------------------------- #


def render_planner_result(result: PlannerResult, fmt: str) -> str:
    """Render a ``PlannerResult`` to ``md`` or ``json`` (design D-6).

    json: the verbatim ``PlannerResult`` serialization (narrative / findings /
    loop_result / intent) so downstream can parse it. md: the narrative
    (already markdown) + a findings summary + one telemetry line. Findings come
    straight from already-redacted ``Finding`` objects; this function adds no
    re-derivation and leaks no env vars (CLAUDE.md §4.4 / §7).
    """
    if fmt == "json":
        return result.model_dump_json(indent=2)

    parts: list[str] = [result.narrative.rstrip("\n")]

    if result.findings:
        parts.append("")
        parts.append("## Findings")
        for finding in result.findings:
            tags = f" [{', '.join(finding.tags)}]" if finding.tags else ""
            parts.append(f"- {finding.severity}: {finding.message}{tags}")

    loop = result.loop_result
    usage = loop.usage_totals
    parts.append("")
    parts.append(
        f"turns={loop.turns} status={loop.terminal_status} "
        f"tokens_in={usage.input_tokens} tokens_out={usage.output_tokens}"
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# DiagnosticianResult rendering
# --------------------------------------------------------------------------- #


def render_diagnostician_result(result: DiagnosticianResult, fmt: str) -> str:
    """Render a ``DiagnosticianResult`` to ``md`` or ``json`` (design D-7, spec §需求).

    json: the verbatim ``DiagnosticianResult`` serialization (narrative /
    findings(top-level, authoritative, id-bearing) / hypotheses / status /
    planner_result(nested findings are the unstamped debug originals) /
    diagnostician_loop(may be null)) so downstream can ``model_validate_json``
    round-trip it; downstream MUST read the **top-level** ``findings``.

    Before rendering, the whole ``DiagnosticianResult`` graph is passed through
    the ``core/redact`` boundary via ``redact_diagnostician_result_for_render``
    (parity with the ``Report`` render path): every string field — top-level
    findings / hypotheses / narrative AND the nested ``planner_result`` /
    ``diagnostician_loop`` loop telemetry (whose ``tool_invocations`` carry raw
    inspector output) — is masked, so neither md nor json leaks a secret
    pattern. md and json both render from this redacted copy.

    md: the diagnosis narrative + a ``## Findings`` summary (from the top-level
    canonical set) + a ``## 根因假设`` section + one telemetry line. Three
    tolerances the spec mandates:

    - **Empty narrative** (degraded paths carry ``final_text=""``): render no
      narrative heading at all — never an empty title (spec §场景:降级致 narrative
      为空时渲染容忍).
    - **No hypotheses**: emit the ``_暂无根因假设_`` placeholder, reusing the
      exact wording/style from ``reporting.render_markdown`` (spec §场景:无根因
      假设时显示占位).
    - **No findings**: skip the ``## Findings`` heading; narrative + 根因假设
      placeholder + telemetry still render.
    """
    result = redact_diagnostician_result_for_render(result)

    if fmt == "json":
        return result.model_dump_json(indent=2)

    parts: list[str] = []

    narrative = result.narrative.rstrip("\n")
    if narrative:
        parts.append(narrative)

    if result.findings:
        if parts:
            parts.append("")
        parts.append("## Findings")
        for finding in result.findings:
            tags = f" [{', '.join(finding.tags)}]" if finding.tags else ""
            parts.append(f"- {finding.severity}: {finding.message}{tags}")

    if parts:
        parts.append("")
    parts.append("## 根因假设")
    if not result.hypotheses:
        parts.append("_暂无根因假设_")
    else:
        for h in result.hypotheses:
            parts.append("")
            parts.append(f"### {h.description}")
            parts.append(f"- **Confidence:** {h.confidence}")
            if h.supporting_findings:
                parts.append(f"- **Supporting findings:** {', '.join(h.supporting_findings)}")
            if h.suggested_actions:
                parts.append("- **Suggested actions:**")
                for action in h.suggested_actions:
                    parts.append(f"  - {action}")

    # Telemetry: prefer the diagnosis loop's counters. When the diagnosis stage
    # was skipped (Planner degraded → diagnostician_loop is None), there is no
    # diagnosis loop to count, so report the Planner loop's telemetry and note
    # the skip. ``status`` is always the reconciled DiagnosticianResult.status.
    parts.append("")
    diag_loop = result.diagnostician_loop
    if diag_loop is not None:
        usage = diag_loop.usage_totals
        parts.append(
            f"turns={diag_loop.turns} status={result.status} "
            f"tokens_in={usage.input_tokens} tokens_out={usage.output_tokens}"
        )
    else:
        planner_loop = result.planner_result.loop_result
        usage = planner_loop.usage_totals
        parts.append(
            f"turns={planner_loop.turns} status={result.status} "
            f"tokens_in={usage.input_tokens} tokens_out={usage.output_tokens} "
            "(diagnosis skipped: planner degraded)"
        )
    return "\n".join(parts)
