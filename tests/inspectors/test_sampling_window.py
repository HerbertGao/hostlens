"""Tests for `collect.sampling_window` injection + injectable clock.

Covers the inspector-plugin-system delta:

  (a) window vars reach the rendered command; `window_start` is exactly
      `duration` seconds before `window_end`; both use `YYYY-MM-DD HH:MM:SS`.
  (b) `window_seconds` reaches the Finding DSL evaluation context.
  (c) omitting `sampling_window` injects none of the three vars and keeps the
      pre-delta behaviour byte-identical.
  (d) under a frozen clock two renders of the same window Inspector produce a
      byte-identical command.
  (e) a `parameters` property colliding with a reserved window name is
      rejected by the loader.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.schema import InspectorManifest
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


class _RecordingTarget:
    """Minimal in-memory `ExecutionTarget` that records every command it sees.

    `command -v <bin>` probes succeed (exit 0); the main command returns a
    canned stdout. We capture the *rendered* command strings so tests can
    assert exactly what the runner produced.
    """

    def __init__(self, *, main_stdout: str) -> None:
        self.name = "rec"
        self.type = "local"
        self.commands: list[str] = []
        self._main_stdout = main_stdout

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.commands.append(cmd)
        if cmd.startswith("command -v "):
            return ExecResult(
                stdout="/usr/bin/probe",
                stderr="",
                exit_code=0,
                duration_seconds=0.0,
                timed_out=False,
            )
        return ExecResult(
            stdout=self._main_stdout,
            stderr="",
            exit_code=0,
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise NotImplementedError


def _runner(clock: Any = None) -> Any:
    from hostlens.inspectors.runner import InspectorRunner

    logger = structlog.get_logger()
    settings = Settings()
    registry = TargetRegistry()
    if clock is None:
        return InspectorRunner(registry, settings=settings, logger=logger)
    return InspectorRunner(registry, settings=settings, logger=logger, clock=clock)


def _frozen_clock() -> datetime:
    return datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


# kv manifest whose command embeds the window strings and whose finding rule
# references `window_seconds`.
_WINDOW_MANIFEST: dict[str, Any] = {
    "name": "log.tail.error_burst",
    "version": "1.0.0",
    "description": "Count errors in a time window.",
    "targets": ["local"],
    "requires_capabilities": ["shell"],
    "collect": {
        "command": ('echo "since={{ window_start }} until={{ window_end }} "; echo error_count=7'),
        "sampling_window": {"duration_seconds": 300},
    },
    "parse": {"format": "kv"},
    "output_schema": {
        "type": "object",
        "properties": {"error_count": {"type": "string"}},
        "additionalProperties": True,
    },
    "findings": [
        {
            "when": "int(error_count) > 0 and window_seconds == 300",
            "severity": "warning",
            "message": "errors {error_count} over {window_seconds}s window",
        }
    ],
}


@pytest.mark.asyncio
async def test_window_vars_in_rendered_command() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)
    target = _RecordingTarget(main_stdout="error_count=7\n")
    result = await _runner(clock=_frozen_clock).run(manifest, target)

    assert result.status == "ok"
    main_cmd = next(c for c in target.commands if "since=" in c)
    m = re.search(
        r"since=(?P<start>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
        r"until=(?P<end>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ",
        main_cmd,
    )
    assert m is not None, main_cmd
    start, end = m.group("start"), m.group("end")

    fmt = "%Y-%m-%d %H:%M:%S"
    start_dt = datetime.strptime(start, fmt)
    end_dt = datetime.strptime(end, fmt)
    assert (end_dt - start_dt).total_seconds() == 300
    # Format assertion: exactly `YYYY-MM-DD HH:MM:SS`, no `T` / timezone offset.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", start)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", end)
    assert "T" not in start and "+" not in start


@pytest.mark.asyncio
async def test_window_seconds_in_dsl_context() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)
    target = _RecordingTarget(main_stdout="error_count=7\n")
    result = await _runner(clock=_frozen_clock).run(manifest, target)

    assert result.status == "ok"
    # The finding fires only when `window_seconds == 300` is true in the DSL
    # context, so a single warning finding proves the variable reached it.
    assert len(result.findings) == 1
    assert result.findings[0].severity == "warning"
    assert "300s window" in result.findings[0].message


@pytest.mark.asyncio
async def test_omitted_window_no_vars_and_old_behaviour() -> None:
    no_window: dict[str, Any] = {
        "name": "system.no_window",
        "version": "1.0.0",
        "description": "No sampling window.",
        "targets": ["local"],
        "requires_capabilities": ["shell"],
        "collect": {"command": "echo error_count=1"},
        "parse": {"format": "kv"},
        "output_schema": {
            "type": "object",
            "properties": {"error_count": {"type": "string"}},
            "additionalProperties": True,
        },
        "findings": [
            # Referencing a window var must raise NameNotDefined → rule skipped,
            # proving the var is absent from the DSL context (old behaviour).
            {
                "when": "window_seconds == 300",
                "severity": "warning",
                "message": "should never fire",
            },
        ],
    }
    manifest = InspectorManifest.model_validate(no_window)
    assert manifest.collect.sampling_window is None

    target = _RecordingTarget(main_stdout="error_count=1\n")
    result = await _runner(clock=_frozen_clock).run(manifest, target)

    assert result.status == "ok"
    # No window vars rendered into the command.
    main_cmd = next(c for c in target.commands if "error_count" in c)
    assert "window_start" not in main_cmd
    assert "window_end" not in main_cmd
    # DSL context lacks `window_seconds` → finding skipped, none produced.
    assert result.findings == []


@pytest.mark.asyncio
async def test_frozen_clock_byte_stable_command() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)
    clock = _frozen_clock

    t1 = _RecordingTarget(main_stdout="error_count=7\n")
    t2 = _RecordingTarget(main_stdout="error_count=7\n")
    await _runner(clock=clock).run(manifest, t1)
    await _runner(clock=clock).run(manifest, t2)

    cmd1 = next(c for c in t1.commands if "since=" in c)
    cmd2 = next(c for c in t2.commands if "since=" in c)
    assert cmd1 == cmd2


def test_reserved_window_param_rejected_by_loader(tmp_path: Path) -> None:
    manifest_yaml = """
name: bad.reserved
version: 1.0.0
description: declares a reserved window param name
targets: [local]
requires_capabilities: [shell]
parameters:
  type: object
  properties:
    window_start:
      type: string
      pattern: "^[a-z]+$"
collect:
  command: "echo {{ window_start | sh }}"
parse:
  format: kv
output_schema:
  type: object
  additionalProperties: true
"""
    path = tmp_path / "bad.yaml"
    path.write_text(manifest_yaml)

    with pytest.raises(InspectorError) as exc_info:
        load_manifest(path)
    assert exc_info.value.kind == "parameter_reserved_window_name"
