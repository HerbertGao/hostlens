"""Snapshot tests for the ``docker.containers.restart_loop`` builtin inspector.

These run the **real** ``InspectorRunner`` against ``ReplayTarget`` fixtures
recorded from a live Docker host (see ``tests/inspectors/fixtures/
docker_restart_loop_*.json``), so they exercise the full
``preflight → render → collect → parse → findings`` path **offline** — zero
Docker daemon, zero real host.

The inspector proves load-bearing wall 1 (all extraction / derivation in the
collector: the ``docker ps`` → ``docker inspect`` join, the ``/``-stripped name,
the State/Health projection, and the ``{results: [...]}`` wrapping are all done
in shell + jq; the Finding DSL only compares the ready scalar fields) and wall 2
(cross-container correlation happens inside the collector; the single
``for_each`` binding iterates one already-joined container).

Recorded scenarios:

  * ``loop``      — a restart-loop container (RestartCount > threshold) alongside
                    a healthy one → one ``critical`` restart-loop finding.
  * ``unhealthy`` — a running container reporting ``health=unhealthy`` → one
                    ``critical`` unhealthy finding.
  * ``empty``     — no matching container (``docker ps`` succeeds, zero ids) →
                    ``{results: []}`` → zero findings (genuine empty set).
  * ``daemon_down`` — the Docker daemon is unreachable so ``docker ps`` exits
                    non-zero with empty stdout → ``status=exception`` (honesty
                    regression lock, Authoring Contract rule 8): a dead daemon
                    must NOT be fabricated into a healthy ``{results: []}``.

See ``_record_docker_restart_loop.py`` for the recorder.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hostlens"
    / "inspectors"
    / "builtin"
    / "docker"
    / "containers_restart_loop.yaml"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


async def _run(fixture: str, *, name_filter: str) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST_PATH)
    replay = ReplayTarget("docker", fixture=_FIXTURE_DIR / fixture)
    result = await _runner().run(manifest, replay, {"name_filter": name_filter})
    return replay, result


async def test_restart_loop_detected() -> None:
    replay, result = await _run("docker_restart_loop_loop.json", name_filter="hlc-")

    # Strict consumption: every command the runner sent hit the fixture.
    assert replay.misses == []
    assert result.status == "ok"

    # Collector already joined ps + inspect and derived the scalar fields; the
    # DSL only compared restart_count > threshold (default 5).
    assert result.output == {
        "results": [
            {
                "name": "hlc-rl-test",
                "restart_count": 6,
                "state": "restarting",
                "health": "none",
            },
            {
                "name": "hlc-ok-test",
                "restart_count": 0,
                "state": "running",
                "health": "none",
            },
        ]
    }

    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "container hlc-rl-test in restart loop (RestartCount=6)"),
    ]


async def test_cold_target_without_docker_cli_capability_still_runs() -> None:
    # Regression lock (Authoring Contract rule 9): Local/SSH targets add the
    # `docker_cli` capability only LAZILY (after the first exec), but the runner
    # checks capabilities (preflight step 2) before any exec/binary probe
    # (step 5). So the manifest must gate on the `docker` binary, NOT
    # `requires_capabilities: [docker_cli]` — otherwise a Docker-capable host
    # fails preflight with `requires_unmet` and the inspector never runs. This
    # fixture carries only the cold-target caps {shell, file_read} (no
    # `docker_cli`); the inspector must still reach status=ok, not requires_unmet.
    replay, result = await _run("docker_restart_loop_cold_target.json", name_filter="hlc-")

    assert result.status != "requires_unmet", "cold target wrongly gated out by capability"
    assert result.status == "ok"
    assert replay.misses == []
    assert [f.severity for f in result.findings] == ["critical"]


async def test_unhealthy_detected() -> None:
    replay, result = await _run("docker_restart_loop_unhealthy.json", name_filter="hlc-unhealthy")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "results": [
            {
                "name": "hlc-unhealthy-test",
                "restart_count": 0,
                "state": "running",
                "health": "unhealthy",
            }
        ]
    }

    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "container hlc-unhealthy-test reports health=unhealthy"),
    ]


async def test_empty_set_no_findings() -> None:
    """No matching container → ``{results: []}`` → zero findings (empty-set)."""

    replay, result = await _run(
        "docker_restart_loop_empty.json", name_filter="no-such-container-zzz"
    )

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


async def test_daemon_down_fails_loud() -> None:
    """Docker daemon unreachable → status=exception, NOT a fabricated healthy
    ``{results: []}``. The honesty regression lock: ``docker ps`` exits non-zero
    with empty stdout, so the runner collapses to status=exception instead of
    blessing a dead daemon as "no restart-loop containers" (Authoring Contract
    rule 8).
    """

    replay, result = await _run("docker_restart_loop_daemon_down.json", name_filter="hlc-")

    assert replay.misses == []
    assert result.status != "ok"
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []
