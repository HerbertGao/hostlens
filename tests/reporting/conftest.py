"""Test-scoped forward-ref resolution for `Report.inspector_results`.

The production `Report.model_rebuild(_types_namespace={"InspectorResult":
...}, force=True)` hook already runs at the bottom of
`hostlens/inspectors/result.py` (see add-report-data-model task 5.4).
This conftest still calls `model_rebuild` as a safety net for the rare
case where a reporting test imports `hostlens.reporting.models.Report`
without first (transitively) importing `hostlens.inspectors.result` —
kept for defense in depth; the call is idempotent and harmless once
the production hook has already executed.
"""

from __future__ import annotations

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Report

Report.model_rebuild(_types_namespace={"InspectorResult": InspectorResult}, force=True)
