"""Tests for the regression diff engine (`hostlens.reporting.diff`).

Covers spec report-regression-diff §需求 逐条对照:

- `RegressionDiff` extra=forbid + `diff_skipped_reason` 闭集
- `compute_diff` 规则 0-7:
  - 同报告 diff 自身无回归
  - current 新增 finding 进 added / baseline 独有进 resolved
  - severity 变化进 changed_severity 而非 added+resolved
  - 含 None id 的 finding 跳过 (missing_finding_ids)
  - meta=None 的 legacy 报告跳过且不 None-deref
  - current.meta 缺失但 baseline.meta 在仍投影 baseline_meta
  - 基线非 ok 跳过 / force 覆盖
  - inspector 版本升级时其 finding 排除
  - 跨 target raise ValueError
  - schema 版本不一致跳过 (schema_changed)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import pytest
from pydantic import ValidationError

import hostlens.inspectors.result  # noqa: F401  # triggers Report.model_rebuild
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.diff import (
    ConfidenceChange,
    FindingFingerprint,
    HypothesisFingerprint,
    RegressionDiff,
    SeverityChange,
    compute_diff,
)
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    RootCauseHypothesis,
    Severity,
)

Confidence = Literal["low", "medium", "high"]


def _t0() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _t1() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 2)


def _ir(
    name: str,
    *,
    version: str = "1.0",
    status: str = "ok",
    findings: list[Finding] | None = None,
    duration_seconds: float = 0.1,
) -> InspectorResult:
    kwargs: dict[str, object] = {
        "name": name,
        "version": version,
        "status": status,
        "target_name": "t",
        "duration_seconds": duration_seconds,
        "findings": findings if findings is not None else [],
    }
    if status in ("timeout", "target_unreachable", "exception"):
        kwargs["error"] = f"{status} happened"
    if status == "requires_unmet":
        kwargs["missing"] = ["needs_root"]
    return InspectorResult(**kwargs)  # type: ignore[arg-type]


def _f(message: str, severity: Severity = "info") -> Finding:
    return Finding(severity=severity, message=message)


def _report(
    inspector_results: list[InspectorResult],
    *,
    target_id: str | None = None,
    status: ReportStatus | None = None,
    hypotheses: list[RootCauseHypothesis] | None = None,
) -> Report:
    report = Report.from_inspector_results(
        "t",
        inspector_results,
        started_at=_t0(),
        finished_at=_t1(),
        target_id=target_id,
        status=status,
    )
    if hypotheses is not None:
        report = report.model_copy(update={"hypotheses": hypotheses})
    return report


def _h(
    *supporting_findings: str,
    confidence: Confidence = "medium",
    description: str = "root cause",
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        description=description,
        confidence=confidence,
        supporting_findings=list(supporting_findings),
    )


def _ok_report(*, hypotheses: list[RootCauseHypothesis] | None = None) -> Report:
    return _report([_ir("insp.a", findings=[_f("A")])], hypotheses=hypotheses)


# --------------------------------------------------------------------------- #
# RegressionDiff model
# --------------------------------------------------------------------------- #


def test_regression_diff_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, not_a_field="x")  # type: ignore[call-arg]


def test_diff_skipped_reason_is_closed_set() -> None:
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, diff_skipped_reason="whatever")  # type: ignore[arg-type]


def test_diff_skipped_reason_accepts_three_legal_values_and_none() -> None:
    for v in (None, "baseline_not_ok", "schema_changed", "missing_finding_ids"):
        d = RegressionDiff(baseline_meta=None, diff_skipped_reason=v)  # type: ignore[arg-type]
        assert d.diff_skipped_reason == v


def test_regression_diff_defaults() -> None:
    d = RegressionDiff(baseline_meta=None)
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.inspector_upgraded == []
    assert d.dst_boundary_crossed is False
    assert d.diff_skipped_reason is None


def test_finding_fingerprint_field_set() -> None:
    fp = FindingFingerprint(id="abc", inspector_name="insp.a", severity="warning", message="m")
    assert fp.id == "abc"
    assert fp.inspector_name == "insp.a"
    assert fp.severity == "warning"


def test_severity_change_field_set() -> None:
    sc = SeverityChange(id="abc", from_severity="warning", to_severity="critical", message="m")
    assert sc.from_severity == "warning"
    assert sc.to_severity == "critical"


# --------------------------------------------------------------------------- #
# compute_diff — no-regression / added / resolved
# --------------------------------------------------------------------------- #


def test_same_report_diffs_to_no_regression() -> None:
    r = _report([_ir("insp.a", findings=[_f("m1"), _f("m2")])])
    d = compute_diff(r, r)
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.diff_skipped_reason is None
    assert d.baseline_meta is not None


def test_current_new_finding_goes_to_added() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current)
    assert [fp.message for fp in d.added] == ["B"]
    assert d.resolved == []
    assert d.changed_severity == []


def test_baseline_only_finding_goes_to_resolved() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    current = _report([_ir("insp.a", findings=[_f("A")])])
    d = compute_diff(baseline, current)
    assert [fp.message for fp in d.resolved] == ["B"]
    assert d.added == []
    assert d.changed_severity == []


# --------------------------------------------------------------------------- #
# compute_diff — changed_severity
# --------------------------------------------------------------------------- #


def test_severity_change_goes_to_changed_severity_not_added_resolved() -> None:
    # Same (inspector, version, message) → same id; only severity differs.
    baseline = _report([_ir("insp.a", findings=[_f("disk 95%", severity="warning")])])
    current = _report([_ir("insp.a", findings=[_f("disk 95%", severity="critical")])])
    d = compute_diff(baseline, current)
    assert len(d.changed_severity) == 1
    sc = d.changed_severity[0]
    assert sc.from_severity == "warning"
    assert sc.to_severity == "critical"
    assert sc.message == "disk 95%"
    assert d.added == []
    assert d.resolved == []
    # the changed-severity finding's id must not surface in added/resolved
    changed_id = sc.id
    assert all(fp.id != changed_id for fp in d.added)
    assert all(fp.id != changed_id for fp in d.resolved)


# --------------------------------------------------------------------------- #
# compute_diff — None id / meta=None gates
# --------------------------------------------------------------------------- #


def test_none_id_finding_skips_diff() -> None:
    # A directly-constructed Report with a finding lacking id (not via factory).
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    # current carries a finding with id=None (legacy/direct construction).
    bare_finding = Finding(severity="info", message="A")  # id is None
    current = baseline.model_copy(update={"findings": [bare_finding]})
    d = compute_diff(baseline, current)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    # baseline.meta is present → baseline_meta projected even on skip
    assert d.baseline_meta is not None


def test_meta_none_on_baseline_skips_without_none_deref() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], hypotheses=[_h("f1")])
    current = _report([_ir("insp.a", findings=[_f("A")])], hypotheses=[_h("f1")])
    baseline_no_meta = baseline.model_copy(update={"meta": None})
    # Must not raise AttributeError on `.meta.target_id`.
    d = compute_diff(baseline_no_meta, current)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    # baseline.meta is None → baseline_meta is None
    assert d.baseline_meta is None
    assert (
        d.hypothesis_added == []
        and d.hypothesis_resolved == []
        and d.hypothesis_confidence_changed == []
        and d.hypothesis_unanchored == 0
        and d.hypothesis_ambiguous_keys == 0
    )


def test_meta_none_on_current_skips_but_projects_baseline_meta() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A")])])
    current_no_meta = current.model_copy(update={"meta": None})
    d = compute_diff(baseline, current_no_meta)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    # current.meta is None but baseline.meta is present → still projected
    assert d.baseline_meta is not None
    assert baseline.meta is not None
    assert d.baseline_meta.run_id == baseline.meta.run_id


# --------------------------------------------------------------------------- #
# compute_diff — baseline status gate + force
# --------------------------------------------------------------------------- #


def test_baseline_not_ok_skips_diff() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], status=ReportStatus.PARTIAL)
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current, force=False)
    assert d.diff_skipped_reason == "baseline_not_ok"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.baseline_meta is not None


def test_force_overrides_non_ok_baseline() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], status=ReportStatus.PARTIAL)
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current, force=True)
    assert d.diff_skipped_reason is None
    assert [fp.message for fp in d.added] == ["B"]


# --------------------------------------------------------------------------- #
# compute_diff — inspector version alignment
# --------------------------------------------------------------------------- #


def test_inspector_upgrade_excludes_its_findings() -> None:
    # baseline version 1.0 has finding "old"; current version 1.1 has "new".
    baseline = _report([_ir("linux.disk.usage", version="1.0", findings=[_f("old")])])
    current = _report([_ir("linux.disk.usage", version="1.1", findings=[_f("new")])])
    d = compute_diff(baseline, current)
    assert d.inspector_upgraded == ["linux.disk.usage"]
    # version-bumped inspector's findings must not surface as added/resolved
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []


def test_inspector_upgrade_isolated_other_inspector_still_diffs() -> None:
    # insp.a upgraded (excluded); insp.b stable (its added finding still shows).
    baseline = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("a-old")]),
            _ir("insp.b", version="2.0", findings=[_f("b1")]),
        ]
    )
    current = _report(
        [
            _ir("insp.a", version="1.1", findings=[_f("a-new")]),
            _ir("insp.b", version="2.0", findings=[_f("b1"), _f("b2")]),
        ]
    )
    d = compute_diff(baseline, current)
    assert d.inspector_upgraded == ["insp.a"]
    assert [fp.message for fp in d.added] == ["b2"]
    assert d.resolved == []


# --------------------------------------------------------------------------- #
# compute_diff — per-target isolation + schema alignment
# --------------------------------------------------------------------------- #


def test_cross_target_diff_raises() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], target_id="host-a")
    current = _report([_ir("insp.a", findings=[_f("A")])], target_id="host-b")
    with pytest.raises(ValueError, match="across targets"):
        compute_diff(baseline, current)


def test_schema_mismatch_skips_diff() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    assert baseline.meta is not None
    # Force a report_schema_version mismatch on the baseline meta.
    bumped_meta = baseline.meta.model_copy(update={"report_schema_version": "9.9"})
    baseline_bumped = baseline.model_copy(update={"meta": bumped_meta})
    d = compute_diff(baseline_bumped, current)
    assert d.diff_skipped_reason == "schema_changed"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.baseline_meta is not None


# --------------------------------------------------------------------------- #
# compute_diff — baseline_meta projection
# --------------------------------------------------------------------------- #


def test_baseline_meta_projects_inspector_versions() -> None:
    baseline = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("A")]),
            _ir("insp.b", version="2.5", findings=[_f("B")]),
        ]
    )
    current = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("A")]),
            _ir("insp.b", version="2.5", findings=[_f("B")]),
        ]
    )
    d = compute_diff(baseline, current)
    assert d.baseline_meta is not None
    assert d.baseline_meta.inspector_versions == {"insp.a": "1.0", "insp.b": "2.5"}
    assert baseline.meta is not None
    assert d.baseline_meta.run_id == baseline.meta.run_id


def test_dst_boundary_crossed_always_false() -> None:
    r = _report([_ir("insp.a", findings=[_f("A")])])
    d = compute_diff(r, r)
    assert d.dst_boundary_crossed is False


# --------------------------------------------------------------------------- #
# RegressionDiff model — hypothesis fields
# --------------------------------------------------------------------------- #


def test_regression_diff_hypothesis_defaults() -> None:
    d = RegressionDiff(baseline_meta=None)
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


def test_regression_diff_rejects_negative_hypothesis_counts() -> None:
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, hypothesis_unanchored=-1)
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, hypothesis_ambiguous_keys=-1)
    d = RegressionDiff(baseline_meta=None, hypothesis_unanchored=0, hypothesis_ambiguous_keys=0)
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


def test_hypothesis_fingerprint_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        HypothesisFingerprint(  # type: ignore[call-arg]
            confidence="low", supporting_findings=["a"], description="d", extra="x"
        )


def test_confidence_change_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ConfidenceChange(  # type: ignore[call-arg]
            supporting_findings=["a"],
            from_confidence="low",
            to_confidence="high",
            description="d",
            extra="x",
        )


# --------------------------------------------------------------------------- #
# compute_diff — hypothesis added / resolved / confidence_changed
# --------------------------------------------------------------------------- #


def test_hypothesis_new_evidence_set_goes_to_added() -> None:
    baseline = _ok_report(hypotheses=[_h("f1")])
    current = _ok_report(hypotheses=[_h("f1"), _h("f2", description="new cause")])
    d = compute_diff(baseline, current)
    assert [hf.description for hf in d.hypothesis_added] == ["new cause"]
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []


def test_hypothesis_baseline_only_goes_to_resolved() -> None:
    baseline = _ok_report(hypotheses=[_h("f1"), _h("f2", description="gone")])
    current = _ok_report(hypotheses=[_h("f1")])
    d = compute_diff(baseline, current)
    assert [hf.description for hf in d.hypothesis_resolved] == ["gone"]
    assert d.hypothesis_added == []
    assert d.hypothesis_confidence_changed == []


def test_hypothesis_same_key_confidence_change() -> None:
    baseline = _ok_report(hypotheses=[_h("f1", confidence="low")])
    current = _ok_report(hypotheses=[_h("f1", confidence="high")])
    d = compute_diff(baseline, current)
    assert len(d.hypothesis_confidence_changed) == 1
    cc = d.hypothesis_confidence_changed[0]
    assert cc.from_confidence == "low"
    assert cc.to_confidence == "high"
    assert cc.supporting_findings == ["f1"]
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []


def test_hypothesis_matches_on_evidence_set_not_description() -> None:
    # Same evidence key, same confidence, different description → no change.
    baseline = _ok_report(hypotheses=[_h("f1", description="text A")])
    current = _ok_report(hypotheses=[_h("f1", description="text B (rephrased)")])
    d = compute_diff(baseline, current)
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []


def test_hypothesis_key_is_order_independent_and_deduped() -> None:
    # ["A","A"] and ["A"] → same key (frozenset dedupes) → matched.
    baseline = _ok_report(hypotheses=[_h("A", "A")])
    current = _ok_report(hypotheses=[_h("A")])
    d = compute_diff(baseline, current)
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    # And the projected fingerprint dedupes/sorts.
    baseline2 = _ok_report(hypotheses=[_h("b", "a", "a")])
    current2 = _ok_report()
    d2 = compute_diff(baseline2, current2)
    assert d2.hypothesis_resolved[0].supporting_findings == ["a", "b"]


def test_hypothesis_empty_report_no_hypothesis_diff() -> None:
    baseline = _ok_report()
    current = _ok_report()
    d = compute_diff(baseline, current)
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


# --------------------------------------------------------------------------- #
# compute_diff — collision: added key emits exactly one representative
# --------------------------------------------------------------------------- #


def test_collision_added_key_emits_single_representative() -> None:
    # current has 2 hypotheses sharing key {f1} (same confidence, diff
    # description), key absent from baseline → exactly 1 representative.
    current = _ok_report(
        hypotheses=[
            _h("f1", confidence="medium", description="zeta"),
            _h("f1", confidence="medium", description="alpha"),
        ]
    )
    baseline = _ok_report()
    d = compute_diff(baseline, current)
    assert len(d.hypothesis_added) == 1
    # per-report sort by (sorted(support), confidence, description) → "alpha" first
    assert d.hypothesis_added[0].description == "alpha"
    assert d.hypothesis_ambiguous_keys == 0


def test_collision_ambiguous_key_confidence_skipped_and_counted() -> None:
    # baseline: key {f1} exactly 1 (low). current: key {f1} has 2 (one high).
    baseline = _ok_report(hypotheses=[_h("f1", confidence="low")])
    current = _ok_report(
        hypotheses=[
            _h("f1", confidence="high", description="a"),
            _h("f1", confidence="medium", description="b"),
        ]
    )
    d = compute_diff(baseline, current)
    # key present both sides → not in added/resolved
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    # ambiguous → confidence comparison skipped, key counted
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_ambiguous_keys == 1


def test_mixed_clean_and_ambiguous_keys_do_not_cross_contaminate() -> None:
    # Clean key {f2,f1}: exactly 1 each side, confidence low → high → counted as
    # a confidence change. Ambiguous key {g1}: current side has 2 collisions →
    # skipped + counted, must not suppress the clean key's confidence change.
    baseline = _ok_report(
        hypotheses=[
            _h("f2", "f1", confidence="low", description="clean"),
            _h("g1", confidence="medium", description="amb-base"),
        ]
    )
    current = _ok_report(
        hypotheses=[
            _h("f2", "f1", confidence="high", description="clean"),
            _h("g1", confidence="high", description="amb-a"),
            _h("g1", confidence="medium", description="amb-b"),
        ]
    )
    d = compute_diff(baseline, current)
    assert len(d.hypothesis_confidence_changed) == 1
    cc = d.hypothesis_confidence_changed[0]
    assert cc.supporting_findings == ["f1", "f2"]
    assert cc.from_confidence == "low"
    assert cc.to_confidence == "high"
    assert d.hypothesis_ambiguous_keys == 1
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []

    # Reversed key ordering: ambiguous key {a1} sorts BEFORE clean key {z2,z1}.
    # Pins that the per-key skip is isolated regardless of iteration order — a
    # contamination bug that suppressed later keys would surface only here.
    baseline_rev = _ok_report(
        hypotheses=[
            _h("a1", confidence="medium", description="amb-base"),
            _h("z2", "z1", confidence="low", description="clean"),
        ]
    )
    current_rev = _ok_report(
        hypotheses=[
            _h("a1", confidence="high", description="amb-a"),
            _h("a1", confidence="medium", description="amb-b"),
            _h("z2", "z1", confidence="high", description="clean"),
        ]
    )
    d_rev = compute_diff(baseline_rev, current_rev)
    assert len(d_rev.hypothesis_confidence_changed) == 1
    cc_rev = d_rev.hypothesis_confidence_changed[0]
    assert cc_rev.supporting_findings == ["z1", "z2"]
    assert cc_rev.from_confidence == "low"
    assert cc_rev.to_confidence == "high"
    assert d_rev.hypothesis_ambiguous_keys == 1
    assert d_rev.hypothesis_added == []
    assert d_rev.hypothesis_resolved == []


# --------------------------------------------------------------------------- #
# compute_diff — empty supporting_findings → unanchored
# --------------------------------------------------------------------------- #


def test_empty_support_counts_unanchored_and_absent_from_lists() -> None:
    baseline = _ok_report(hypotheses=[_h(description="no evidence")])
    current = _ok_report(hypotheses=[_h(description="no evidence")])
    d = compute_diff(baseline, current)
    # two-run total: baseline 1 + current 1 = 2
    assert d.hypothesis_unanchored == 2
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []


# --------------------------------------------------------------------------- #
# compute_diff — gate inheritance (hypothesis fields zeroed)
# --------------------------------------------------------------------------- #


def test_gate_baseline_not_ok_zeroes_hypothesis_fields() -> None:
    baseline = _report(
        [_ir("insp.a", findings=[_f("A")])],
        status=ReportStatus.PARTIAL,
        hypotheses=[_h("f1")],
    )
    current = _ok_report(hypotheses=[_h("f2")])
    d = compute_diff(baseline, current, force=False)
    assert d.diff_skipped_reason == "baseline_not_ok"
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


def test_gate_schema_mismatch_zeroes_hypothesis_fields() -> None:
    baseline = _ok_report(hypotheses=[_h("f1")])
    current = _ok_report(hypotheses=[_h("f2")])
    assert baseline.meta is not None
    bumped_meta = baseline.meta.model_copy(update={"report_schema_version": "9.9"})
    baseline_bumped = baseline.model_copy(update={"meta": bumped_meta})
    d = compute_diff(baseline_bumped, current)
    assert d.diff_skipped_reason == "schema_changed"
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


def test_gate_none_finding_id_zeroes_hypothesis_fields() -> None:
    baseline = _ok_report(hypotheses=[_h("f1")])
    bare_finding = Finding(severity="info", message="A")  # id is None
    current = baseline.model_copy(update={"findings": [bare_finding], "hypotheses": [_h("f2")]})
    d = compute_diff(baseline, current)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.hypothesis_added == []
    assert d.hypothesis_resolved == []
    assert d.hypothesis_confidence_changed == []
    assert d.hypothesis_unanchored == 0
    assert d.hypothesis_ambiguous_keys == 0


def test_gate_cross_target_raises_before_hypothesis_diff() -> None:
    baseline = _report(
        [_ir("insp.a", findings=[_f("A")])], target_id="host-a", hypotheses=[_h("f1")]
    )
    current = _report(
        [_ir("insp.a", findings=[_f("A")])], target_id="host-b", hypotheses=[_h("f2")]
    )
    with pytest.raises(ValueError, match="across targets"):
        compute_diff(baseline, current)


# --------------------------------------------------------------------------- #
# compute_diff — inspector skew does not rewrite hypothesis keys
# --------------------------------------------------------------------------- #


def test_inspector_skew_does_not_rewrite_hypothesis_key() -> None:
    # insp upgraded 1.0 → 1.1; finding message differs → finding ids differ.
    baseline_report = _report([_ir("linux.disk.usage", version="1.0", findings=[_f("old")])])
    current_report = _report([_ir("linux.disk.usage", version="1.1", findings=[_f("new")])])
    # Anchor hypotheses to the (distinct) finding ids declared as-is.
    assert baseline_report.findings[0].id is not None
    assert current_report.findings[0].id is not None
    baseline_fid = baseline_report.findings[0].id
    current_fid = current_report.findings[0].id
    baseline = baseline_report.model_copy(
        update={"hypotheses": [_h(baseline_fid, description="baseline cause")]}
    )
    current = current_report.model_copy(
        update={"hypotheses": [_h(current_fid, description="current cause")]}
    )
    d = compute_diff(baseline, current)
    # finding segment shows no change (rule 5 excludes upgraded inspector)
    assert d.inspector_upgraded == ["linux.disk.usage"]
    assert d.added == []
    assert d.resolved == []
    # but hypothesis keys are NOT rewritten → key drift produces added+resolved pair
    assert [hf.supporting_findings for hf in d.hypothesis_resolved] == [[baseline_fid]]
    assert [hf.supporting_findings for hf in d.hypothesis_added] == [[current_fid]]


# --------------------------------------------------------------------------- #
# compute_diff — determinism of representative selection + output ordering
# --------------------------------------------------------------------------- #


def test_hypothesis_lists_are_deterministic() -> None:
    baseline = _ok_report(
        hypotheses=[
            _h("f3", description="d3"),
            _h("f1", description="d1"),
        ]
    )
    current = _ok_report(
        hypotheses=[
            _h("f4", description="d4"),
            _h("f2", description="d2"),
        ]
    )
    d1 = compute_diff(baseline, current)
    d2 = compute_diff(baseline, current)
    assert d1.hypothesis_added == d2.hypothesis_added
    assert d1.hypothesis_resolved == d2.hypothesis_resolved
    # output sorted by sorted(supporting_findings)
    assert [hf.supporting_findings for hf in d1.hypothesis_added] == [["f2"], ["f4"]]
    assert [hf.supporting_findings for hf in d1.hypothesis_resolved] == [["f1"], ["f3"]]
