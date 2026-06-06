"""Snapshot tests for the ``redis.persistence`` service-inspector-contract probe.

This inspector reports Redis RDB persistence debt as aggregate scalars from
``INFO persistence``. The secret is declared as ``HOSTLENS_REDIS_PASSWORD`` (the
``HOSTLENS_`` prefix per the ssh-execution-target contract) and REMAPPED inside
the collector to redis-cli's native ``REDISCLI_AUTH`` env channel, so the
password never reaches argv.

AOF PREMISE GATE (design D-8): the only finding is gated on ``aof_enabled == 0``.
A high ``rdb_changes_since_last_save`` is a real persistence risk ONLY when AOF
is off — with AOF on, those changes are already fsync-durable, so alerting would
be a false positive. ``test_aof_on_no_finding_despite_high_rdb_changes`` proves
"no finding" is contributed PURELY by the AOF gate (the rdb_changes count is over
threshold there), not by a vacuous under-threshold count.

All fixtures were recorded by the dev-tool recorder (``_record_redis_persistence.py``)
driving the real ``InspectorRunner`` against the pinned compose ``redis`` service
(``--save ""`` → AOF off → aof_enabled=0), so the recorded command strings are
byte-identical to what the runner sends — replay hits with zero ``misses``.

``test_conn_refused_fails_loud`` is the honesty regression lock (Authoring
Contract rule 8 / D-3): a conn-refused backend must surface as ``status=exception``,
never a fabricated healthy object.
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

_FIXTURES = Path(__file__).parent / "fixtures" / "redis"

#: The manifest's frozen ``warn_changes`` default. Asserted against the recorded
#: aof_on output so the "no finding" there is proven non-vacuous (rdb_changes is
#: OVER this threshold; only the AOF gate suppresses the finding).
_WARN_CHANGES_DEFAULT = 100


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "persistence.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("redis-persistence-test"),
    )


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_REDIS_PASSWORD as a secret; preflight
    # requires it in the environment. The recorded instances had no auth, so an
    # empty value reproduces the recorded (no REDISCLI_AUTH) command path.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "redis.persistence"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["redis-cli"]
    assert manifest.secrets == ["HOSTLENS_REDIS_PASSWORD"]
    # Version premise declared via tags (no `+`) + free-text description.
    assert "redis6" in manifest.tags
    assert "service" in manifest.tags
    assert "persistence" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # Secret reaches the client only via the REDISCLI_AUTH env remap — never argv.
    cmd = manifest.collect.command
    assert "REDISCLI_AUTH" in cmd
    assert "-a " not in cmd  # no argv plaintext password
    # No hook.py sibling — the contract probe is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # A handful of un-saved changes, well below the default warn_changes (100) →
    # no finding (genuine healthy → status=ok).
    assert result.output["aof_enabled"] == 0
    assert result.output["rdb_changes_since_last_save"] < _WARN_CHANGES_DEFAULT
    assert result.findings == []


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """semantic-abnormal fixture: an AOF-OFF instance with rdb_changes OVER the
    DEFAULT threshold (no override) fires a warning. This is the contract's
    proof-of-detection track — a genuine RDB snapshot debt at the manifest
    default warn_changes, not a lowered inspector threshold (D-4).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_semantic_abnormal.json")

    # DEFAULT thresholds — no parameter override.
    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["aof_enabled"] == 0
    assert result.output["rdb_changes_since_last_save"] == 500
    # aof_enabled==0 AND rdb_changes(500) >= warn_changes(100) → a single warning.
    assert [f.severity for f in result.findings] == ["warning"]
    msg = result.findings[0].message
    assert "500 changes" in msg
    assert "AOF disabled" in msg


async def test_aof_on_no_finding_despite_high_rdb_changes() -> None:
    """AOF-ON instance with rdb_changes OVER threshold → NO finding. Proves the
    "no finding" is contributed PURELY by the AOF premise gate (design D-8), not
    by a vacuous under-threshold count: rdb_changes here is the SAME量纲 as the
    semantic-abnormal fixture (>= warn_changes default), only aof_enabled differs.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_aof_on.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # The finding is suppressed ONLY by the AOF gate — the count is over threshold.
    assert result.output["aof_enabled"] == 1
    assert result.output["rdb_changes_since_last_save"] >= _WARN_CHANGES_DEFAULT
    assert result.findings == []


async def test_special_char_password_auth_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTH instance whose requirepass is a space + glob metachar password
    (``p w*d``), recorded via the REDISCLI_AUTH env remap → healthy snapshot.
    Replay does not match on env, so the value is irrelevant for the match — it
    only needs to be present so preflight's secret-presence gate passes. This
    fixture is what makes the crosscheck leak scan non-vacuous for
    redis.persistence (task 6.4)."""

    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "p w*d")
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_special_char_pw.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["aof_enabled"] == 0
    assert result.output["rdb_changes_since_last_save"] < _WARN_CHANGES_DEFAULT
    assert "rdb_last_save_time" in result.output
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy object. The honesty regression lock (Authoring Contract rule 8 /
    D-3): the collector exits non-zero with empty stdout, so the runner collapses
    to status=exception instead of blessing a dead backend as "healthy".
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_conn_refused.json")

    result = await _runner().run(manifest, replay, {"port": 6390})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


class _NoBinaryTarget:
    """Stub target where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

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
        raise AssertionError(f"collector must not run when redis-cli is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_redis_cli_binary_requires_unmet() -> None:
    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    # Missing client binary → graceful skip, not a crash and not an exception.
    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declaring ``secrets: [HOSTLENS_REDIS_PASSWORD]`` forces preflight to require
    the env var present (D-3). With it unset entirely (not even an empty string),
    preflight maps to requires_unmet — a no-auth instance must export ``X=`` to
    pass, distinguishing "no env" from "empty value".
    """

    monkeypatch.delenv("HOSTLENS_REDIS_PASSWORD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "persistence_healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert result.missing == ["env:HOSTLENS_REDIS_PASSWORD"]
