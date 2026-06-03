"""Unit tests for the ``--intent`` Report assembly seam (group BC, tasks 2.1-2.4).

Spec: ``openspec/changes/add-intent-report-persistence/specs/agent-report-assembly/spec.md``.

These exercise the orchestration-layer helpers in ``hostlens.cli._intent`` that
turn the per-run ``InspectorResultCollector`` snapshot + a ``DiagnosticianResult``
into a faithful first-class ``Report``:

- ``_seed_findings_from_snapshot`` — id stamping from the Planner-phase snapshot
  (content-deterministic ``compute_finding_id``, no registry re-lookup).
- ``_sum_loop_usage`` — field-level token-usage summation across both loops.
- ``_assemble_report`` — Report assembly + hypotheses / narrative projection +
  status override + the id-consistency invariant.

They use directly-constructed ``InspectorResult`` / ``DiagnosticianResult``
objects (no backend / CLI) so the assembly contract is tested in isolation. The
full ``run_intent_diagnosis`` two-loop timing + no-result paths are covered at
the CLI level in ``test_inspect_intent_report.py``.

``asyncio_mode = "auto"`` (pyproject) — no marker needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hostlens.agent.diagnostician import DiagnosticianResult
from hostlens.agent.loop import LoopResult, LoopUsage
from hostlens.agent.planner import PlannerResult
from hostlens.cli._intent import (
    _DIAGNOSIS_NARRATIVE_KEY,
    _assemble_report,
    _seed_findings_from_snapshot,
    _sum_loop_usage,
)
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import (
    Finding,
    ReportStatus,
    RootCauseHypothesis,
    compute_finding_id,
)
from hostlens.tools.finding_store import FindingStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)


def _ir(
    *,
    name: str = "linux.cpu",
    version: str = "1.0.0",
    status: str = "ok",
    findings: list[Finding] | None = None,
    error: str | None = None,
    missing: list[str] | None = None,
) -> InspectorResult:
    return InspectorResult(
        name=name,
        version=version,
        status=status,  # type: ignore[arg-type]
        target_name="local-host",
        duration_seconds=0.5,
        findings=findings or [],
        error=error,
        missing=missing or [],
    )


def _empty_loop_result(usage: LoopUsage | None = None) -> LoopResult:
    return LoopResult(
        final_text="diag narrative",
        tool_invocations=[],
        turns=1,
        terminal_status="ok",
        usage_totals=usage or LoopUsage(),
        stop_reason="end_turn",
    )


def _planner_result(usage: LoopUsage | None = None) -> PlannerResult:
    return PlannerResult(
        narrative="planner narrative",
        findings=[],
        loop_result=_empty_loop_result(usage),
        intent="检查健康",
    )


def _diag_result(
    *,
    status: ReportStatus = ReportStatus.OK,
    hypotheses: list[RootCauseHypothesis] | None = None,
    narrative: str = "诊断完成",
    diagnostician_loop: LoopResult | None = None,
) -> DiagnosticianResult:
    return DiagnosticianResult(
        narrative=narrative,
        findings=[],
        hypotheses=hypotheses or [],
        status=status,
        planner_result=_planner_result(),
        diagnostician_loop=diagnostician_loop
        if diagnostician_loop is not None
        else _empty_loop_result(),
    )


# --------------------------------------------------------------------------- #
# 2.2 — id stamping (content-determinism, no registry re-lookup)
# --------------------------------------------------------------------------- #


def test_seed_findings_stamps_ids_from_snapshot_no_registry() -> None:
    """Each finding gets an id computed from its source InspectorResult's
    name/version + message (no registry passed in at all)."""

    snapshot = [
        _ir(findings=[Finding(severity="warning", message="cpu high")]),
        _ir(
            name="linux.mem",
            version="2.1.0",
            findings=[Finding(severity="info", message="mem ok")],
        ),
    ]
    store = FindingStore()

    seeded = _seed_findings_from_snapshot(snapshot, store)

    assert [s.label for s in seeded] == ["F1", "F2"]
    assert seeded[0].finding.id == compute_finding_id("linux.cpu", "1.0.0", "cpu high")
    assert seeded[1].finding.id == compute_finding_id("linux.mem", "2.1.0", "mem ok")
    # Findings are seeded into the store under those labels.
    assert store.resolve_label("F1") == seeded[0].finding.id
    assert store.resolve_label("F2") == seeded[1].finding.id


def test_seed_id_equals_report_id_same_source() -> None:
    """The id the seed helper stamps equals the id from_inspector_results produces
    for the same finding (same content-deterministic function + same version)."""

    snapshot = [_ir(findings=[Finding(severity="warning", message="cpu high")])]
    store = FindingStore()

    seeded = _seed_findings_from_snapshot(snapshot, store)
    report = _assemble_report(
        "local-host",
        "检查健康",
        snapshot,
        _diag_result(),
        _T0,
        _T1,
        token_usage=_sum_loop_usage(),
        target_type="local",
    )

    assert len(report.findings) == 1
    assert report.findings[0].id == seeded[0].finding.id


# --------------------------------------------------------------------------- #
# 2.2 — hypotheses / narrative projection + supporting_findings ⊆ Report.findings
# --------------------------------------------------------------------------- #


def test_assemble_projects_hypotheses_and_narrative() -> None:
    snapshot = [_ir(findings=[Finding(severity="warning", message="cpu high")])]
    real_id = compute_finding_id("linux.cpu", "1.0.0", "cpu high")
    diag = _diag_result(
        hypotheses=[
            RootCauseHypothesis(
                description="资源争用",
                confidence="medium",
                supporting_findings=[real_id],
                suggested_actions=["扩容"],
            )
        ],
        narrative="根因综述",
    )

    report = _assemble_report(
        "local-host",
        "检查健康",
        snapshot,
        diag,
        _T0,
        _T1,
        token_usage=_sum_loop_usage(),
        target_type="local",
    )

    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].supporting_findings == [real_id]
    assert report.metadata[_DIAGNOSIS_NARRATIVE_KEY] == "根因综述"
    # supporting_findings ⊆ Report.findings ids.
    finding_ids = {f.id for f in report.findings}
    for h in report.hypotheses:
        for ref in h.supporting_findings:
            assert ref in finding_ids


# --------------------------------------------------------------------------- #
# 2.3 — id-consistency invariant: dangling supporting_findings → fail-loud
# --------------------------------------------------------------------------- #


def test_assemble_dangling_supporting_finding_raises() -> None:
    snapshot = [_ir(findings=[Finding(severity="warning", message="cpu high")])]
    diag = _diag_result(
        hypotheses=[
            RootCauseHypothesis(
                description="悬空引用",
                confidence="low",
                supporting_findings=["deadbeefdeadbeef"],  # not in Report.findings
                suggested_actions=[],
            )
        ]
    )

    with pytest.raises(ValueError, match="id-consistency invariant"):
        _assemble_report(
            "local-host",
            "检查健康",
            snapshot,
            diag,
            _T0,
            _T1,
            token_usage=_sum_loop_usage(),
            target_type="local",
        )


# --------------------------------------------------------------------------- #
# 2.4 — status override / derivation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("diag_status", "snapshot_statuses", "expected"),
    [
        # reconcile degraded → override verbatim (covers; _derive can't make these).
        (ReportStatus.DEGRADED_TOKEN_BUDGET, ["ok"], ReportStatus.DEGRADED_TOKEN_BUDGET),
        (ReportStatus.EMPTY_RESPONSE, ["ok"], ReportStatus.EMPTY_RESPONSE),
        # reconcile ok → status=None → _derive_report_status by §9.
        (ReportStatus.OK, ["ok", "ok"], ReportStatus.OK),
        # non-ok only timeout with an ok → still ok (§9 single timeout no-degrade).
        (ReportStatus.OK, ["ok", "timeout"], ReportStatus.OK),
        # target_unreachable → partial (not blanket "any non-ok → partial").
        (ReportStatus.OK, ["ok", "target_unreachable"], ReportStatus.PARTIAL),
        # all non-ok (target_unreachable) → partial.
        (ReportStatus.OK, ["target_unreachable"], ReportStatus.PARTIAL),
    ],
)
def test_assemble_status_override_and_derivation(
    diag_status: ReportStatus,
    snapshot_statuses: list[str],
    expected: ReportStatus,
) -> None:
    """meta.status: degraded reconcile overrides; ok reconcile defers to §9 derive."""

    snapshot: list[InspectorResult] = []
    for i, st in enumerate(snapshot_statuses):
        if st == "ok":
            snapshot.append(_ir(name=f"insp{i}", findings=[]))
        elif st == "target_unreachable":
            snapshot.append(_ir(name=f"insp{i}", status="target_unreachable", error="unreachable"))
        elif st == "timeout":
            snapshot.append(_ir(name=f"insp{i}", status="timeout", error="timed out"))
        else:  # pragma: no cover - defensive
            raise AssertionError(st)

    diag = _diag_result(status=diag_status)
    report = _assemble_report(
        "local-host",
        "检查健康",
        snapshot,
        diag,
        _T0,
        _T1,
        token_usage=_sum_loop_usage(),
        target_type="local",
    )

    assert report.meta is not None
    assert report.meta.status == expected


# --------------------------------------------------------------------------- #
# 2.1 — token usage is the field-level sum of both loops
# --------------------------------------------------------------------------- #


def test_sum_loop_usage_field_level() -> None:
    planner = LoopUsage(
        input_tokens=10,
        output_tokens=2,
        cache_creation_input_tokens=5,
        cache_read_input_tokens=3,
    )
    diag = LoopUsage(
        input_tokens=7,
        output_tokens=4,
        cache_creation_input_tokens=1,
        cache_read_input_tokens=9,
    )

    total = _sum_loop_usage(planner, diag)

    assert total.input_tokens == 17
    assert total.output_tokens == 6
    assert total.cache_creation_input_tokens == 6
    assert total.cache_read_input_tokens == 12


def test_assemble_token_usage_reaches_meta() -> None:
    snapshot = [_ir(findings=[])]
    usage = _sum_loop_usage(
        LoopUsage(input_tokens=10, output_tokens=2),
        LoopUsage(input_tokens=7, output_tokens=4),
    )

    report = _assemble_report(
        "local-host",
        "检查健康",
        snapshot,
        _diag_result(),
        _T0,
        _T1,
        token_usage=usage,
        target_type="local",
    )

    assert report.meta is not None
    assert report.meta.token_usage.input_tokens == 17
    assert report.meta.token_usage.output_tokens == 6
    # started/finished come from the orchestration-layer clock, not the loop.
    assert report.started_at == _T0
    assert report.finished_at == _T1
    assert report.meta.duration_seconds == 5.0


# --------------------------------------------------------------------------- #
# target_type threading: the resolved ExecutionTarget.type reaches meta
# (not the factory default "local") — parity with the --inspector path.
# --------------------------------------------------------------------------- #


def test_assemble_target_type_reaches_meta() -> None:
    snapshot = [_ir(findings=[])]

    report = _assemble_report(
        "ssh-host",
        "检查健康",
        snapshot,
        _diag_result(),
        _T0,
        _T1,
        token_usage=_sum_loop_usage(),
        target_type="ssh",
    )

    assert report.meta is not None
    assert report.meta.target_type == "ssh"
