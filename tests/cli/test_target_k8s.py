"""``hostlens target test`` + ``doctor --json`` coverage for k8s targets.

Covers group D task 6.5 (doctor k8s row form) and the type-agnostic claims
that group D's registry/doctor wiring leans on:

- ``cli/target.py`` carries no ``type == "k8s"`` special-casing: the
  disabled gate (exit 1) fires before ``KubernetesTarget`` would load a
  kubeconfig or dial the API server, and the connectivity probe surfaces
  the k8s-class ``TargetError.kind`` verbatim.
- ``cli/doctor.py`` carries no k8s special-casing (Decision 8): now that
  ``cli/_doctor_schema.TargetHealth.type`` admits ``"k8s"``, a configured
  k8s target no longer crashes doctor with a ``pydantic.ValidationError``.
  A disabled k8s target reports ``connectivity == "skipped"`` (no API
  dial) via the *same* generic ``echo`` probe path docker / ssh / local
  use — there is no per-type doctor branch.

No live cluster is required: the disabled gate short-circuits before any
k8s call, and an enabled target pointed at a non-existent kubeconfig forces
a k8s-class failure regardless of whether the ``[k8s]`` extra is installed
(``k8s_sdk_unavailable`` when absent, ``k8s_unavailable`` when present but
the kubeconfig cannot be loaded). The accepted-kind set below tolerates
both environments so the test is stable on CI and on a developer box.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app

# Every kind ``KubernetesTarget`` may surface from its connectivity path
# when no live cluster is reachable. The CLI / doctor must pass whichever
# one applies through verbatim — they never rewrite or swallow it.
_K8S_PROBE_KINDS = frozenset(
    {
        "k8s_sdk_unavailable",
        "k8s_unavailable",
        "pod_not_found",
        "pod_not_running",
        "container_not_found",
        "container_not_running",
    }
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


# ---------------------------------------------------------------------------
# ``hostlens target test <k8s-target>`` (type-agnostic CLI path)
# ---------------------------------------------------------------------------


def test_target_test_disabled_k8s_exits_1_no_cluster(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """A disabled k8s target exits 1 via the shared disabled gate.

    The ``enabled is False`` check in ``test_cmd`` is type-agnostic and
    fires before ``KubernetesTarget`` would touch kubernetes-asyncio, so
    this passes with or without a cluster / the ``[k8s]`` extra.
    """

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "off-k8s",
                    "type": "k8s",
                    "pod": "some-pod",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "off-k8s"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "is disabled in targets.yaml" in result.stderr


def test_target_test_k8s_passes_through_k8s_kind(
    runner: CliRunner,
    targets_yaml: Path,
    tmp_path: Path,
) -> None:
    """An enabled k8s target with an unreadable kubeconfig exits 1 and
    surfaces a k8s-class ``TargetError.kind`` on stderr.

    Pointing ``kubeconfig`` at a non-existent file forces ``k8s_unavailable``
    when the ``[k8s]`` extra is installed; when it is not, the SDK import
    guard raises ``k8s_sdk_unavailable`` first. Either way the CLI passes
    the kind through unchanged — confirming ``target test`` needs no
    k8s-specific branch.
    """

    missing_kubeconfig = tmp_path / "nonexistent-kubeconfig.yaml"
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "demo-k8s",
                    "type": "k8s",
                    "pod": "no-such-pod",
                    "kubeconfig": str(missing_kubeconfig),
                },
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "demo-k8s"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert any(kind in result.stderr for kind in _K8S_PROBE_KINDS), result.stderr
    assert "target=demo-k8s" in result.stderr


# ---------------------------------------------------------------------------
# ``hostlens doctor --json`` covers k8s targets (type-agnostic, Decision 8)
# ---------------------------------------------------------------------------


def test_doctor_json_disabled_k8s_skipped_no_cluster(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled k8s target → ``connectivity == "skipped"``, exit 0.

    The disabled gate in ``_check_targets`` short-circuits before any probe,
    so this needs no cluster / ``[k8s]`` extra. The row must carry
    ``type == "k8s"`` (regression nail for the ``TargetHealth.type`` Literal
    gap closed by task 6.2) and doctor must emit valid JSON without raising.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "off-k8s",
                    "type": "k8s",
                    "pod": "some-pod",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)  # valid JSON, no ValidationError
    [row] = payload["targets"]
    assert row["type"] == "k8s"
    assert row["connectivity"] == "skipped"
    assert row["enabled"] is False


def test_doctor_json_enabled_k8s_bad_kubeconfig_fails(
    runner: CliRunner,
    targets_yaml: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled k8s target with an unreadable kubeconfig → ``failed``, exit 1.

    The generic ``echo`` probe (no k8s special-casing — Decision 8) drives
    ``KubernetesTarget.exec``, which fails to load the missing kubeconfig and
    surfaces a k8s-class ``error_kind``; doctor flips the overall exit to 1.
    doctor must still produce valid JSON (no crash), confirming the
    ``TargetHealth.type`` Literal now admits ``"k8s"``.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    missing_kubeconfig = tmp_path / "nonexistent-kubeconfig.yaml"
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "demo-k8s",
                    "type": "k8s",
                    "pod": "no-such-pod",
                    "kubeconfig": str(missing_kubeconfig),
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)  # valid JSON, no ValidationError
    [row] = payload["targets"]
    assert row["type"] == "k8s"
    assert row["connectivity"] == "failed"
    assert row["error_kind"] in _K8S_PROBE_KINDS
