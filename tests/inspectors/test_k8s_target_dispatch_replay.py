"""End-to-end k8s-dispatch replay tests (enable-k8s-inspector-targets §4).

Proves the k8s dispatch path runs end-to-end for one representative inspector
per cohort class (service / runtime / process / network), per authoring-contract
§场景:容器类派发路径必须有代表性回放验证.

Strategy (design Decision 5, mirroring the docker version): the collector
command string is **orthogonal** to the target type — the same recorded command
replays identically whether dispatched through a KubernetesTarget or an
SSHTarget. So instead of recording real pod fixtures, each test reuses the
inspector's existing local/ssh fixture and flips its top-level ``impersonate``
to ``k8s``. This drives ``ReplayTarget.type == "k8s"`` so the runner
preflight's ``target.type in manifest.targets`` gate passes for the
k8s-declaring manifest, and the full ``preflight → render → collect → parse →
findings`` path runs offline, ending in ``InspectorResult.status == "ok"``.

``replay.misses == []`` on every run asserts the rendered command matches the
recorded fixture byte-for-byte — i.e. flipping ``impersonate`` does NOT perturb
the command, confirming target-type / collector orthogonality.

The capability-gate cases at the bottom back design Decision 5 point (c):
KubernetesTarget advertises the same restricted capability set as DockerTarget
(``{SHELL, FILE_READ}`` baseline, no ``ssh``, ``systemd`` only when probed), so
an inspector requiring ``ssh`` / ``systemd`` must be stopped by preflight with
``requires_unmet`` even if it mis-declares a ``k8s`` target.

Per memory ``project_test_sibling_helper_import_ci`` this module imports only
from ``hostlens.*`` (no ``tests.inspectors.*`` sibling import) so console
``pytest`` (pythonpath=src, no ``tests/__init__.py``) does not crash. Snapshot
string assertions use ``.rstrip("\\n")`` to tolerate trailing-newline drift.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import CollectSpec, InspectorManifest, ParseSpec
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

# Frozen clock matching the recording clock of the reused fixtures; none of
# the four representative fixtures has a sampling_window, so this only pins
# determinism and is harmless either way.
_FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("k8s-dispatch-test"),
        clock=lambda: _FROZEN_DT,
    )


def _k8s_fixture(src: Path, tmp_path: Path) -> Path:
    """Copy an existing local/ssh fixture, flip ``impersonate`` to ``k8s``,
    and return the path to the rewritten temp fixture. Everything else (recorded
    commands, capabilities, files) is preserved byte-for-byte."""

    data = json.loads(src.read_text(encoding="utf-8"))
    data["impersonate"] = "k8s"
    dst = tmp_path / src.name
    dst.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dst


async def _run_k8s(
    manifest_rel: str,
    fixture_rel: str,
    tmp_path: Path,
    parameters: dict[str, Any] | None = None,
) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_builtin_root() / manifest_rel)
    k8s_fx = _k8s_fixture(_FIXTURE_ROOT / fixture_rel, tmp_path)
    replay = ReplayTarget("k8s-pod", fixture=k8s_fx)
    assert replay.type == "k8s"
    result = await _runner().run(manifest, replay, parameters)
    return replay, result


# --------------------------------------------------------------------------- #
# 4.1 — service class representative: redis.memory_usage
# --------------------------------------------------------------------------- #


async def test_service_redis_memory_usage_k8s_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # redis.memory_usage declares secrets:[HOSTLENS_REDIS_PASSWORD]; the healthy
    # fixture was recorded against a no-auth instance, so an empty value
    # reproduces the recorded (no REDISCLI_AUTH) command path.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")

    replay, result = await _run_k8s(
        "redis/memory_usage.yaml",
        "redis/memory_usage_healthy.json",
        tmp_path,
    )

    assert replay.type == "k8s"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"used_memory": 1042080, "maxmemory": 0, "used_pct": None}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.1 — runtime class representative: go.heap
# --------------------------------------------------------------------------- #


async def test_runtime_go_heap_k8s_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_k8s(
        "go/heap.yaml",
        "go/heap_ok.json",
        tmp_path,
    )

    assert replay.type == "k8s"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"heap_inuse_bytes": 100000000, "heap_alloc_bytes": 80000000}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.1 — process class representative: linux.process.zombies (ps axo → PID ns)
# --------------------------------------------------------------------------- #


async def test_process_zombies_k8s_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_k8s(
        "linux/process_zombies.yaml",
        "os_process/process_zombies_ok.json",
        tmp_path,
    )

    assert replay.type == "k8s"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"zombie_count": 0, "results": []}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.1 — network class representative: net.listening_ports (pod netns view)
# --------------------------------------------------------------------------- #


async def test_net_listening_ports_k8s_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_k8s(
        "net/listening_ports.yaml",
        "os_net/listening_ports_ok.json",
        tmp_path,
        parameters={"allowed_ports": [22, 443]},
    )

    assert replay.type == "k8s"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Snapshot of the k8s dispatch cohort (.rstrip("\n") tolerance). The serialized
# {name: status} map across all four classes is the cohort snapshot — a single
# regression lock that every representative dispatched green on k8s.
# --------------------------------------------------------------------------- #

_EXPECTED_SNAPSHOT = """\
go.heap=ok
linux.process.zombies=ok
net.listening_ports=ok
redis.memory_usage=ok
"""


async def test_k8s_dispatch_cohort_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")

    cases: list[tuple[str, str, dict[str, Any] | None]] = [
        ("redis/memory_usage.yaml", "redis/memory_usage_healthy.json", None),
        ("go/heap.yaml", "go/heap_ok.json", None),
        ("linux/process_zombies.yaml", "os_process/process_zombies_ok.json", None),
        (
            "net/listening_ports.yaml",
            "os_net/listening_ports_ok.json",
            {"allowed_ports": [22, 443]},
        ),
    ]

    rows: list[str] = []
    for i, (manifest_rel, fixture_rel, params) in enumerate(cases):
        manifest = load_manifest(_builtin_root() / manifest_rel)
        sub = tmp_path / str(i)
        sub.mkdir()
        replay, result = await _run_k8s(manifest_rel, fixture_rel, sub, params)
        assert replay.type == "k8s"
        assert replay.misses == []
        rows.append(f"{manifest.name}={result.status}")

    snapshot = "\n".join(sorted(rows))
    assert snapshot.rstrip("\n") == _EXPECTED_SNAPSHOT.rstrip("\n")


# --------------------------------------------------------------------------- #
# 4.2 — capability gate backstop: a k8s-typed target with the container-class
# restricted capability set (no ssh / no systemd, identical to DockerTarget's
# baseline) must stop an inspector requiring those capabilities at preflight.
# --------------------------------------------------------------------------- #


def _restricted_k8s_target(tmp_path: Path) -> ReplayTarget:
    fixture = tmp_path / "restricted_k8s.json"
    fixture.write_text(
        json.dumps(
            {
                "impersonate": "k8s",
                "capabilities": ["shell", "file_read"],
                "commands": [],
                "files": {},
            }
        ),
        encoding="utf-8",
    )
    return ReplayTarget("k8s-pod", fixture=fixture)


def _capability_manifest(requires: list[str]) -> InspectorManifest:
    return InspectorManifest(
        name="test.k8s_capability_gate",
        version="1.0.0",
        description="capability gate backstop probe",
        targets=["k8s"],
        requires_capabilities=requires,
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )


@pytest.mark.parametrize("capability", ["ssh", "systemd"])
async def test_k8s_capability_gate_rejects_unmet_capability(
    tmp_path: Path, capability: str
) -> None:
    target = _restricted_k8s_target(tmp_path)
    result = await _runner().run(_capability_manifest([capability]), target, None)

    assert result.status == "requires_unmet"
    assert result.missing == [capability]


async def test_k8s_capability_gate_passes_with_shell_only(tmp_path: Path) -> None:
    # Sanity counterpart: the same restricted capability set satisfies a
    # shell-only inspector — proving the rejection above is the capability
    # check, not the target type. Calls _preflight directly because the
    # restricted fixture records no commands: a full run() would stop at the
    # collect stage with a ReplayMiss and obscure the preflight-pass result.
    target = _restricted_k8s_target(tmp_path)
    status, missing, _err = await _runner()._preflight(
        _capability_manifest(["shell"]), target, allow_privileged=False
    )

    assert status == "ok"
    assert missing == []
