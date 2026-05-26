"""Env-var literal pass-through across both render boundaries.

`render_markdown.render` already has coverage in
``test_render_markdown_escape_and_env_var.py`` for the `$VAR` /
`${VAR}` non-expansion contract. This module additionally pins the
same contract for `render_json.render` so that the JSON sink (used by
``hostlens inspect --format json``, ``--output`` files, and any
downstream MCP client) cannot accidentally start expanding
``Evidence.command`` env-var references either.

The contract is single-sourced in proposal §Security & Secrets and in
`docs/operations/inspect.md` → "Redaction boundary": the renderer must
emit the manifest template string verbatim and must not call
``os.path.expandvars`` (or any equivalent) on user-supplied fields.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_json import render as render_json
from hostlens.reporting.render_markdown import render as render_md

SENTINEL_HOST = "db.prod.internal-do-not-leak"
SENTINEL_USER = "admin-do-not-leak"


def _report_with_env_var_command() -> Report:
    ev = Evidence(
        kind="command_output",
        command="psql -h $PGHOST -U ${PGUSER}",
        stdout="",
    )
    finding = Finding(severity="info", message="env probe", evidence=[ev])
    ir = InspectorResult(
        name="demo.env",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.01,
        output={},
        findings=[finding],
        error=None,
        missing=[],
    )
    t = datetime(2026, 5, 26, 12, 0, 0)
    return Report.from_inspector_results(
        "local-host",
        [ir],
        started_at=t,
        finished_at=t,
    )


def test_render_markdown_does_not_expand_env_vars(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Markdown sink keeps `$PGHOST` / `${PGUSER}` verbatim even when
    the env vars are set in the process environment.
    """
    monkeypatch.setenv("PGHOST", SENTINEL_HOST)
    monkeypatch.setenv("PGUSER", SENTINEL_USER)

    out = render_md(_report_with_env_var_command())

    assert "psql -h $PGHOST -U ${PGUSER}" in out
    assert SENTINEL_HOST not in out
    assert SENTINEL_USER not in out
    # Sanity check: the env vars *are* expandable — proves the
    # renderer is deliberately not calling expandvars.
    assert os.path.expandvars("$PGHOST") == SENTINEL_HOST


def test_render_json_does_not_expand_env_vars(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """JSON sink keeps `$PGHOST` / `${PGUSER}` verbatim and parses
    back as a `Report` whose `Evidence.command` still carries the
    literal template string.
    """
    monkeypatch.setenv("PGHOST", SENTINEL_HOST)
    monkeypatch.setenv("PGUSER", SENTINEL_USER)

    raw = render_json(_report_with_env_var_command())

    assert SENTINEL_HOST not in raw
    assert SENTINEL_USER not in raw

    data = json.loads(raw)
    command = data["inspector_results"][0]["findings"][0]["evidence"][0]["command"]
    assert command == "psql -h $PGHOST -U ${PGUSER}"
