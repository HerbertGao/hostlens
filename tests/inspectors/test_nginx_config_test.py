"""Snapshot tests for the ``nginx.config_test`` service inspector.

``nginx.config_test`` is a FINDING-ROUTE probe: ``nginx -t`` reporting an invalid
config is a SUCCESSFUL collection of an abnormal result (a finding), NOT a
collection failure (an exception). Its collector's fail-loud direction is the
OPPOSITE of the rest of this batch — rc=1 (bad config) routes to a finding with
the collector itself exiting 0; only a non-{0,1} rc collapses to exception.

Fixtures were recorded by ``_record_nginx.py`` driving the real
``InspectorRunner`` against throwaway nginx containers:
  * ``config_test_valid`` — default config → rc=0 → config_valid:true → ok, no
    finding.
  * ``config_test_invalid`` (= semantic-abnormal) — ``bad.conf`` mounted as the
    effective ``/etc/nginx/nginx.conf`` → rc=1 → config_valid:false + a finding
    at the DEFAULT severity. Its ``detail`` carries the ``nginx -t`` stderr
    verdict, which naturally contains QUOTES, a BACKSLASH and NEWLINES — proving
    the collector's ``jq -n --arg`` escaping produced valid JSON instead of
    degrading to an exception.
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
    return _builtin_root() / "nginx" / "config_test.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("nginx-config-test-test"),
    )


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "nginx.config_test"
    assert manifest.parse.format == "json"
    assert "nginx" in manifest.requires_binaries
    assert "jq" in manifest.requires_binaries
    assert manifest.secrets == []
    # Finding-route: exactly one rule (config_valid == false).
    assert len(manifest.findings) == 1
    # No hook.py sibling — pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_valid_config_no_finding() -> None:
    """Default config validates (rc=0) → config_valid:true → ok, no finding."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "config_test_valid.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []
    assert result.output["config_valid"] is True


async def test_invalid_config_finding_at_default_thresholds() -> None:
    """semantic-abnormal: a real invalid config (rc=1) → finding-route. The
    collection SUCCEEDS (status=ok), the result is a bad config, and the finding
    fires at the manifest DEFAULT thresholds with its declared severity +
    message semantics. ``detail`` carries the escaped ``nginx -t`` stderr,
    proving the ``jq -n --arg`` escaping produced valid JSON (quotes/backslash/
    newline did NOT degrade the run to an exception).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "config_test_invalid.json")

    # No threshold override — exercises the manifest DEFAULTS (default_thresholds).
    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    # Finding-route: collection succeeded, the result is just a bad config.
    assert result.status == "ok"
    assert result.output["config_valid"] is False
    detail = result.output["detail"]
    assert detail  # non-empty
    # The jq-escaping evidence: the raw stderr verdict carried a quote and a
    # backslash and a newline; the round-tripped detail must preserve them
    # (proves valid JSON was produced, not an exception).
    assert '"' in detail, detail
    assert "\\" in detail, detail
    assert "\n" in detail, detail
    assert [f.severity for f in result.findings] == ["critical"]
    msg = result.findings[0].message
    assert "nginx config test failed" in msg


async def test_unexpected_rc_fails_loud() -> None:
    """A non-{0,1} ``nginx -t`` rc (here rc=2 from a shimmed nginx) is NOT
    swallowed into config_valid:false — the collector exits 1 with empty stdout,
    so the run collapses to status=exception (design D-5 safety boundary: an
    uninvokable / abnormally-exiting nginx is not "a bad config")."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "config_test_unexpected_rc.json")

    result = await _runner().run(manifest, replay, None)

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
        raise AssertionError(f"collector must not run when nginx is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_nginx_binary_requires_unmet() -> None:
    """A target without the nginx binary → preflight requires_unmet skip (a
    premise gap), NOT an exception."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing
