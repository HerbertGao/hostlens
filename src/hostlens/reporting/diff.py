"""Regression diff engine — compares two `Report`s across runs.

`compute_diff(baseline, current)` matches findings by their
severity-agnostic `Finding.id` (see `compute_finding_id`) to produce
`added` / `resolved` / `changed_severity`, while guarding against
baseline pollution (per-target isolation, baseline status gate, schema
alignment, inspector version alignment).

`RegressionDiff` is the closed output shape. `diff_skipped_reason` is a
three-value closed set so CLI rendering never drifts; "no baseline
available" is *not* one of them — that case is handled by the CLI
emitting text rather than constructing a `RegressionDiff` at all.

Field-name note: the baseline reference field is `baseline_meta` (not
`baseline_ref`) to avoid colliding with `ReportMeta.baseline_ref` (the
report's own self-recorded baseline reference).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hostlens.reporting.models import (
    BaselineRef,
    Finding,
    Report,
    ReportMeta,
    RootCauseHypothesis,
    Severity,
)

Confidence = Literal["low", "medium", "high"]

__all__ = [
    "ConfidenceChange",
    "FindingFingerprint",
    "HypothesisFingerprint",
    "RegressionDiff",
    "SeverityChange",
    "compute_diff",
]


class FindingFingerprint(BaseModel):
    """Compact projection of a `Finding` for added/resolved listings.

    Carries the `id` (the diff match key), source inspector, severity, and
    message — enough for CLI rendering without shipping the full evidence
    payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    inspector_name: str | None
    severity: Severity
    message: str


class SeverityChange(BaseModel):
    """A finding whose `id` is present in both runs but whose severity
    changed (`from_severity` → `to_severity`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    from_severity: Severity
    to_severity: Severity
    message: str


class HypothesisFingerprint(BaseModel):
    """Compact projection of a `RootCauseHypothesis` for hypothesis-level
    added/resolved listings.

    The match key is `frozenset(supporting_findings)`; `supporting_findings`
    here is its sorted+deduped readable form (rendering/audit). `description`
    is display-only and does not participate in matching.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence: Confidence
    supporting_findings: list[str]
    description: str


class ConfidenceChange(BaseModel):
    """A hypothesis whose evidence-set key is present in both runs but whose
    `confidence` changed (`from_confidence` → `to_confidence`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    supporting_findings: list[str]
    from_confidence: Confidence
    to_confidence: Confidence
    description: str


class RegressionDiff(BaseModel):
    """Closed output shape of `compute_diff`.

    `baseline_meta` is non-None iff `baseline.meta is not None` (regardless
    of whether `current.meta` is None and regardless of any skip reason);
    it is None only when there is no baseline meta to project from.

    `diff_skipped_reason` is a three-value closed set; when set, the diff
    lists are empty (the comparison was skipped to avoid a polluted or
    unsound diff).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_meta: BaselineRef | None
    added: list[FindingFingerprint] = Field(default_factory=list)
    resolved: list[FindingFingerprint] = Field(default_factory=list)
    changed_severity: list[SeverityChange] = Field(default_factory=list)
    inspector_upgraded: list[str] = Field(default_factory=list)
    dst_boundary_crossed: bool = False
    diff_skipped_reason: (
        Literal["baseline_not_ok", "schema_changed", "missing_finding_ids"] | None
    ) = None
    hypothesis_added: list[HypothesisFingerprint] = Field(default_factory=list)
    hypothesis_resolved: list[HypothesisFingerprint] = Field(default_factory=list)
    hypothesis_confidence_changed: list[ConfidenceChange] = Field(default_factory=list)
    hypothesis_unanchored: int = Field(default=0, ge=0)
    hypothesis_ambiguous_keys: int = Field(default=0, ge=0)


def _project_baseline_meta(meta: ReportMeta) -> BaselineRef:
    """Project a baseline `Report.meta` into a `BaselineRef`.

    `inspector_versions` is taken from `meta.inspectors_used` (name→version)
    so diff version-alignment and `latest_ok_baseline` share one source.
    """
    return BaselineRef(
        run_id=meta.run_id,
        timestamp=meta.timestamp,
        status=meta.status,
        inspector_versions={run.name: run.version for run in meta.inspectors_used},
        report_schema_version=meta.report_schema_version,
    )


def _fingerprint(finding: Finding) -> FindingFingerprint:
    # `id` is guaranteed non-None here: callers only fingerprint findings
    # that passed the rule-2 identity-completeness gate.
    assert finding.id is not None
    return FindingFingerprint(
        id=finding.id,
        inspector_name=finding.inspector_name,
        severity=finding.severity,
        message=finding.message,
    )


def _upgraded_inspectors(baseline: Report, current: Report) -> set[str]:
    """Inspector names whose version differs between the two runs.

    Versions come from each side's `meta.inspectors_used` (the
    authoritative per-inspector run summary). An inspector present on only
    one side is not "upgraded" — version mismatch requires both sides.
    """
    assert baseline.meta is not None and current.meta is not None
    baseline_versions = {run.name: run.version for run in baseline.meta.inspectors_used}
    current_versions = {run.name: run.version for run in current.meta.inspectors_used}
    return {
        name
        for name in baseline_versions.keys() & current_versions.keys()
        if baseline_versions[name] != current_versions[name]
    }


def _hyp_key(hypothesis: RootCauseHypothesis) -> frozenset[str]:
    return frozenset(hypothesis.supporting_findings)


def _sorted_key(key: frozenset[str]) -> list[str]:
    return sorted(key)


def _hyp_fingerprint(hypothesis: RootCauseHypothesis) -> HypothesisFingerprint:
    return HypothesisFingerprint(
        confidence=hypothesis.confidence,
        supporting_findings=sorted(set(hypothesis.supporting_findings)),
        description=hypothesis.description,
    )


def _index_hypotheses(
    hypotheses: list[RootCauseHypothesis],
) -> tuple[dict[frozenset[str], list[RootCauseHypothesis]], int]:
    """Group anchored hypotheses by evidence-set key, dropping empty-support
    ones (counted into `unanchored`). Within each key the list is stably
    sorted by `(sorted(supporting_findings), confidence, description)` so the
    deterministic representative is the first element.
    """
    unanchored = 0
    by_key: dict[frozenset[str], list[RootCauseHypothesis]] = {}
    for h in hypotheses:
        if not h.supporting_findings:
            unanchored += 1
            continue
        by_key.setdefault(_hyp_key(h), []).append(h)
    for group in by_key.values():
        group.sort(key=lambda h: (sorted(h.supporting_findings), h.confidence, h.description))
    return by_key, unanchored


def _hypothesis_diff(
    baseline: Report, current: Report
) -> tuple[
    list[HypothesisFingerprint],
    list[HypothesisFingerprint],
    list[ConfidenceChange],
    int,
    int,
]:
    baseline_by_key, baseline_unanchored = _index_hypotheses(baseline.hypotheses)
    current_by_key, current_unanchored = _index_hypotheses(current.hypotheses)

    added = [
        _hyp_fingerprint(current_by_key[key][0])
        for key in sorted(current_by_key.keys() - baseline_by_key.keys(), key=_sorted_key)
    ]
    resolved = [
        _hyp_fingerprint(baseline_by_key[key][0])
        for key in sorted(baseline_by_key.keys() - current_by_key.keys(), key=_sorted_key)
    ]

    confidence_changed: list[ConfidenceChange] = []
    ambiguous_keys = 0
    for key in sorted(baseline_by_key.keys() & current_by_key.keys(), key=_sorted_key):
        baseline_group = baseline_by_key[key]
        current_group = current_by_key[key]
        if len(baseline_group) > 1 or len(current_group) > 1:
            ambiguous_keys += 1
            continue
        baseline_h = baseline_group[0]
        current_h = current_group[0]
        if baseline_h.confidence != current_h.confidence:
            confidence_changed.append(
                ConfidenceChange(
                    supporting_findings=_sorted_key(key),
                    from_confidence=baseline_h.confidence,
                    to_confidence=current_h.confidence,
                    description=current_h.description,
                )
            )

    return (
        added,
        resolved,
        confidence_changed,
        baseline_unanchored + current_unanchored,
        ambiguous_keys,
    )


def compute_diff(baseline: Report, current: Report, *, force: bool = False) -> RegressionDiff:
    """Compute a `RegressionDiff` between a baseline and current report.

    Rules are applied in strict order (see spec report-regression-diff):

    0. meta completeness — either side `meta is None` → `missing_finding_ids`
       (checked before any `.meta.` dereference; `baseline_meta` is still
       projected when `baseline.meta` is present).
    1. per-target isolation — differing `target_id` → `ValueError`.
    2. finding identity completeness — any `Finding.id is None` →
       `missing_finding_ids`.
    3. baseline status gate — `baseline.meta.status != "ok"` and not
       `force` → `baseline_not_ok`.
    4. schema alignment — differing `report_schema_version` →
       `schema_changed`.
    5. inspector version alignment — upgraded inspectors' findings are
       excluded; their names go to `inspector_upgraded`.
    6. fingerprint set difference — by `Finding.id`.
    """
    # Rule 0: meta completeness front-gate (before any `.meta.` deref).
    if baseline.meta is None or current.meta is None:
        baseline_meta = _project_baseline_meta(baseline.meta) if baseline.meta is not None else None
        return RegressionDiff(
            baseline_meta=baseline_meta, diff_skipped_reason="missing_finding_ids"
        )

    baseline_meta = _project_baseline_meta(baseline.meta)

    # Rule 1: per-target isolation.
    if baseline.meta.target_id != current.meta.target_id:
        raise ValueError(
            f"cannot diff across targets: baseline target_id="
            f"{baseline.meta.target_id!r} != current target_id="
            f"{current.meta.target_id!r}"
        )

    # Rule 2: finding identity completeness.
    if any(f.id is None for f in baseline.findings) or any(f.id is None for f in current.findings):
        return RegressionDiff(
            baseline_meta=baseline_meta, diff_skipped_reason="missing_finding_ids"
        )

    # Rule 3: baseline status gate.
    if baseline.meta.status != "ok" and not force:
        return RegressionDiff(baseline_meta=baseline_meta, diff_skipped_reason="baseline_not_ok")

    # Rule 4: schema alignment.
    if baseline.meta.report_schema_version != current.meta.report_schema_version:
        return RegressionDiff(baseline_meta=baseline_meta, diff_skipped_reason="schema_changed")

    # Rule 5: inspector version alignment — exclude upgraded inspectors.
    upgraded = _upgraded_inspectors(baseline, current)

    baseline_findings = [f for f in baseline.findings if f.inspector_name not in upgraded]
    current_findings = [f for f in current.findings if f.inspector_name not in upgraded]

    # Rule 6: fingerprint set difference by `Finding.id`.
    baseline_by_id = {f.id: f for f in baseline_findings}
    current_by_id = {f.id: f for f in current_findings}

    added = [_fingerprint(f) for fid, f in current_by_id.items() if fid not in baseline_by_id]
    resolved = [_fingerprint(f) for fid, f in baseline_by_id.items() if fid not in current_by_id]

    changed_severity: list[SeverityChange] = []
    for fid, current_finding in current_by_id.items():
        baseline_finding = baseline_by_id.get(fid)
        if baseline_finding is None:
            continue
        if baseline_finding.severity != current_finding.severity:
            assert fid is not None
            changed_severity.append(
                SeverityChange(
                    id=fid,
                    from_severity=baseline_finding.severity,
                    to_severity=current_finding.severity,
                    message=current_finding.message,
                )
            )

    (
        hypothesis_added,
        hypothesis_resolved,
        hypothesis_confidence_changed,
        hypothesis_unanchored,
        hypothesis_ambiguous_keys,
    ) = _hypothesis_diff(baseline, current)

    return RegressionDiff(
        baseline_meta=baseline_meta,
        added=added,
        resolved=resolved,
        changed_severity=changed_severity,
        inspector_upgraded=sorted(upgraded),
        dst_boundary_crossed=False,
        hypothesis_added=hypothesis_added,
        hypothesis_resolved=hypothesis_resolved,
        hypothesis_confidence_changed=hypothesis_confidence_changed,
        hypothesis_unanchored=hypothesis_unanchored,
        hypothesis_ambiguous_keys=hypothesis_ambiguous_keys,
    )
