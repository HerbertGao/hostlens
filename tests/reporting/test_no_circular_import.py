"""Verify the forward-ref / model_rebuild dance survives all import orders.

Spec: ``openspec/changes/add-report-data-model/specs/report-data-model/spec.md``
§需求:`reporting.models.Report` 与 `inspectors.result.InspectorResult`
不能形成静态循环导入.

The proposal breaks the cycle by:
1. ``Report.inspector_results`` uses a string forward-ref to
   ``InspectorResult`` guarded by ``TYPE_CHECKING``.
2. ``inspectors/result.py`` ends with
   ``Report.model_rebuild(_types_namespace={...}, force=True)`` to resolve
   the forward-ref at runtime.

To prove the design is correct (not just "works on my machine") this
test fires **four clean subprocesses**, each `python -c ...` invocations
that import in different orders:

- (a) ``inspectors.result`` first, then construct a ``Report`` -> exit 0
- (b) ``reporting`` first, then ``inspectors.result`` -> exit 0
- (c) ``inspectors.result`` then ``Report(...)`` end-to-end -> exit 0
- (d) ``reporting`` only (NO ``inspectors.result`` import) +
       ``Report(...)`` -> exit != 0 with stderr containing
       ``PydanticUndefinedAnnotation``

The fourth case is the "fail loudly" assertion: if a future contributor
removes the ``model_rebuild`` call from ``inspectors/result.py`` the
forward-ref stays unresolved and Pydantic raises a clearly-named
exception rather than silently producing broken validation.
"""

from __future__ import annotations

import subprocess
import sys


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Spawn a clean Python subprocess; capture stdout/stderr text."""

    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_import_inspectors_result_then_report_succeeds() -> None:
    """Case (a): ``inspectors.result`` first, ``Report`` second."""

    result = _run_python(
        "import hostlens.inspectors.result\n"
        "from hostlens.reporting.models import Report\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_import_reporting_then_inspectors_result_succeeds() -> None:
    """Case (b): ``reporting`` first, ``inspectors.result`` second."""

    result = _run_python(
        "import hostlens.reporting\nimport hostlens.inspectors.result\nprint('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_construct_report_after_inspectors_result_imported_succeeds() -> None:
    """Case (c): full end-to-end construction works after the rebuild trigger."""

    code = (
        "import hostlens.inspectors.result\n"
        "from datetime import datetime, timezone\n"
        "from uuid import uuid4\n"
        "from hostlens.inspectors.result import InspectorResult\n"
        "from hostlens.reporting.models import Report\n"
        "ir = InspectorResult(\n"
        "    name='hello.echo', version='1.0.0', status='ok',\n"
        "    target_name='t', duration_seconds=0.01, output={}, findings=[],\n"
        ")\n"
        "started = datetime(2026, 1, 1, tzinfo=timezone.utc)\n"
        "finished = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)\n"
        "r = Report(\n"
        "    report_id=uuid4(), schema_version='1.0', target_name='t',\n"
        "    inspector_results=[ir], findings=[],\n"
        "    started_at=started, finished_at=finished,\n"
        ")\n"
        "print(r.target_name)\n"
    )
    result = _run_python(code)
    assert result.returncode == 0, result.stderr
    assert "t" in result.stdout


def test_construct_report_without_rebuild_trigger_raises_undefined_annotation() -> None:
    """Case (d): skipping ``inspectors.result`` MUST raise a loud,
    *named* error rather than silently producing a half-validated model.

    Two complementary subprocess assertions:

    1. ``Report(...)`` without the rebuild trigger raises
       ``PydanticUserError`` with the spec-mandated
       ``class-not-fully-defined`` hint (Pydantic 2.13's user-facing
       failure mode for partial builds at construction time).
    2. ``Report.model_rebuild()`` without the types namespace surfaces
       the underlying ``PydanticUndefinedAnnotation`` — the exact
       exception class name the spec calls out as the load-bearing
       signal that the forward-ref is unresolved.

    Either path proves "loud failure"; both are checked so a future
    Pydantic behaviour change that flips which path triggers which name
    is detected immediately rather than silently regressing.
    """

    # IMPORTANT: deliberately omit ``import hostlens.inspectors.result``.
    # Without it, ``Report.inspector_results``'s forward-ref to
    # ``InspectorResult`` stays undefined and Pydantic should reject the
    # construction.
    construct_code = (
        "import hostlens.reporting\n"
        "from datetime import datetime, timezone\n"
        "from uuid import uuid4\n"
        "from hostlens.reporting.models import Report\n"
        "started = datetime(2026, 1, 1, tzinfo=timezone.utc)\n"
        "finished = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)\n"
        "Report(\n"
        "    report_id=uuid4(), schema_version='1.0', target_name='t',\n"
        "    inspector_results=[object()],\n"
        "    started_at=started, finished_at=finished,\n"
        ")\n"
    )
    construct_result = _run_python(construct_code)
    assert construct_result.returncode != 0, (
        "expected non-zero exit (forward-ref unresolved); "
        f"stdout={construct_result.stdout!r} stderr={construct_result.stderr!r}"
    )
    # Pydantic 2.13 raises `PydanticUserError` with this canonical
    # message when a partially-built model is instantiated.
    assert "is not fully defined" in construct_result.stderr, (
        f"expected 'is not fully defined' in stderr; got: {construct_result.stderr!r}"
    )

    # Second probe: trigger model_rebuild() without the types namespace
    # to expose the underlying PydanticUndefinedAnnotation exception
    # name (the spec-named signal documented in design.md §决策 1).
    rebuild_code = (
        "import hostlens.reporting\n"
        "from hostlens.reporting.models import Report\n"
        "Report.model_rebuild()\n"
    )
    rebuild_result = _run_python(rebuild_code)
    assert rebuild_result.returncode != 0, (
        f"expected non-zero exit on bare model_rebuild(); stderr={rebuild_result.stderr!r}"
    )
    assert "PydanticUndefinedAnnotation" in rebuild_result.stderr, (
        f"expected 'PydanticUndefinedAnnotation' in stderr; got: {rebuild_result.stderr!r}"
    )
