"""Snapshot tests for the ``redis.memory_usage`` service-inspector-contract probe.

This inspector is contract probe 1 of ``add-service-inspector-contract-spike``:
it proves the minimal common service contract for a PROVEN client (redis-cli) —
secret declared as ``HOSTLENS_REDIS_PASSWORD`` (the ``HOSTLENS_`` prefix per the
ssh-execution-target contract) and REMAPPED inside the collector to redis-cli's
native ``REDISCLI_AUTH`` env channel, so the password never reaches argv.

All fixtures were recorded by the dev-tool recorder (``_record_redis_memory_usage.py``)
driving the real ``InspectorRunner`` against the pinned compose redis services, so
the recorded command strings are byte-identical to what the runner sends — replay
hits with zero ``misses``.

Dual-track fixtures (contract requirement, D-4):
  * ``finding_trigger`` — a healthy ``redis-abnormal`` instance recorded with a
    LOWERED warn threshold, validating finding wiring ONLY.
  * ``semantic_abnormal`` — the SAME instance filled to a REAL >=95% usage,
    asserting a critical at the manifest DEFAULT thresholds.

``test_conn_refused_fails_loud`` is the honesty regression lock (Authoring
Contract rule 8 / D-3): a conn-refused backend must surface as
``status=exception``, never a fabricated healthy ``{"used_pct":0}``.

``test_special_char_password_authenticates`` is the word-split regression lock:
a password with a space + glob metachar (``p w*d``) authenticates successfully
through the ``REDISCLI_AUTH`` env channel, proving the env remap does not
word-split the secret the way an unquoted ``-a $pwd`` would.
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

#: The special-char password the special-char fixture was recorded with. Set as
#: the secret env value when replaying that fixture so the rendered command
#: (which references the secret only via env, never argv) matches byte-for-byte.
_SPECIAL_PW = "p w*d"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "memory_usage.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("redis-memory-usage-test"),
    )


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_REDIS_PASSWORD as a secret; preflight
    # requires it in the environment. The recorded instances had no auth, so an
    # empty value reproduces the recorded (no REDISCLI_AUTH) command path. Tests
    # that need the auth path (special-char password) override this per-test.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "redis.memory_usage"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["redis-cli"]
    assert manifest.secrets == ["HOSTLENS_REDIS_PASSWORD"]
    # Version premise declared via tags (no `+`) + free-text description.
    assert "redis6" in manifest.tags
    assert "service" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # Secret reaches the client only via the REDISCLI_AUTH env remap — never argv.
    cmd = manifest.collect.command
    assert "REDISCLI_AUTH" in cmd
    assert "-a " not in cmd  # no argv plaintext password
    # No hook.py sibling — the contract probe is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # No maxmemory limit → used_pct is null; the pct findings are guarded against
    # null, so a healthy unbounded instance reports raw bytes with no finding.
    assert result.output == {"used_memory": 1042080, "maxmemory": 0, "used_pct": None}
    assert result.findings == []


async def test_finding_trigger_emits_warning() -> None:
    """finding-trigger fixture: healthy instance + LOWERED warn threshold fires a
    warning. Validates finding wiring ONLY (the recorded params are passed so the
    replayed command matches; at the manifest defaults this usage is healthy).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_finding_trigger.json")

    # Recorded with these lowered thresholds — pass them so the rendered command
    # matches and the DSL evaluates against the same values.
    result = await _runner().run(
        manifest, replay, {"warn_used_pct": 0.0, "critical_used_pct": 99.0}
    )

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"used_memory": 1042240, "maxmemory": 2097152, "used_pct": 49.7}
    # 49.7 in [warn=0.0, critical=99.0) → a single warning.
    assert [f.severity for f in result.findings] == ["warning"]
    assert "49.7%" in result.findings[0].message


async def test_semantic_abnormal_critical_at_default_thresholds() -> None:
    """semantic-abnormal fixture: a REAL high-memory instance (filled to ~99.86%)
    fires a critical at the manifest DEFAULT thresholds (no override). This is the
    contract's proof-of-detection track — distinct from finding-trigger, which
    only fires under lowered thresholds.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_semantic_abnormal.json")

    # DEFAULT thresholds — no parameter override.
    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"used_memory": 2094272, "maxmemory": 2097152, "used_pct": 99.86}
    # 99.86 >= critical_used_pct(95.0) → a single critical with semantic message.
    assert [f.severity for f in result.findings] == ["critical"]
    msg = result.findings[0].message
    assert "99.86%" in msg
    assert "2094272/2097152 bytes" in msg


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy object. The honesty regression lock (Authoring Contract rule 8 /
    D-3): the collector exits non-zero with empty stdout, so the runner collapses
    to status=exception instead of blessing a dead backend as "healthy".
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_conn_refused.json")

    result = await _runner().run(manifest, replay, {"port": 6390})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_special_char_password_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password with a space + glob metachar (``p w*d``) authenticates through
    the REDISCLI_AUTH env channel → status=ok. Proves the env remap does NOT
    word-split the secret into bogus args (the failure mode of an unquoted
    ``-a $pwd``), so a legitimate special-char password is never misclassified as
    an auth failure.
    """

    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", _SPECIAL_PW)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_special_char_pw.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["used_pct"] is None  # no maxmemory limit on this instance
    assert result.findings == []
    # The secret never leaks into the recorded command or its output.
    recorded = (_FIXTURES / "memory_usage_special_char_pw.json").read_text()
    assert _SPECIAL_PW not in recorded


class _EnvCapturingTarget:
    """Target that captures the ``env`` passed to the collector ``exec`` call.

    Unlike ``ReplayTarget`` (which IGNORES ``env``, so a runner that forgot to
    deliver the password via ``env`` would still replay green), this records the
    ``env`` of the MAIN collector call so the test can assert the secret was
    delivered through the ``env=`` channel — and is ABSENT from the command
    string. Preflight probes get canned success; the collector gets a valid
    no-maxmemory JSON object so the run reaches ``ok``.
    """

    type = "local"
    name = "env-capture-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    def __init__(self) -> None:
        self.collector_cmd: str | None = None
        self.collector_env: dict[str, str] | None = None

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout
        if cmd.startswith("command -v ") or cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        self.collector_cmd = cmd
        self.collector_env = dict(env or {})
        return ExecResult(
            exit_code=0,
            stdout='{"used_memory":1,"maxmemory":0,"used_pct":null}',
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_special_char_password_delivered_via_env_not_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner must deliver a special-char password to the collector through
    the ``env=`` channel, NEVER spliced into the command string. ReplayTarget
    ignores ``env`` and so cannot prove this; this fake target captures the env
    of the real collector call and asserts (a) the secret reaches ``env=`` and
    (b) its plaintext is absent from the rendered command string."""

    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", _SPECIAL_PW)
    manifest = load_manifest(_manifest_path())
    target = _EnvCapturingTarget()

    result = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "ok", result.error
    # (a) The secret was delivered to the collector via env=, under its declared name.
    assert target.collector_env is not None
    assert target.collector_env.get("HOSTLENS_REDIS_PASSWORD") == _SPECIAL_PW
    # (b) The plaintext password never appears in the rendered command string —
    # it is referenced only via the ${...} env expansion / REDISCLI_AUTH remap.
    assert target.collector_cmd is not None
    assert _SPECIAL_PW not in target.collector_cmd


# --------------------------------------------------------------------------- #
# Failure classification (D-3): missing client binary / missing declared secret
# both map to requires_unmet (a graceful skip, NOT an exception). These exercise
# the preflight gates directly without a recorded fixture.
# --------------------------------------------------------------------------- #


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


async def test_missing_redis_cli_requires_unmet() -> None:
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
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "memory_usage_healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any("HOSTLENS_REDIS_PASSWORD" in m for m in result.missing), result.missing
