"""Typed ``LoopEvent`` set + ``LoopObserver`` Protocol (add-intent-cli M2.7).

The Agent loop is the project's核心展示点 (CLAUDE.md §4.1): keeping it readable
means telemetry must not pollute the core control flow. These events are the
minimal UI-level observation surface (design D-1): frozen dataclasses (plain
process-internal value passing — no Pydantic validation/serialization cost) and
a single-method ``on_event`` Protocol so adding an event never breaks an
existing observer.

``ToolCompleted`` carries the loop's own ``ToolInvocation`` record verbatim; it
is referenced only at the type level (``TYPE_CHECKING``) because ``loop`` imports
this module, so importing ``ToolInvocation`` at runtime would be a circular
import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hostlens.agent.loop import ToolInvocation

__all__ = [
    "LoopEvent",
    "LoopObserver",
    "ModelResponded",
    "RunFinalized",
    "ToolCompleted",
    "ToolStarted",
    "TurnStarted",
]


@dataclass(frozen=True)
class TurnStarted:
    """A model call is about to be made for this (1-based) turn."""

    turn: int


@dataclass(frozen=True)
class ModelResponded:
    """A model response landed; ``text`` is display-only (empty on tool_use turns)."""

    turn: int
    stop_reason: str
    text: str


@dataclass(frozen=True)
class ToolStarted:
    """A ``tool_use`` block entered dispatch (emitted before any branch check)."""

    turn: int
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolCompleted:
    """A ``tool_use`` block produced its ``ToolInvocation`` (success or error)."""

    turn: int
    invocation: ToolInvocation


@dataclass(frozen=True)
class RunFinalized:
    """The run reached a terminal status and is about to return ``LoopResult``."""

    terminal_status: str
    turns: int


# PEP 604 union evaluated at module load → a usable runtime ``types.UnionType``
# value (Python 3.11+), not just a type-checker-only construct.
LoopEvent = TurnStarted | ModelResponded | ToolStarted | ToolCompleted | RunFinalized


@runtime_checkable
class LoopObserver(Protocol):
    """Synchronous, non-blocking sink for ``LoopEvent`` values.

    ``on_event`` MUST NOT raise: the loop calls it directly with no defensive
    try/except (design D-2), so isolating internal (e.g. rendering) errors is
    the observer's own responsibility.
    """

    def on_event(self, event: LoopEvent) -> None: ...
