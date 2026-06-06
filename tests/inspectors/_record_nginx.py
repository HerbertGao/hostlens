"""One-shot fixture recorder for the wave-2a nginx inspectors (dev-tool).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against TWO different recording
targets — one per inspector, because the two probes have opposite locality:

  * nginx.health — probes the stub_status endpoint with HOST curl (the alpine
    nginx container has no curl; the host has /usr/bin/curl). So health runs on
    a `_HostShellTarget` that execs `sh -c` on the host, declaring only SHELL
    capability. The compose `nginx` service publishes container :80 → host :18080
    with a stub_status location; `wait_healthy` polls its busybox-wget
    healthcheck. The down fixture targets host :18099 (nothing listening) →
    curl non-zero → exception (recorded with allow_failed=True).

  * nginx.config_test — runs `nginx -t` which needs the nginx binary, only
    present INSIDE the container. So config_test runs on a `DockerExecTarget`
    against a throwaway nginx container. The collector also needs `jq`, which the
    1.27.3-alpine image lacks — so each throwaway container gets `apk add
    --no-cache jq` before recording (network-dependent; fail-loud if it can't).
    valid: default config (rc=0 → config_valid:true → ok, no finding).
    invalid (= semantic-abnormal): bad.conf mounted as /etc/nginx/nginx.conf,
    container kept alive with `sleep` (so a bad config doesn't crash it), rc=1 →
    config_valid:false + detail (stderr with quotes/backslash/newline) → finding.
    unexpected-rc: a fake `nginx` shimmed onto PATH ahead of the real binary so
    `nginx -t` returns rc=2 (∉ {0,1}) → collector exits 1 with empty stdout →
    status=exception (design D-5 safety boundary: a non-{0,1} rc must NOT be
    swallowed into config_valid:false).

Recording NEVER runs in day-to-day CI (no `test_` prefix). Tears down every
container it starts.

Usage:

    PYTHONPATH=. .venv-impl/bin/python tests/inspectors/_record_nginx.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult
from tests.inspectors._compose_record import compose_down, compose_up, wait_healthy

HEALTH_MANIFEST = Path("src/hostlens/inspectors/builtin/nginx/health.yaml")
CONFIG_MANIFEST = Path("src/hostlens/inspectors/builtin/nginx/config_test.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/nginx")
NGINX_IMAGE = "nginx@sha256:814a8e88df978ade80e584cc5b333144b9372a8e3c98872d07137dbf3b44d0e4"
BAD_CONF = Path("tests/inspectors/compose/nginx/bad.conf").resolve()


class _HostShellTarget:
    """`ExecutionTarget` that runs `exec` under `sh -c` on the local HOST.

    Declares only SHELL capability — curl lives in capability `shell`, and the
    nginx.health preflight gates on the `curl` BINARY (not docker_cli). This is
    `_record_docker_restart_loop._LocalDockerTarget` minus the DOCKER_CLI cap.
    """

    type = "local"

    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities: set[Capability] = {Capability.SHELL}

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
        raise AssertionError("read_file is not used by nginx.health")


class _DockerExecTarget:
    """`ExecutionTarget` running `exec` inside a named container via `docker exec`.

    Used for nginx.config_test: `nginx -t` needs the in-container nginx binary.
    Declares only SHELL (the collector gates on `nginx`/`jq` binaries).
    """

    type = "local"

    def __init__(self, name: str, container: str) -> None:
        self.name = name
        self.container = container
        self.capabilities: set[Capability] = {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        argv = ["docker", "exec"]
        for key in env or {}:
            argv += ["-e", key]
        argv += [self.container, "sh", "-c", cmd]
        proc = await asyncio.create_subprocess_exec(
            *argv,
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
        raise AssertionError("read_file is not used by nginx.config_test")


def _write(out_name: str, text: str) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / out_name
    path.write_text(text)
    print(f"wrote {path}")


def _rm(*names: str) -> None:
    subprocess.run(["docker", "rm", "-f", *names], capture_output=True)


def _apk_add_jq(container: str) -> None:
    proc = subprocess.run(
        ["docker", "exec", container, "apk", "add", "--no-cache", "jq"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"apk add jq failed in {container} (network?): {proc.stderr.strip()}")


def _wait_running(name: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip() == "true":
            return
        time.sleep(0.3)
    raise RuntimeError(f"{name} did not reach Running=true")


# --------------------------------------------------------------------------- #
# nginx.health — host curl against compose nginx (:18080) / a dead port (:18099)
# --------------------------------------------------------------------------- #


async def _record_health(out_name: str, *, params: dict[str, Any], allow_failed: bool) -> str:
    manifest = load_manifest(HEALTH_MANIFEST)
    target = _HostShellTarget("nginxrec")
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters=params,
        allow_failed=allow_failed,
    )
    text = fixture.to_json()
    _write(out_name, text)
    return text


async def _record_health_fixtures() -> None:
    compose_up("nginx")
    try:
        wait_healthy("nginx")
        await _record_health(
            "health_up.json",
            params={"host": "127.0.0.1", "port": 18080, "stub_status_path": "/stub_status"},
            allow_failed=False,
        )
        down = await _record_health(
            "health_down.json",
            params={"host": "127.0.0.1", "port": 18099, "stub_status_path": "/stub_status"},
            allow_failed=True,
        )
        # The collector command (curl to a dead port) must have exited non-zero.
        commands = json.loads(down)["commands"]
        collector = [c for c in commands if "curl -fsS" in c["cmd"]]
        assert collector and collector[-1]["exit_code"] != 0, collector
        print("health_down fixture recorded (fail-loud: collector exit != 0)")
    finally:
        compose_down("nginx")


# --------------------------------------------------------------------------- #
# nginx.config_test — `nginx -t` inside a throwaway container (valid / invalid)
# --------------------------------------------------------------------------- #


async def _record_config(out_name: str, container: str) -> str:
    manifest = load_manifest(CONFIG_MANIFEST)
    target = _DockerExecTarget("nginxrec", container)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters={},
        allow_failed=False,
    )
    text = fixture.to_json()
    _write(out_name, text)
    return text


async def _record_config_valid() -> None:
    name = "hlnginx-cfg-valid"
    _rm(name)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, NGINX_IMAGE, "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    try:
        _wait_running(name)
        _apk_add_jq(name)
        text = await _record_config("config_test_valid.json", name)
        doc = json.loads(text)
        collector = [c for c in doc["commands"] if "nginx -t" in c["cmd"]][-1]
        assert json.loads(collector["stdout"])["config_valid"] is True, collector["stdout"]
        print("config_test_valid fixture recorded (rc=0 → config_valid:true)")
    finally:
        _rm(name)


async def _record_config_invalid() -> None:
    name = "hlnginx-cfg-invalid"
    _rm(name)
    # Mount bad.conf as the EFFECTIVE /etc/nginx/nginx.conf; `sleep` keeps the
    # container alive even though the bad config would crash the real entrypoint.
    # `nginx -t` validates /etc/nginx/nginx.conf (= bad.conf) → rc=1.
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{BAD_CONF}:/etc/nginx/nginx.conf:ro",
            NGINX_IMAGE,
            "sleep",
            "3600",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_running(name)
        _apk_add_jq(name)
        text = await _record_config("config_test_invalid.json", name)
        # The detail must carry the escaped quote/backslash evidence (proves the
        # `jq -n --arg` escaping produced valid JSON instead of an exception).
        doc = json.loads(text)
        collector = [c for c in doc["commands"] if "nginx -t" in c["cmd"]]
        out = collector[-1]["stdout"]
        parsed = json.loads(out)
        assert parsed["config_valid"] is False, parsed
        assert parsed["detail"], "detail must be non-empty"
        assert '"' in parsed["detail"], parsed["detail"]
        assert "\\" in parsed["detail"], parsed["detail"]
        assert "\n" in parsed["detail"], parsed["detail"]
        print("config_test_invalid fixture recorded (rc=1 → config_valid:false + escaped detail)")
    finally:
        _rm(name)


async def _record_config_unexpected_rc() -> None:
    name = "hostlens-rec-nginx-badrc"
    _rm(name)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, NGINX_IMAGE, "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    try:
        _wait_running(name)
        _apk_add_jq(name)
        # Shadow the real nginx with a fake one that exits 2 (∉ {0,1}).
        # /usr/local/bin precedes /usr/sbin (where the real nginx lives) in the
        # alpine PATH, so `nginx -t` resolves to this shim and returns rc=2.
        shim = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-c",
                'printf "#!/bin/sh\\nexit 2\\n" > /usr/local/bin/nginx '
                "&& chmod +x /usr/local/bin/nginx && command -v nginx",
            ],
            capture_output=True,
            text=True,
        )
        assert shim.returncode == 0, shim.stderr
        assert shim.stdout.strip() == "/usr/local/bin/nginx", shim.stdout

        manifest = load_manifest(CONFIG_MANIFEST)
        target = _DockerExecTarget("nginxrec", name)
        fixture = await record_fixture(
            manifest,
            target,  # type: ignore[arg-type]
            settings=Settings(),
            parameters={},
            allow_failed=True,
        )
        text = fixture.to_json()
        _write("config_test_unexpected_rc.json", text)
        doc = json.loads(text)
        collector = [c for c in doc["commands"] if "nginx -t" in c["cmd"]][-1]
        assert collector["exit_code"] != 0, collector
        assert collector["stdout"] == "", collector
        print("config_test_unexpected_rc fixture recorded (rc=2 → exit 1 + empty stdout)")
    finally:
        _rm(name)


async def _main() -> None:
    await _record_health_fixtures()
    await _record_config_valid()
    await _record_config_invalid()
    await _record_config_unexpected_rc()


if __name__ == "__main__":
    asyncio.run(_main())
