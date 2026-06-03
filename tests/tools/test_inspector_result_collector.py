"""Tests for the per-run `InspectorResultCollector` (task 1.1).

(a) append + snapshot preserve insertion order and the complete `InspectorResult`
    (real status / version / duration / findings).
(b) each instance is independent (per-run, not module-global).
(c) snapshot returns a copy, not the live internal list.
"""

from __future__ import annotations

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.tools.inspector_result_collector import InspectorResultCollector


def _result(
    name: str = "linux.load",
    version: str = "1.0.0",
    status: str = "ok",
    *,
    messages: list[str] | None = None,
) -> InspectorResult:
    findings = [
        Finding(
            severity="warning",
            message=m,
            id=compute_finding_id(name, version, m),
            inspector_name=name,
            inspector_version=version,
        )
        for m in (messages or [])
    ]
    return InspectorResult(
        name=name,
        version=version,
        status=status,  # type: ignore[arg-type]
        target_name="local-host",
        duration_seconds=0.42,
        findings=findings,
    )


def test_append_then_snapshot_preserves_complete_result() -> None:
    collector = InspectorResultCollector()
    r = _result(messages=["disk full"])
    collector.append(r)
    snap = collector.snapshot()
    assert snap == [r]
    only = snap[0]
    assert only.status == "ok"
    assert only.version == "1.0.0"
    assert only.duration_seconds == 0.42
    assert [f.message for f in only.findings] == ["disk full"]


def test_snapshot_is_insertion_ordered() -> None:
    collector = InspectorResultCollector()
    a = _result(name="alpha.probe")
    b = _result(name="beta.probe")
    collector.append(a)
    collector.append(b)
    assert [r.name for r in collector.snapshot()] == ["alpha.probe", "beta.probe"]


def test_snapshot_returns_a_copy_not_the_live_list() -> None:
    collector = InspectorResultCollector()
    collector.append(_result())
    snap = collector.snapshot()
    snap.clear()
    # The internal list is untouched by mutating the returned copy.
    assert len(collector.snapshot()) == 1


def test_instances_are_independent_not_module_global() -> None:
    one = InspectorResultCollector()
    two = InspectorResultCollector()
    one.append(_result())
    assert one.snapshot() != []
    assert two.snapshot() == []
