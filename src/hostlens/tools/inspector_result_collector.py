"""Per-run `InspectorResultCollector` — the complete-`InspectorResult` sink.

The `run_inspector` wire projection (`RunInspectorOutput`) deliberately strips
`status` / `version` / `duration_seconds` off the runner's `InspectorResult`
to keep the LLM-facing tool_result byte-stable (cassette matching). But the
orchestration layer needs those stripped fields to assemble a faithful
first-class `Report` via `Report.from_inspector_results`. This collector is the
side channel that carries the **complete** `InspectorResult` objects out of the
tool handlers without touching the wire.

It is the `InspectorResult` analogue of `FindingStore` (design D-1): a per-run,
mutable, **non-module-global** container, injected into the `run_inspector` /
`request_more_inspection` handlers via closure (never via `ToolContext` — ADR-008
locks its six fields). The orchestration layer holds the same instance across
the Planner loop and the Diagnostician loop, then `snapshot()`s the full set
after both loops finish.

Like `FindingStore`, this container is intentionally synchronous: under the
loop's single-threaded asyncio dispatch, handler appends never interleave across
an `await`, so no lock is needed.
"""

from __future__ import annotations

from hostlens.inspectors.result import InspectorResult

__all__ = ["InspectorResultCollector"]


class InspectorResultCollector:
    """Per-run, insertion-ordered sink of complete `InspectorResult` objects.

    Construct one instance per `--intent` run, inject it into the
    `run_inspector` / `request_more_inspection` handlers via closure, then
    `snapshot()` the full set after the loops finish. Never a module-global
    singleton (CLAUDE.md §6 / spec §需求).
    """

    def __init__(self) -> None:
        # Insertion order is append order (Planner-phase results first, then
        # request_more_inspection supplements), so iteration is deterministic.
        self._results: list[InspectorResult] = []

    def append(self, result: InspectorResult) -> None:
        """Append one complete `InspectorResult` (the runner's object itself).

        Callers MUST pass the `InspectorRunner.run(...)` return value — which
        carries real `status` / `version` / `duration_seconds` / `findings` —
        and never the wire-projected `RunInspectorOutput` (which strips them).
        Both ok and non-ok results are appended: a non-ok `InspectorResult`
        still carries real status/version and belongs in the assembled Report.
        """
        self._results.append(result)

    def snapshot(self) -> list[InspectorResult]:
        """Return an insertion-ordered copy of every appended `InspectorResult`."""
        return list(self._results)
