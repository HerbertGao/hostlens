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
- ``render_planner_result`` — projects a ``PlannerResult`` to md / json.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.tree import Tree

from hostlens.agent.backend import create_backend
from hostlens.agent.events import (
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    import structlog

    from hostlens.agent.events import LoopEvent
    from hostlens.core.config import Settings
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.targets.registry import TargetRegistry

__all__ = ["RichLiveObserver", "build_planner", "render_planner_result"]


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
        # ToolStarted events (loop guarantees turn-level order, not全序).
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
                    summary = f"{summary} — {_one_line(text)}"
                parent.add(summary)
                self._refresh()
            case ToolStarted(turn=turn, tool_name=tool_name, tool_use_id=tool_use_id):
                parent = self._turn_nodes.get(turn, self._tree)
                node = parent.add(f"tool {tool_name} … running")
                self._tool_nodes[tool_use_id] = node
                self._refresh()
            case ToolCompleted(invocation=invocation):
                started_node = self._tool_nodes.get(invocation.tool_use_id)
                outcome = "err" if invocation.error is not None else "ok"
                label = f"tool {invocation.tool_name} … {outcome}"
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

    def context_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return PlannerAgent(backend, registry, settings, context_factory)


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
