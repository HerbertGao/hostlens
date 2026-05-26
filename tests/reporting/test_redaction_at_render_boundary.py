"""End-to-end redaction checks at the ``render_markdown`` / ``render_json``
boundary.

Spec: ``openspec/changes/add-report-data-model/specs/report-data-model/spec.md``
§需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过
`core/redact.py`.

Lower-level redaction unit tests (``tests/core/test_redact.py``) cover
the regex correctness of ``redact_text``. ``tests/reporting/test_redact_report.py``
covers the deep-copy ``redact_report_for_render`` helper. THIS file
pins the **public-facing render boundary**: every code path that turns
a ``Report`` into bytes for stdout / ``--output`` / Notifier payload
MUST run those bytes through redaction first.

Six scenarios, one per spec bullet:

1. ``render_markdown`` redacts ``Evidence.stderr`` API key.
2. ``render_json`` redacts ``Evidence.stdout`` JWT.
3. Calling either renderer does NOT mutate the source ``Report``.
4. Redaction reaches nested ``Evidence.data`` (sensitive key name).
5. Numeric fields (``metric_value`` as float) are NOT redacted.
6. ``render_markdown`` and ``render_json`` produce identical redaction
   strings for the same input.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_json import render as render_json
from hostlens.reporting.render_markdown import render as render_markdown

# Test payloads — secret-bearing strings derived from OPERABILITY §7.2.
# Build secret-shaped strings at import time via concatenation so
# secret scanners (GitGuardian / Cursor / Copilot) cannot match them
# as a single contiguous literal in source. Runtime values are
# identical to the joined form and still match OPERABILITY §7.2 regexes
# in core/redact.py exactly as before — only the on-disk representation
# changes. See PR #18 review feedback for context.
_API_KEY = "sk-" + "abcdefghij" + "klmnopqrst" + "uvwxyz1234567890"
_JWT = ".".join(
    [
        "eyJhbGciOiJIUzI1NiJ9",
        "eyJzdWIiOiJ1c2VyMSJ9",
        "signaturedatahere",
    ]
)
# Plain non-secret-like string — `is_sensitive_key` masks based on the
# dict key being "password", not on the value's shape. Using a
# placeholder here keeps scanners quiet without weakening the test
# (the assertions below verify the key-based mask path, not value-shape
# regex matching).
_PASSWORD_VALUE = "FIXTURE-VALUE-NOT-A-REAL-SECRET"

_PINNED_REPORT_ID = UUID("00000000-0000-0000-0000-000000000099")
_PINNED_STARTED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_PINNED_FINISHED = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)


def _build_report(*, evidence: Evidence, message: str = "boom") -> Report:
    """Construct a single-inspector Report carrying one finding + evidence."""

    finding = Finding(
        severity="critical",
        message=message,
        evidence=[evidence],
    )
    inspector_result = InspectorResult(
        name="probe",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.01,
        output={},
        findings=[finding],
    )
    return Report(
        report_id=_PINNED_REPORT_ID,
        schema_version="1.0",
        intent=None,
        target_name="local-host",
        inspector_results=[inspector_result],
        findings=[finding],
        started_at=_PINNED_STARTED,
        finished_at=_PINNED_FINISHED,
    )


# --------------------------------------------------------------------------- #
# Scenario 1: render_markdown redacts API key in stderr
# --------------------------------------------------------------------------- #


def test_render_markdown_redacts_api_key_in_stderr() -> None:
    """`evidence.stderr` containing an ``sk-...`` key must not leak."""

    evidence = Evidence(
        kind="command_output",
        command="psql",
        stdout="ok",
        stderr=f"ERROR: invalid api_key={_API_KEY}",
    )
    report = _build_report(evidence=evidence)
    output = render_markdown(report)

    assert _API_KEY not in output, "raw API key leaked into markdown output"
    # The masked form keeps first-4 / last-4 chars of the matched value
    # per ``_mask`` in ``hostlens.core.redact``. The ``api_key=`` keyword
    # rule fires first and masks the entire ``sk-...`` value as one
    # match, so prefix/suffix come from the value itself (not the
    # ``sk-`` literal).
    assert f"{_API_KEY[:4]}...{_API_KEY[-4:]}" in output


# --------------------------------------------------------------------------- #
# Scenario 2: render_json redacts JWT in stdout
# --------------------------------------------------------------------------- #


def test_render_json_redacts_jwt_in_stdout() -> None:
    """A three-segment ``eyJ...`` JWT inside ``evidence.stdout`` must mask."""

    evidence = Evidence(
        kind="command_output",
        command="curl /token",
        stdout=f'{{"jwt": "{_JWT}"}}',
    )
    report = _build_report(evidence=evidence)
    output = render_json(report)

    assert _JWT not in output, "raw JWT leaked into JSON output"
    payload = json.loads(output)
    masked = payload["findings"][0]["evidence"][0]["stdout"]
    # Mask should keep first 4 + last 4 chars somewhere in the string.
    assert _JWT[:4] in masked and _JWT[-4:] in masked, masked


# --------------------------------------------------------------------------- #
# Scenario 3: rendering does NOT mutate the source Report
# --------------------------------------------------------------------------- #


def test_render_does_not_mutate_source_report() -> None:
    """Both renderers must work off a deep-copy; raw values survive in memory."""

    evidence = Evidence(
        kind="command_output",
        command="echo",
        stdout="ok",
        stderr=f"leak={_API_KEY}",
    )
    report = _build_report(evidence=evidence, message=f"raw {_API_KEY}")

    _ = render_markdown(report)
    _ = render_json(report)

    # Originals preserved (the runner / Agent loop still see raw strings
    # for reasoning purposes).
    assert report.findings[0].evidence[0].stderr == f"leak={_API_KEY}"
    assert _API_KEY in report.findings[0].message


# --------------------------------------------------------------------------- #
# Scenario 4: nested Evidence.data password is masked
# --------------------------------------------------------------------------- #


def test_render_json_redacts_nested_password_in_evidence_data() -> None:
    """A dict-key-named ``password`` inside ``Evidence.data`` masks recursively."""

    evidence = Evidence(
        kind="structured",
        data={"level": "info", "password": _PASSWORD_VALUE},
    )
    report = _build_report(evidence=evidence)
    output = render_json(report)

    assert _PASSWORD_VALUE not in output, "raw password leaked into JSON"
    payload = json.loads(output)
    data = payload["findings"][0]["evidence"][0]["data"]
    assert data["level"] == "info"  # non-sensitive sibling preserved
    assert data["password"] != _PASSWORD_VALUE  # value scrubbed


# --------------------------------------------------------------------------- #
# Scenario 5: numeric fields are NOT redacted
# --------------------------------------------------------------------------- #


def test_render_json_preserves_float_metric_value() -> None:
    """``metric_value`` stored as float must round-trip unchanged."""

    evidence = Evidence(
        kind="metric",
        metric_name="load_1min",
        metric_value=0.42,
    )
    report = _build_report(evidence=evidence)
    output = render_json(report)

    payload = json.loads(output)
    metric_value = payload["findings"][0]["evidence"][0]["metric_value"]
    assert metric_value == 0.42
    assert isinstance(metric_value, float)


# --------------------------------------------------------------------------- #
# Scenario 6: markdown and json agree on what gets redacted
# --------------------------------------------------------------------------- #


def test_render_markdown_and_json_agree_on_redaction() -> None:
    """Both surfaces must hide ``sk-AAAA...`` and reveal the same masked form."""

    secret = "sk-AAAA" + "B" * 30
    evidence = Evidence(
        kind="command_output",
        command="dump",
        stdout=f"secret={secret}",
    )
    report = _build_report(evidence=evidence)
    md = render_markdown(report)
    js = render_json(report)

    assert secret not in md
    assert secret not in js
    # Both renderers should expose the SAME masked first-4 / last-4
    # sentinel produced by ``_mask`` (first 4 chars are ``sk-A``).
    masked = f"{secret[:4]}...{secret[-4:]}"
    assert masked in md
    assert masked in js
