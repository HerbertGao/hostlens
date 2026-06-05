"""Snapshot tests for the ``redis.slowlog`` metrics-only inspector.

Activity example for the Inspector Authoring Contract (version-sensitive CLI
form): the inspector reports slow-query ``count`` + ``max_micros`` ONLY and
never echoes slow-command argument bytes, so ``redis-cli --json`` can never
emit invalid UTF-8 into stdout (the binary-args boundary that motivated the
metrics-only scope — see the manifest header).

The fixtures were recorded by the dev-tool recorder driving the real
``InspectorRunner`` against a ``redis:7-alpine`` container (nonempty / empty
slowlog), plus a fail-loud fixture pointing redis-cli at a closed port, so the
recorded command strings are byte-identical to what the runner sends — replay
hits with zero ``misses``. See ``_record_redis_slowlog.py`` for the recorder.

``test_conn_refused_fails_loud`` is the honesty regression lock (Authoring
Contract rule 8): a conn-refused backend must surface as ``status=exception``,
never as a fabricated healthy ``{"count":0}``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "redis"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "slowlog.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("redis-slowlog-test"),
    )


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares REDIS_PASSWORD as a secret; preflight requires it
    # in the environment. The recorded container had no auth, so an empty
    # value reproduces the recorded (no `-a`) command path exactly.
    monkeypatch.setenv("REDIS_PASSWORD", "")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "redis.slowlog"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["redis-cli"]
    # Version premise declared via tags (no `+`) + free-text description.
    assert "redis6" in manifest.tags
    assert "json-client" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # No hook.py sibling — the metrics-only path is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_nonempty_slowlog_derives_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_nonempty.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # Metrics-only output: scalar count + max_micros, no command text.
    assert result.output == {"count": 8, "max_micros": 19}
    # count=8 >= warn_count(1) but < critical_count(10), max_micros < slow_micros.
    assert [f.severity for f in result.findings] == ["warning"]
    assert "8 slow queries" in result.findings[0].message


async def test_empty_slowlog_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_empty.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # Genuine empty slowlog: redis-cli succeeds and returns count=0 (a real
    # integer), so the collector emits a valid {"count":0,...} → status=ok.
    assert result.output == {"count": 0, "max_micros": 0}
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy {"count":0}. The honesty regression lock: the collector exits
    non-zero with empty stdout, so the runner collapses to status=exception
    instead of blessing a dead backend as "healthy" (Authoring Contract rule 8).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_conn_refused.json")

    result = await _runner().run(manifest, replay, {"port": 6390})

    assert replay.misses == []
    assert result.status != "ok"
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []
