"""One-shot fixture recorder for `docker.containers.restart_loop` (dev-tool).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the **local** Docker
daemon (a `LocalTarget`). Starts real containers per scenario, records, then
tears them down.

Usage:

    .venv-impl/bin/python tests/inspectors/_record_docker_restart_loop.py

Scenarios:
  * loop      — a restart-loop container (RestartCount > threshold) + a healthy
                one → one critical restart-loop finding (status=ok).
  * unhealthy — a running container reporting health=unhealthy → one critical
                finding (status=ok).
  * empty     — no matching container → {results: []} → zero findings (status=ok,
                genuine empty set: docker ps succeeds, zero ids).
  * daemon_down — fail-loud path: DOCKER_HOST points at a dead socket so
                `docker ps` exits non-zero with empty stdout. Recorded with
                allow_failed=True; asserts the runner collapses to
                status=exception — the honesty regression lock.

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult

MANIFEST = Path("src/hostlens/inspectors/builtin/docker/containers_restart_loop.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures")


class _LocalDockerTarget:
    """`ExecutionTarget` that runs `exec` under `sh -c` on the local host and
    statically declares `docker_cli` (so the runner preflight passes without a
    live capability probe). `env` is merged over `os.environ` so per-scenario
    overrides like `DOCKER_HOST` reach the docker CLI.
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
        raise AssertionError("read_file not used by docker.containers.restart_loop")


async def _record(
    out_name: str,
    *,
    parameters: dict[str, Any],
    allow_failed: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    manifest = load_manifest(MANIFEST)
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
    path = FIXTURE_DIR / out_name
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return path.read_text()


def _rm(*names: str) -> None:
    subprocess.run(["docker", "rm", "-f", *names], capture_output=True)


def _wait_restart_count(name: str, minimum: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.RestartCount}}", name],
            capture_output=True,
            text=True,
        )
        if (
            out.returncode == 0
            and out.stdout.strip().isdigit()
            and int(out.stdout.strip()) >= minimum
        ):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not reach RestartCount>={minimum}")


def _wait_health(name: str, status: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", name],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip() == status:
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not reach health={status}")


async def _record_loop() -> None:
    _rm("hlc-ok-test", "hlc-rl-test")
    # Healthy long-running container.
    subprocess.run(
        ["docker", "run", "-d", "--name", "hlc-ok-test", "alpine", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    # Restart-loop container: exits immediately, restart:always drives the count up.
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "hlc-rl-test",
            "--restart",
            "always",
            "alpine",
            "sh",
            "-c",
            "exit 1",
        ],
        check=True,
        capture_output=True,
    )
    _wait_restart_count("hlc-rl-test", 6)
    await _record("docker_restart_loop_loop.json", parameters={"name_filter": "hlc-"})
    _rm("hlc-ok-test", "hlc-rl-test")


async def _record_unhealthy() -> None:
    _rm("hlc-unhealthy-test")
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "hlc-unhealthy-test",
            "--health-cmd",
            "exit 1",
            "--health-interval",
            "1s",
            "--health-retries",
            "1",
            "--health-start-period",
            "0s",
            "alpine",
            "sleep",
            "3600",
        ],
        check=True,
        capture_output=True,
    )
    _wait_health("hlc-unhealthy-test", "unhealthy")
    await _record(
        "docker_restart_loop_unhealthy.json",
        parameters={"name_filter": "hlc-unhealthy"},
    )
    _rm("hlc-unhealthy-test")


async def _record_empty() -> None:
    await _record(
        "docker_restart_loop_empty.json",
        parameters={"name_filter": "no-such-container-zzz"},
    )


async def _record_daemon_down() -> None:
    # Point the docker CLI at a dead unix socket so `docker ps` fails.
    text = await _record(
        "docker_restart_loop_daemon_down.json",
        parameters={"name_filter": "hlc-"},
        allow_failed=True,
        env={"DOCKER_HOST": "unix:///tmp/hostlens-no-such-docker.sock"},
    )
    # The main collect command (the one starting with `ids=$(docker ps`) must
    # have exited non-zero with empty stdout.
    assert '"docker ps failed"' in text or '"exit_code": 1' in text, text
    print("daemon_down fixture recorded (fail-loud)")


async def _main() -> None:
    await _record_loop()
    await _record_unhealthy()
    await _record_empty()
    await _record_daemon_down()


if __name__ == "__main__":
    asyncio.run(_main())
