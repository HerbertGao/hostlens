"""Snapshot tests for the ``nginx.health`` service inspector.

``nginx.health`` is a NO-FINDING failure-tristate probe (``findings: []``): its
job is "is the service up?". Per ``service-inspector-contract`` the dual-track
mechanical gate ("only inspectors whose ``findings`` is non-empty require a
semantic-abnormal fixture") does NOT bind it; instead the suite spec requires it
prove the up→ok / down→exception tristate. So this module carries no
semantic-abnormal fixture, but DOES assert both tristate ends.

Fixtures were recorded by ``_record_nginx.py`` driving the real
``InspectorRunner``:
  * ``health_up`` — host curl against the compose nginx ``/stub_status`` on
    :18080 → ok, with ``active_connections`` parsed out of the stub_status text
    (proves curl truly hit stub_status, not just any 200 path — guards against a
    false-green up state).
  * ``health_down`` — host curl against a dead port (:18099, nothing listening)
    → curl non-zero → status=exception (fail-loud, never a fabricated healthy
    object).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "nginx"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "nginx" / "health.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("nginx-health-test"),
    )


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "nginx.health"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["curl"]
    # No-finding failure-tristate inspector.
    assert manifest.findings == []
    assert manifest.secrets == []
    assert "nginx" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # No hook.py sibling — pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_up_ok_parses_active_connections() -> None:
    """Reachable stub_status → status=ok with the integer ``active_connections``
    lifted out of the stub_status text. Asserting that field exists proves curl
    hit the real stub_status endpoint (not an arbitrary 200 page) — the no-finding
    up-state false-green guard.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "health_up.json")

    result = await _runner().run(
        manifest,
        replay,
        {"host": "127.0.0.1", "port": 18080, "stub_status_path": "/stub_status"},
    )

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []
    assert result.output["healthy"] is True
    assert isinstance(result.output["active_connections"], int)
    assert result.output["active_connections"] >= 1


async def test_down_fails_loud() -> None:
    """nginx unreachable (dead port) → curl non-zero → status=exception. The
    no-finding inspector never fabricates a healthy object for a down backend.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "health_down.json")

    result = await _runner().run(
        manifest,
        replay,
        {"host": "127.0.0.1", "port": 18099, "stub_status_path": "/stub_status"},
    )

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


class _NoBinaryTarget:
    """Stub target where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when curl is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_curl_binary_requires_unmet() -> None:
    """A target without the curl client → preflight requires_unmet skip (a
    premise gap), NOT an exception."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing
