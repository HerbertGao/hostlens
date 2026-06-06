"""One-shot fixture recorder for `docker.images.disk_usage` + `docker.networks`
(dev-tool, NOT collected by pytest — no `test_` prefix).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the **local** Docker
daemon (a `_LocalDockerTarget` that runs `exec` under `sh -c` and statically
declares `{shell, file_read, docker_cli}` so preflight passes without a live
probe). Reads GLOBAL daemon state, so this group must be recorded ALONE (not
concurrently with other docker-touching recorders).

DOCKER RECORDING CONVENTION (tasks §1.3): every resource this recorder needs is
**built/created by the recorder itself, recorded, then torn down** in a
`finally`. It NEVER deletes or prunes the operator's pre-existing images /
containers / networks (no `docker system prune`, no blanket `image prune`). All
temporary resources carry the `hostlens-rec-*` prefix.

  * docker.images.disk_usage — the "dominant-image flip" recipe makes
    `reclaimable_pct` cross the default 80 threshold DETERMINISTICALLY,
    independent of the operator's existing image set: build one throwaway image
    far larger than the existing total (a ~12GB `dd`-filled layer tagged
    `hostlens-rec-bigimg`).
      - semantic-abnormal: the big image is UNUSED (no container) → it is
        entirely reclaimable → it dominates `reclaimable_pct` over 80.
      - healthy: a running `hostlens-rec-bigimg-pin` container pins the big
        image → it moves from reclaimable to active → `reclaimable_pct` drops
        well under 80.
    Because the big image dwarfs everything else, its active/inactive flip
    drives the percentage across 80 regardless of the operator's other images.

  * docker.networks — create a fixed number of unattached user-defined networks
    (`hostlens-rec-net-1` / `-2`) to make `dangling_networks >= warn_count`.
      - healthy: recorded BEFORE creating any test network (the operator's
        `infra_*` networks are in-use, so natural dangling count is 0).
      - semantic-abnormal: recorded with the two created networks present.

  * daemon-down (both inspectors): `DOCKER_HOST=tcp://127.0.0.1:1` points at a
    dead endpoint so `docker` exits non-zero with empty stdout → status=exception
    (the fail-loud honesty lock; same class as a conn_refused).

Usage:

    PYTHONPATH=. .venv-impl/bin/python tests/inspectors/_record_docker_images_networks.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult

_BUILTIN = Path("src/hostlens/inspectors/builtin/docker")
IMAGES_MANIFEST = _BUILTIN / "images_disk_usage.yaml"
NETWORKS_MANIFEST = _BUILTIN / "networks.yaml"
FIXTURE_DIR = Path("tests/inspectors/fixtures/docker")

BIG_IMAGE = "hostlens-rec-bigimg:latest"
PIN_CONTAINER = "hostlens-rec-bigimg-pin"
REC_NETWORKS = ("hostlens-rec-net-1", "hostlens-rec-net-2")
DEAD_DOCKER_HOST = "tcp://127.0.0.1:1"
# A ~12GB layer dwarfs typical local image totals so the active/inactive flip
# of this one image drives reclaimable_pct across the default 80 threshold
# regardless of the operator's other images.
BIG_IMAGE_MB = 12288


class _LocalDockerTarget:
    """`ExecutionTarget` that runs `exec` under `sh -c` on the local host and
    statically declares `docker_cli` (so preflight passes without a live probe).
    `env` is merged over `os.environ` so per-scenario `DOCKER_HOST` overrides
    reach the docker CLI.
    """

    type = "local"

    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities: set[Capability] = {
            Capability.SHELL,
            Capability.FILE_READ,
            Capability.DOCKER_CLI,
        }

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
        out, err = await proc.communicate()
        return ExecResult(
            exit_code=proc.returncode,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError("read_file not used by docker images/networks inspectors")


async def _record(
    manifest_path: Path,
    out_name: str,
    *,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    target = _LocalDockerTarget("recorder")
    old_env = dict(os.environ)
    if env:
        os.environ.update(env)
    try:
        fixture = await record_fixture(
            manifest,
            target,  # type: ignore[arg-type]
            settings=Settings(),
            parameters=parameters,
            allow_failed=allow_failed,
        )
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / out_name
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return fixture.to_dict()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _main_collect_stdout(fixture: dict[str, Any]) -> str:
    """Return the stdout of the LAST recorded command (the main collector — the
    preflight probes come first, the collector is last)."""

    return fixture["commands"][-1]["stdout"]


def _build_big_image() -> None:
    # Base pinned by @sha256 digest (not a floating `alpine:3.19` tag) so a
    # re-record builds a byte-stable base layer. The recorded reclaimable_pct is
    # robust to the base anyway — the BIG_IMAGE_MB layer dwarfs it — but pinning
    # keeps the recording lane reproducible end-to-end. NOTE: docker.images /
    # docker.networks recording inherently reads WHOLE-DAEMON state (system df /
    # network ls are global by nature), so re-record reproducibility also depends
    # on the daemon being quiescent at record time; this is guarded fail-loud by
    # the post-record reclaimable_pct (>=80 / <80) and dangling_networks (==0)
    # assertions — a non-quiescent daemon aborts the record rather than committing
    # a polluted fixture. The binding contract (spec「离线回放确定性出结果」) is
    # REPLAY determinism of the committed fixture, which holds regardless.
    with tempfile.TemporaryDirectory(prefix="hostlens-rec-build-") as ctx:
        dockerfile = Path(ctx) / "Dockerfile"
        dockerfile.write_text(
            "FROM alpine@sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1\n"
            f"RUN dd if=/dev/zero of=/big.bin bs=1M count={BIG_IMAGE_MB} 2>/dev/null\n"
        )
        print(f"building {BIG_IMAGE} (~{BIG_IMAGE_MB}MB layer) ...")
        _docker("build", "-t", BIG_IMAGE, ctx)


def _cleanup_all() -> None:
    _docker("rm", "-f", PIN_CONTAINER, check=False)
    _docker("rmi", "-f", BIG_IMAGE, check=False)
    _docker("network", "rm", *REC_NETWORKS, check=False)


async def _record_images() -> None:
    import json

    _cleanup_all()
    try:
        _build_big_image()

        # semantic-abnormal: big image UNUSED → reclaimable_pct over 80.
        fx = await _record(IMAGES_MANIFEST, "images_semantic_abnormal.json")
        out = json.loads(_main_collect_stdout(fx))
        assert out["reclaimable_pct"] >= 80, out
        print(f"  abnormal reclaimable_pct={out['reclaimable_pct']} size={out['size']}")

        # healthy: pin the big image with a running container → reclaimable_pct
        # drops well under 80.
        _docker("run", "-d", "--name", PIN_CONTAINER, BIG_IMAGE, "sleep", "3600")
        fx = await _record(IMAGES_MANIFEST, "images_healthy.json")
        out = json.loads(_main_collect_stdout(fx))
        assert out["reclaimable_pct"] < 80, out
        print(f"  healthy reclaimable_pct={out['reclaimable_pct']} size={out['size']}")

        # daemon-down: dead endpoint → docker exits non-zero, empty stdout.
        fx = await _record(
            IMAGES_MANIFEST,
            "images_daemon_down.json",
            allow_failed=True,
            env={"DOCKER_HOST": DEAD_DOCKER_HOST},
        )
        assert fx["commands"][-1]["exit_code"] != 0, fx["commands"][-1]
        assert fx["commands"][-1]["stdout"] == "", fx["commands"][-1]
        print("  daemon_down recorded (fail-loud)")
    finally:
        _cleanup_all()


async def _record_networks() -> None:
    import json

    _docker("network", "rm", *REC_NETWORKS, check=False)
    try:
        # healthy: BEFORE creating any test network. The operator's infra_*
        # networks are in-use (non-empty Containers) so the natural dangling
        # count is 0.
        fx = await _record(NETWORKS_MANIFEST, "networks_healthy.json")
        out = json.loads(_main_collect_stdout(fx))
        assert out["dangling_networks"] == 0, out
        print(f"  healthy dangling_networks={out['dangling_networks']}")

        # semantic-abnormal: create fixed unattached user-defined networks.
        for net in REC_NETWORKS:
            _docker("network", "create", net)
        fx = await _record(NETWORKS_MANIFEST, "networks_semantic_abnormal.json")
        out = json.loads(_main_collect_stdout(fx))
        assert out["dangling_networks"] >= 1, out
        names = {r["name"] for r in out["results"]}
        assert set(REC_NETWORKS) <= names, out
        print(
            f"  abnormal dangling_networks={out['dangling_networks']} results_names={sorted(names)}"
        )

        # daemon-down: dead endpoint → docker exits non-zero, empty stdout.
        fx = await _record(
            NETWORKS_MANIFEST,
            "networks_daemon_down.json",
            allow_failed=True,
            env={"DOCKER_HOST": DEAD_DOCKER_HOST},
        )
        assert fx["commands"][-1]["exit_code"] != 0, fx["commands"][-1]
        assert fx["commands"][-1]["stdout"] == "", fx["commands"][-1]
        print("  daemon_down recorded (fail-loud)")
    finally:
        _docker("network", "rm", *REC_NETWORKS, check=False)


async def _main() -> None:
    await _record_networks()
    await _record_images()
    # Final safety sweep: nothing hostlens-rec-* may survive.
    _cleanup_all()


if __name__ == "__main__":
    asyncio.run(_main())
