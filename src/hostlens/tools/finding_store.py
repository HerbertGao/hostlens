"""Per-run `FindingStore` — the label↔finding registry for the Diagnostician.

The Diagnostician references findings by short ordinal **labels** (`F1`, `F2`,
…) rather than the 16-hex `Finding.id` (design D-9). `FindingStore` is the
per-run, mutable, non-module-global container that maps each label to its
stamped `Finding`, injected into the `correlate_findings` /
`request_more_inspection` handlers via closure (design D-8, mirroring the
`run_inspector` clock-binding precedent).

Why the **unique key is the label, not the id**: `compute_finding_id`
deliberately excludes severity (`reporting.models`), and Planner harvest does
not deduplicate, so two findings with the same
`(inspector_name, version, message)` but different severities receive the
**same** real `id`. Keying by id would let the second overwrite the first and
leave a label dangling. Keying by label guarantees one label ↔ exactly one
`Finding` object, while `resolve(label) → real_id` may legitimately map several
labels onto the same real id.

This container is intentionally synchronous: under the loop's single-threaded
asyncio dispatch, handler reads/writes never interleave across an `await`, so
`append` / `resolve` need no lock (design D-8).
"""

from __future__ import annotations

from hostlens.reporting.models import Finding

__all__ = ["FindingStore"]


class FindingStore:
    """Per-run, label-keyed registry of stamped findings.

    Construct one instance per `--intent` run, seed it with the Planner's
    stamped findings, then pass it into the Diagnostician tool handlers. Never
    a module-global singleton (CLAUDE.md §6 / spec §需求).
    """

    _LABEL_PREFIX = "F"

    def __init__(self) -> None:
        # Insertion-ordered label → stamped Finding. Insertion order is the
        # label-assignment order (F1, F2, …), so iteration is deterministic.
        self._by_label: dict[str, Finding] = {}

    def seed(self, findings: list[Finding]) -> list[str]:
        """Seed the store with the Planner's stamped findings.

        Assigns each a fresh unique label in order and returns the assigned
        labels (parallel to the input list) so the orchestration layer can
        render them in the first user message. Each finding **must** already
        carry a real `id` (orchestration stamps before seeding).
        """
        return [self._add(finding) for finding in findings]

    def append(self, finding: Finding) -> str:
        """Append one freshly-collected (already stamped) finding.

        Allocates and returns a new unique label. Used by
        `request_more_inspection` for findings collected mid-diagnosis.
        """
        return self._add(finding)

    def resolve_label(self, label: str) -> str | None:
        """Resolve an ordinal label to its finding's real `id`.

        Returns ``None`` when the label is not in the store (a dangling
        reference — the handler turns this into an error envelope; design D-8).
        Multiple labels may resolve to the same real id (the severity-collision
        case) — that is correct, not an overwrite.
        """
        finding = self._by_label.get(label)
        if finding is None:
            return None
        # ids are filled before any finding enters the store, so a None id is a
        # programming error in the orchestration layer, not a runtime branch.
        if finding.id is None:
            raise ValueError(
                "FindingStore.resolve_label: finding id must be filled before it "
                "enters the store; a None id is an orchestration-layer programming error"
            )
        return finding.id

    def contains(self, label: str) -> bool:
        """Whether ``label`` has been assigned in this run."""
        return label in self._by_label

    def snapshot(self) -> list[Finding]:
        """Full label-ordered snapshot of every stamped finding in the store.

        This is the canonical finding set for `DiagnosticianResult.findings`
        (Planner findings + every `request_more_inspection` addition).
        """
        return list(self._by_label.values())

    def _add(self, finding: Finding) -> str:
        label = f"{self._LABEL_PREFIX}{len(self._by_label) + 1}"
        self._by_label[label] = finding
        return label
