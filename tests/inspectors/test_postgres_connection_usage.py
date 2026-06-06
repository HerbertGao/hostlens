"""Snapshot tests for the ``postgres.connection_usage`` service inspector.

Service-inspector-contract probe for the NEW client (``psql``) + a secret
declared as ``HOSTLENS_POSTGRES_PASSWORD`` (the ``HOSTLENS_`` prefix per the
``ssh-execution-target`` contract) remapped to the client-native ``PGPASSWORD``
env, with a one-round-trip ``pg_stat_activity`` count + ``max_connections`` query
whose ``used|max`` output is awk'd on a SEPARATE line (command-sub, never piped)
and emitted as a top-level JSON object.

The fixtures were recorded by the dev-tool recorder driving the real
``InspectorRunner`` against the pinned compose ``postgres`` / ``postgres-lowconn``
services, so the recorded command strings are byte-identical to what the runner
sends — replay hits with zero ``misses``. See
``_record_postgres_connection_usage.py`` for the recorder.

Failure-classification locks:
  * ``test_access_denied_fails_loud`` — an auth-failed backend surfaces as
    ``status=exception``, NOT a fabricated healthy ``used_pct=0``. (The compose
    image ``trust``es loopback, so the access_denied fixture connects via the
    container's non-loopback hostname to hit the ``scram-sha-256`` pg_hba rule;
    the recorded host is read back from the fixture so replay matches.)
  * ``test_conn_refused_fails_loud`` — an unreachable backend (port closed)
    surfaces as ``status=exception``, not a silent zero.
  * ``test_missing_psql_binary_requires_unmet`` /
    ``test_missing_secret_env_requires_unmet`` — a missing client binary / a
    missing declared secret skip the run as ``requires_unmet`` (a premise gap),
    distinct from the ``exception`` reachability failures.

Acceptance sufficiency rests on
``test_semantic_abnormal_critical_at_default_thresholds`` (a real high-connection
state crossing the DEFAULT critical threshold); ``test_finding_trigger_warning``
(a lowered warn) only exercises the finding-wiring, not acceptance.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import ClassVar

import jinja2
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

_FIXTURES = Path(__file__).parent / "fixtures" / "postgres"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "postgres" / "connection_usage.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("postgres-connection-usage-test"),
    )


def _recorded_host(fixture: Path) -> str:
    """The ``-h <host>`` value baked into the fixture's main collect command.

    The access_denied fixture was recorded against the container's non-loopback
    hostname (a per-record container id) to defeat the loopback ``trust`` rule;
    re-deriving it from the fixture keeps the replay params matching the recorded
    command without hardcoding a volatile container id.
    """

    main = json.loads(fixture.read_text())["commands"][-1]
    match = re.search(r"-h (\S+)", main["cmd"])
    assert match is not None, main["cmd"]
    return match.group(1)


@pytest.fixture(autouse=True)
def _postgres_pwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_POSTGRES_PASSWORD as a secret; preflight
    # requires it in the environment. ReplayTarget neither matches on nor stores
    # env, so the value here is irrelevant to command matching — it only has to be
    # PRESENT so preflight's secret-presence gate passes. Tests asserting the
    # missing-secret path delenv it explicitly.
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", "test-" + "pw")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "postgres.connection_usage"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["psql"]
    assert manifest.secrets == ["HOSTLENS_POSTGRES_PASSWORD"]
    # Tags carry no `+` version pin sigil.
    assert all("+" not in tag for tag in manifest.tags)
    # No hook.py sibling — the metrics-only path is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "used_connections": 6,
        "max_connections": 100,
        "used_pct": 6.0,
    }
    # 6.0% is far below the default warn (80) / critical (95) thresholds.
    assert result.findings == []


async def test_finding_trigger_warning() -> None:
    """Healthy server + a LOW warn_used_pct param → the real (low) used_pct
    crosses warn → a single warning. Verifies the finding-wiring track only —
    acceptance sufficiency rests on the semantic-abnormal default-threshold test.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "finding_trigger.json")

    result = await _runner().run(manifest, replay, {"user": "postgres", "warn_used_pct": 0.5})

    assert replay.misses == []
    assert result.status == "ok"
    assert [f.severity for f in result.findings] == ["warning"]
    assert "6.0%" in result.findings[0].message


async def test_semantic_abnormal_critical_at_default_thresholds() -> None:
    """The postgres-lowconn instance (max_connections=10) saturated with held
    pg_sleep backends → used_pct=120 → critical AT THE DEFAULT thresholds (95), a
    genuine high-connection state, not a lowered inspector threshold.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "semantic_abnormal.json")

    # No threshold override — exercises the DEFAULT warn 80 / critical 95.
    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "used_connections": 12,
        "max_connections": 10,
        "used_pct": 120.0,
    }
    assert [f.severity for f in result.findings] == ["critical"]
    assert "120.0%" in result.findings[0].message
    assert "(12/10)" in result.findings[0].message


def _render_collect_command(**params: object) -> str:
    """Render the manifest's REAL ``collect.command`` exactly as
    ``InspectorRunner._render_command`` does — a fresh Jinja2 env with the ``sh``
    filter mapped to ``shlex.quote`` — so this drives the actual collector shell,
    not a copy (drift-proof: a manifest change re-renders here)."""

    command = load_manifest(_manifest_path()).collect.command
    env = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)
    env.filters["sh"] = lambda value: shlex.quote(str(value))
    return env.from_string(command).render(**params)


def test_collector_maxc_zero_yields_null_used_pct(tmp_path: Path) -> None:
    """``max_connections`` can never be 0 on a reachable postgres (>= 1), so the
    ReplayTarget fixtures cannot record the ``maxc <= 0`` else-branch. Drive the
    real rendered collector shell against a fake ``psql`` returning ``5|0`` and
    assert the branch emits ``used_pct: null`` — NOT a division-by-zero crash nor
    a fabricated 0% (the symmetric defensive twin of redis maxmemory=0)."""

    rendered = _render_collect_command(
        host="127.0.0.1", port=5432, user="postgres", dbname="postgres"
    )

    fake_psql = tmp_path / "psql"
    fake_psql.write_text("#!/bin/sh\nprintf '5|0\\n'\n")
    fake_psql.chmod(0o755)

    proc = subprocess.run(
        ["sh", "-c", rendered],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "HOSTLENS_POSTGRES_PASSWORD": "x",
        },
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == '{"used_connections":5,"max_connections":0,"used_pct":null}'


async def test_access_denied_fails_loud() -> None:
    """Auth failure (wrong password) → status=exception, NOT a fabricated
    healthy used_pct=0. The honesty regression lock: psql exits non-zero with
    empty stdout, so the runner collapses to status=exception instead of blessing
    an auth-failed backend as "healthy".
    """

    manifest = load_manifest(_manifest_path())
    fixture = _FIXTURES / "access_denied.json"
    replay = ReplayTarget("pgrec", fixture=fixture)

    result = await _runner().run(
        manifest, replay, {"user": "postgres", "host": _recorded_host(fixture)}
    )

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (port closed) → status=exception. The server-side port
    being closed leaves the host-level psql client exiting non-zero → exception,
    distinct from a target_unreachable (the HOST being unreachable), an orthogonal
    transport-layer state.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, {"user": "postgres", "port": 15999})

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
        raise AssertionError(f"collector must not run when psql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_psql_binary_requires_unmet() -> None:
    """A target without the psql client → preflight requires_unmet skip (a
    premise gap), NOT an error that aborts the run."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, {"user": "postgres"})  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared secret absent from the environment → preflight requires_unmet
    skip (a premise gap), NOT an exception. The collector is never reached, so the
    fixture's commands do not matter — only the preflight gate.
    """

    monkeypatch.delenv("HOSTLENS_POSTGRES_PASSWORD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert result.status == "requires_unmet"
    assert result.missing == ["env:HOSTLENS_POSTGRES_PASSWORD"]
    assert result.output == {}
