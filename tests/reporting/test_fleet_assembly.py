"""Tests for `Report.from_fleet_results` — the deterministic multi-target
(fleet) Report assembly path.

Covers report-data-model spec §需求:多 target Report 必须由确定性 fleet 组装
路径产出 and the deterministic-inspection-mode status truth table:

- one Report across multiple targets, findings flattened cross-target
- each flattened finding stamped with its source `target_name`
- identity fields (`id` / `inspector_name` / `inspector_version`) filled,
  never None (proposal C dedup prerequisite)
- `Report.target_name` is a deterministic fleet label (sorted, order-
  independent, `min_length=1`)
- `meta.target_id` is a deterministic fleet id (sorted target ids +
  schedule_name, `fleet:` prefixed) — distinct fleets differ, a single-
  member fleet does not collide with that host's per-target target_id
- `meta.inspectors_used` keeps one run per (target, inspector), status
  preserved verbatim (incl. `requires_unmet`)
- status derivation treats `requires_unmet` as ok (no degrade), real
  failures still degrade
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report, ReportStatus


def _t0() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _t1() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 2)


def _ir(
    name: str,
    target_name: str,
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
        "target_name": target_name,
        "duration_seconds": duration_seconds,
        "findings": findings if findings is not None else [],
    }
    if status in ("timeout", "target_unreachable", "exception"):
        kwargs["error"] = f"{status} happened"
    if status == "requires_unmet":
        kwargs["missing"] = ["needs_root"]
    return InspectorResult(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# one Report across multiple targets
# --------------------------------------------------------------------------- #


def test_fleet_produces_one_report_across_targets() -> None:
    # spec §场景:多 target 组装产出一份 Report
    fa = Finding(severity="warning", message="cpu a")
    fb = Finding(severity="info", message="cpu b")
    ir_a = _ir("linux.cpu", "a", findings=[fa])
    ir_b = _ir("linux.cpu", "b", findings=[fb])
    r = Report.from_fleet_results(
        [ir_a, ir_b],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert isinstance(r, Report)
    assert len(r.inspector_results) == 2
    assert [f.message for f in r.findings] == ["cpu a", "cpu b"]


def test_fleet_empty_results_raises() -> None:
    with pytest.raises(ValueError, match="from_fleet_results requires at least one"):
        Report.from_fleet_results([], schedule_name="daily", started_at=_t0(), finished_at=_t1())


# --------------------------------------------------------------------------- #
# findings carry source target_name + identity fields
# --------------------------------------------------------------------------- #


def test_fleet_findings_carry_source_target_name() -> None:
    # spec §场景:fleet Report 的 findings 带来源 target_name
    fa = Finding(severity="warning", message="cpu a")
    fb = Finding(severity="info", message="cpu b")
    ir_a = _ir("linux.cpu", "a", findings=[fa])
    ir_b = _ir("linux.cpu", "b", findings=[fb])
    r = Report.from_fleet_results(
        [ir_a, ir_b], schedule_name="daily", started_at=_t0(), finished_at=_t1()
    )
    by_message = {f.message: f for f in r.findings}
    assert by_message["cpu a"].target_name == "a"
    assert by_message["cpu b"].target_name == "b"


def test_fleet_findings_identity_fields_non_none() -> None:
    # spec §场景:fleet Report 的 findings 身份字段非 None(C 去重前提)
    f = Finding(severity="warning", message="cpu high")
    ir = _ir("linux.cpu.top_processes", "a", version="2.0", findings=[f])
    r = Report.from_fleet_results([ir], schedule_name="daily", started_at=_t0(), finished_at=_t1())
    out = r.findings[0]
    assert out.inspector_name == "linux.cpu.top_processes"
    assert out.inspector_version == "2.0"
    assert out.id is not None
    assert out.target_name == "a"


def test_fleet_finding_id_matches_single_target_factory() -> None:
    # The fleet path fills identity exactly like from_inspector_results, so
    # the same (name, version, message) yields the same id regardless of
    # which target produced it (target_name not in the fingerprint).
    f = Finding(severity="warning", message="disk 95%")
    ir_a = _ir("linux.disk", "a", findings=[f])
    ir_b = _ir("linux.disk", "b", findings=[f])
    r = Report.from_fleet_results(
        [ir_a, ir_b], schedule_name="daily", started_at=_t0(), finished_at=_t1()
    )
    ids = {fnd.id for fnd in r.findings}
    assert len(ids) == 1  # same id across targets


# --------------------------------------------------------------------------- #
# deterministic fleet label / id
# --------------------------------------------------------------------------- #


def test_fleet_target_name_label_deterministic_and_order_independent() -> None:
    # spec §场景:fleet target_name 标签确定性 + order-independence.
    ir_ab = [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")]
    ir_ba = [_ir("linux.cpu", "b"), _ir("linux.cpu", "a")]
    r_ab = Report.from_fleet_results(
        ir_ab, schedule_name="daily", started_at=_t0(), finished_at=_t1()
    )
    r_ba = Report.from_fleet_results(
        ir_ba, schedule_name="daily", started_at=_t0(), finished_at=_t1()
    )
    assert r_ab.target_name == r_ba.target_name
    assert len(r_ab.target_name) >= 1


def test_fleet_target_id_deterministic_same_inputs() -> None:
    # spec §场景:fleet target_id 由有序 target 集合与 schedule 确定性派生
    ir1 = [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")]
    ir2 = [_ir("linux.cpu", "b"), _ir("linux.cpu", "a")]
    r1 = Report.from_fleet_results(ir1, schedule_name="daily", started_at=_t0(), finished_at=_t1())
    r2 = Report.from_fleet_results(ir2, schedule_name="daily", started_at=_t0(), finished_at=_t1())
    assert r1.meta is not None and r2.meta is not None
    assert r1.meta.target_id == r2.meta.target_id


def test_fleet_target_id_differs_for_different_target_set() -> None:
    r_ab = Report.from_fleet_results(
        [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    r_ac = Report.from_fleet_results(
        [_ir("linux.cpu", "a"), _ir("linux.cpu", "c")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r_ab.meta is not None and r_ac.meta is not None
    assert r_ab.meta.target_id != r_ac.meta.target_id


def test_fleet_target_id_differs_for_different_schedule() -> None:
    irs = [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")]
    r_daily = Report.from_fleet_results(
        irs, schedule_name="daily", started_at=_t0(), finished_at=_t1()
    )
    r_hourly = Report.from_fleet_results(
        irs, schedule_name="hourly", started_at=_t0(), finished_at=_t1()
    )
    assert r_daily.meta is not None and r_hourly.meta is not None
    assert r_daily.meta.target_id != r_hourly.meta.target_id


def test_fleet_target_id_unambiguous_when_name_or_schedule_contains_comma() -> None:
    # Regression: a bare comma-join aliases (targets=[a,b], schedule="c,d") with
    # (targets=[a,b,c], schedule="d") to the same "fleet:a,b,c,d" store key,
    # silently overwriting one fleet's history. The hashed id keeps them distinct.
    r1 = Report.from_fleet_results(
        [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")],
        schedule_name="c,d",
        started_at=_t0(),
        finished_at=_t1(),
    )
    r2 = Report.from_fleet_results(
        [_ir("linux.cpu", "a"), _ir("linux.cpu", "b"), _ir("linux.cpu", "c")],
        schedule_name="d",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r1.meta is not None and r2.meta is not None
    assert r1.meta.target_id != r2.meta.target_id


def test_single_member_fleet_target_id_does_not_collide_with_per_target() -> None:
    # spec §场景:单成员 fleet 的 target_id 不撞该成员的 per-target target_id.
    r_fleet = Report.from_fleet_results(
        [_ir("linux.cpu", "x")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    r_per_target = Report.from_inspector_results(
        "x",
        [_ir("linux.cpu", "x")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r_fleet.meta is not None and r_per_target.meta is not None
    assert r_per_target.meta.target_id == "x"
    assert r_fleet.meta.target_id != "x"
    assert r_fleet.meta.target_id.startswith("fleet:")


# --------------------------------------------------------------------------- #
# inspectors_used per-(target, inspector), status verbatim
# --------------------------------------------------------------------------- #


def test_fleet_inspectors_used_one_per_target_inspector() -> None:
    irs = [
        _ir("linux.cpu", "a"),
        _ir("linux.disk", "a"),
        _ir("linux.cpu", "b"),
        _ir("linux.disk", "b"),
    ]
    r = Report.from_fleet_results(irs, schedule_name="daily", started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert len(r.meta.inspectors_used) == 4


def test_fleet_inspectors_used_preserves_requires_unmet() -> None:
    # spec §场景:fleet 的 inspectors_used 逐项保真 requires_unmet(C 覆盖行前提)
    irs = [
        _ir("linux.cpu", "a", status="ok"),
        _ir("mysql.replication", "a", status="requires_unmet"),
    ]
    r = Report.from_fleet_results(irs, schedule_name="daily", started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    statuses = {run.name: run.status for run in r.meta.inspectors_used}
    assert statuses["mysql.replication"] == "requires_unmet"
    # requires_unmet must not degrade the report.
    assert r.meta.status == ReportStatus.OK


# --------------------------------------------------------------------------- #
# status truth table (deterministic semantics)
# --------------------------------------------------------------------------- #


def test_fleet_status_all_ok() -> None:
    r = Report.from_fleet_results(
        [_ir("linux.cpu", "a"), _ir("linux.cpu", "b")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK


def test_fleet_status_requires_unmet_only_stays_ok() -> None:
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="requires_unmet")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK


def test_fleet_status_timeout_with_ok_stays_ok() -> None:
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="ok"), _ir("b.insp", "b", status="timeout")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK


def test_fleet_status_all_timeout_degrades_partial() -> None:
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="timeout"), _ir("b.insp", "b", status="timeout")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_fleet_status_target_unreachable_degrades_partial() -> None:
    # spec §场景:真正的失败仍降级 — target_unreachable must not be swallowed
    # by the requires_unmet exemption.
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="ok"), _ir("b.insp", "b", status="target_unreachable")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_fleet_status_exception_degrades_partial() -> None:
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="ok"), _ir("b.insp", "b", status="exception")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_fleet_status_requires_unmet_does_not_mask_exception() -> None:
    r = Report.from_fleet_results(
        [
            _ir("a.insp", "a", status="requires_unmet"),
            _ir("b.insp", "b", status="exception"),
        ],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_fleet_status_override_passes_through() -> None:
    r = Report.from_fleet_results(
        [_ir("a.insp", "a", status="exception")],
        schedule_name="daily",
        started_at=_t0(),
        finished_at=_t1(),
        status=ReportStatus.OK,
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK
