"""Snapshot tests for the ``mysql.connection_usage`` service inspector.

Service-inspector-contract probe for the NEW client (``mysql``) + a secret
declared as ``HOSTLENS_MYSQL_PWD`` (the ``HOSTLENS_`` prefix per the
``ssh-execution-target`` contract) remapped to the client-native ``MYSQL_PWD``
env + a TSV→JSON collector normalization that never touches a table parser.

The fixtures were recorded by the dev-tool recorder driving the real
``InspectorRunner`` against the pinned compose ``mysql`` / ``mysql-abnormal``
services, so the recorded command strings are byte-identical to what the runner
sends — replay hits with zero ``misses``. See
``_record_mysql_connection_usage.py`` for the recorder.

Failure-classification locks (design D-3):
  * ``test_access_denied_fails_loud`` — an auth-failed backend surfaces as
    ``status=exception``, NOT a fabricated healthy ``used_pct=0``.
  * ``test_conn_refused_fails_loud`` — an unreachable backend (port closed on
    the host) surfaces as ``status=exception`` (host-level client non-zero), not
    a silent zero.
  * ``test_missing_mysql_binary_requires_unmet`` /
    ``test_missing_secret_env_requires_unmet`` — a missing client binary / a
    missing declared secret skip the run as ``requires_unmet`` (a premise gap),
    distinct from the ``exception`` reachability failures.

``test_lowpriv_user_sees_global_threads_connected`` is the processlist-undercount
regression lock: ``SHOW GLOBAL STATUS LIKE 'Threads_connected'`` returns the
GLOBAL connection count even for a non-PROCESS-privileged user — unlike
``COUNT(*) FROM information_schema.processlist``, which such a user would see
only its own thread for (silent under-count → false "healthy" while the backend
is connection-saturated).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "mysql"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "mysql" / "connection_usage.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("mysql-connection-usage-test"),
    )


@pytest.fixture(autouse=True)
def _mysql_pwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_MYSQL_PWD as a secret; preflight requires it
    # in the environment. ReplayTarget neither matches on nor stores env, so the
    # value here is irrelevant to command matching — it only has to be PRESENT so
    # preflight's secret-presence gate passes. Tests asserting the missing-secret
    # path delenv it explicitly.
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", "test-" + "pw")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "mysql.connection_usage"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["mysql"]
    assert manifest.secrets == ["HOSTLENS_MYSQL_PWD"]
    # Version premise declared via tags (no `+`) + free-text description.
    assert "mysql57" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # No hook.py sibling — the metrics-only path is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "used_connections": 1,
        "max_connections": 151,
        "used_pct": 0.66,
    }
    # 0.66% is far below the default warn (80) / critical (95) thresholds.
    assert result.findings == []


async def test_finding_trigger_warning() -> None:
    """Healthy server + a LOW warn_used_pct param → the real (low) used_pct
    crosses warn → a single warning. Verifies the finding-wiring track (D-4).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "finding_trigger.json")

    result = await _runner().run(manifest, replay, {"user": "root", "warn_used_pct": 0.5})

    assert replay.misses == []
    assert result.status == "ok"
    assert [f.severity for f in result.findings] == ["warning"]
    assert "0.66%" in result.findings[0].message


async def test_semantic_abnormal_critical_at_default_thresholds() -> None:
    """The mysql-abnormal instance (max-connections=5) saturated with held
    connections → used_pct=100 → critical AT THE DEFAULT thresholds (95), a
    genuine high-connection state, not a lowered inspector threshold (D-4).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "semantic_abnormal.json")

    # No threshold override — exercises the DEFAULT warn 80 / critical 95.
    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "used_connections": 5,
        "max_connections": 5,
        "used_pct": 100.0,
    }
    assert [f.severity for f in result.findings] == ["critical"]
    assert "100.0%" in result.findings[0].message
    assert "(5/5)" in result.findings[0].message


async def test_access_denied_fails_loud() -> None:
    """Auth failure (wrong password) → status=exception, NOT a fabricated
    healthy used_pct=0. The honesty regression lock (design D-3): mysql exits
    non-zero with empty stdout, so the runner collapses to status=exception
    instead of blessing an auth-failed backend as "healthy".
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "access_denied.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (port closed on the host) → status=exception. The
    server-side port being closed leaves the host-level mysql client exiting
    non-zero → exception, distinct from a target_unreachable (the HOST being
    unreachable), which is an orthogonal transport-layer state (design D-3).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, {"user": "root", "port": 13999})

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
        raise AssertionError(f"collector must not run when mysql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_mysql_binary_requires_unmet() -> None:
    """A target without the mysql client → preflight requires_unmet skip (a
    premise gap), NOT an error that aborts the run (design D-3)."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, {"user": "root"})  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared secret absent from the environment → preflight requires_unmet
    skip (a premise gap), NOT an exception (design D-3). The collector is never
    reached, so the fixture's commands do not matter — only the preflight gate.
    """

    monkeypatch.delenv("HOSTLENS_MYSQL_PWD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert result.status == "requires_unmet"
    assert result.missing == ["env:HOSTLENS_MYSQL_PWD"]
    assert result.output == {}


async def test_lowpriv_user_sees_global_threads_connected() -> None:
    """Processlist-undercount regression lock: a NON-PROCESS-privileged user
    (`lowpriv`, granted only USAGE) still gets the GLOBAL connection count from
    `SHOW GLOBAL STATUS LIKE 'Threads_connected'`. A processlist-based count
    (`COUNT(*) FROM information_schema.processlist`) would show such a user only
    its OWN thread (1) → silent under-count → false "healthy" while the backend
    is saturated.

    The fixture was recorded with user=lowpriv against the healthy server while
    SEVERAL OTHER connections were held open, so the global Threads_connected is
    clearly > 1 (the lowpriv user's own single thread). That gap is what makes
    this assertion non-vacuous: a processlist-based collector would report 1,
    whereas SHOW GLOBAL STATUS reports the larger global figure.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "lowpriv_global.json")

    result = await _runner().run(manifest, replay, {"user": "lowpriv"})

    assert replay.misses == []
    assert result.status == "ok"
    # The global status var returns the real global connection count, which —
    # because other connections were held during recording — is clearly GREATER
    # than the lowpriv user's own single thread (1). A processlist-based
    # collector restricted to the user's own thread could only ever yield 1, so
    # a value >= 3 proves the GLOBAL path was taken (not a per-user undercount).
    assert result.output["used_connections"] >= 3, result.output
    assert result.output["max_connections"] == 151
