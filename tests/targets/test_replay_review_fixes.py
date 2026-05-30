"""Regression tests for PR #38 review findings (add-incident-pack).

Two Copilot review comments were valid ship-blockers that landed before the
fixes; these tests lock in the fixes so they cannot silently regress:

  1. ``ReplayTarget`` built its command index with a dict-comprehension, which
     silently overwrote earlier entries when two recorded commands normalised
     to the same match key (duplicate ``cmd`` / trailing-whitespace-only diff).
     That defeats the loud-failure contract — a fixture authoring error must
     raise, not return the wrong recorded result.
  2. ``InspectorRunner._build_window_context`` formatted whatever timezone the
     injected ``clock()`` returned, but ``sampling_window`` promises UTC. A
     non-UTC (or naive) clock would produce wrong window strings and break
     replay fixture stability.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import InspectorManifest
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

# --------------------------------------------------------------------------- #
# Fix 1 — duplicate command in fixture must raise, not silently overwrite.
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(data))
    return path


def test_duplicate_command_exact_raises(tmp_path: Path) -> None:
    fixture = _write(
        tmp_path,
        {
            "impersonate": "local",
            "capabilities": ["shell"],
            "commands": [
                {"cmd": "uptime", "stdout": "A\n", "exit_code": 0},
                {"cmd": "uptime", "stdout": "B\n", "exit_code": 0},
            ],
            "files": {},
        },
    )
    with pytest.raises(ConfigError) as exc:
        ReplayTarget(name="replay-host", fixture=fixture)
    assert exc.value.kind == "replay_fixture_duplicate_command"  # type: ignore[attr-defined]


def test_duplicate_command_trailing_whitespace_only_raises(tmp_path: Path) -> None:
    # The two commands differ only by trailing per-line whitespace, so they
    # normalise to the same match key — exactly the silent-overwrite hazard.
    fixture = _write(
        tmp_path,
        {
            "impersonate": "local",
            "capabilities": ["shell"],
            "commands": [
                {"cmd": "df -h", "stdout": "first\n", "exit_code": 0},
                {"cmd": "df -h   ", "stdout": "second\n", "exit_code": 0},
            ],
            "files": {},
        },
    )
    with pytest.raises(ConfigError) as exc:
        ReplayTarget(name="replay-host", fixture=fixture)
    assert exc.value.kind == "replay_fixture_duplicate_command"  # type: ignore[attr-defined]


def test_distinct_commands_still_build(tmp_path: Path) -> None:
    # Sanity: distinct commands index without error (no false positives).
    fixture = _write(
        tmp_path,
        {
            "impersonate": "local",
            "capabilities": ["shell"],
            "commands": [
                {"cmd": "uptime", "stdout": "A\n", "exit_code": 0},
                {"cmd": "df -h", "stdout": "B\n", "exit_code": 0},
            ],
            "files": {},
        },
    )
    target = ReplayTarget(name="replay-host", fixture=fixture)
    assert target.misses == []


# --------------------------------------------------------------------------- #
# Fix 2 — window strings are UTC regardless of the injected clock's tzinfo.
# --------------------------------------------------------------------------- #


_WINDOW_MANIFEST: dict[str, Any] = {
    "name": "test.window.echo",
    "version": "1.0.0",
    "description": "Echo the sampling window for assertion.",
    "tags": ["test"],
    "targets": ["local"],
    "collect": {
        "command": 'echo "start={{ window_start }} end={{ window_end }}"',
        "timeout_seconds": 5,
        "sampling_window": {"duration_seconds": 300},
    },
    "parse": {"format": "kv"},
    "output_schema": {
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
    },
    "findings": [],
}


class _CaptureTarget:
    """In-memory target that records the rendered main command string."""

    def __init__(self) -> None:
        self.name = "cap"
        self.type = "local"
        self.commands: list[str] = []

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.SHELL}

    async def exec(
        self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.commands.append(cmd)
        return ExecResult(
            stdout="start=x end=y\n",
            stderr="",
            exit_code=0,
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise NotImplementedError


def _runner(clock: Any) -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger(),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_non_utc_aware_clock_normalised_to_utc() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)

    # An aware clock 8 hours ahead of UTC. The same instant in UTC is
    # 2026-05-31 04:00:00, so the rendered window must use the UTC wall-clock
    # (NOT the +08:00 local wall-clock 12:00:00).
    tz8 = timezone(timedelta(hours=8))
    instant = datetime(2026, 5, 31, 12, 0, 0, tzinfo=tz8)
    target = _CaptureTarget()
    await _runner(lambda: instant).run(manifest, target)

    main = next(c for c in target.commands if c.startswith("echo "))
    assert "end=2026-05-31 04:00:00" in main
    assert "start=2026-05-31 03:55:00" in main  # 300s earlier, in UTC


@pytest.mark.asyncio
async def test_naive_clock_treated_as_utc() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)

    # A naive datetime is interpreted as UTC (documented default-clock contract).
    instant = datetime(2026, 5, 31, 9, 0, 0)
    target = _CaptureTarget()
    await _runner(lambda: instant).run(manifest, target)

    main = next(c for c in target.commands if c.startswith("echo "))
    assert "end=2026-05-31 09:00:00" in main
    assert "start=2026-05-31 08:55:00" in main


@pytest.mark.asyncio
async def test_utc_aware_clock_unchanged() -> None:
    manifest = InspectorManifest.model_validate(_WINDOW_MANIFEST)

    instant = datetime(2026, 5, 31, 9, 0, 0, tzinfo=UTC)
    target = _CaptureTarget()
    await _runner(lambda: instant).run(manifest, target)

    main = next(c for c in target.commands if c.startswith("echo "))
    assert "end=2026-05-31 09:00:00" in main
    assert "start=2026-05-31 08:55:00" in main
