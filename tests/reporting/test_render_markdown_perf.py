"""Performance smoke test for `render_markdown.render`.

Spec §需求:`render_markdown.render` declares single-call latency must
be < 50ms for a Report with one Inspector and zero findings (M1 scope).
The bound is generous; this test exists to catch obvious regressions
(e.g. accidental O(n^2) string concatenation).
"""

from __future__ import annotations

import time
from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Report
from hostlens.reporting.render_markdown import render


def _make_minimal_report() -> Report:
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.01,
        output={"greeting": "hello"},
        findings=[],
        error=None,
        missing=[],
    )
    t = datetime(2026, 5, 26, 12, 0, 0)
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="local-host",
        inspector_results=[ir],
        findings=[],
        started_at=t,
        finished_at=t,
        metadata={},
    )


def test_single_render_under_50ms() -> None:
    report = _make_minimal_report()
    # Warm up once (import / Pydantic field caches).
    render(report)
    start = time.perf_counter()
    render(report)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 50.0, f"render took {elapsed_ms:.2f}ms (budget 50ms)"


def test_render_returns_nonempty_str() -> None:
    out = render(_make_minimal_report())
    assert isinstance(out, str)
    assert out.startswith("# Hostlens Inspection Report")
