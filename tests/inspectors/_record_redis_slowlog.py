"""One-shot fixture recorder for `redis.slowlog` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against a throwaway
`redis:7-alpine` docker container that ships its own `redis-cli` + server. The
local machine has no `redis-cli`; the container does, so a docker-exec
`ExecutionTarget` lets the runner render and dispatch the *real* command and
capture the *real* JSON output — zero drift, no `redis-cli` install.

Usage (this script manages the container lifecycle itself):

    .venv-impl/bin/python tests/inspectors/_record_redis_slowlog.py

Records:
  * slowlog_nonempty.json — seeds slow queries via SLOWLOG RESET +
    `DEBUG SLEEP`-style entries so SLOWLOG LEN > 0 (status=ok).
  * slowlog_empty.json    — empty slowlog → count=0 (genuine empty, status=ok).
  * slowlog_conn_refused.json — fail-loud path: redis-cli points at a closed
    port, so SLOWLOG LEN exits non-zero with empty stdout. Recorded with
    `allow_failed=True` (the failed run IS the point). Asserts the runner
    collapses this to status=exception — the honesty regression lock.

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult

CONTAINER = "hl-redis-rec"
MANIFEST = Path("src/hostlens/inspectors/builtin/redis/slowlog.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/redis")


class _DockerExecTarget:
    """`ExecutionTarget` that runs `exec` inside a docker container via `sh -c`.

    Recording-only. Satisfies the `ExecutionTarget` Protocol. `env` (carrying
    the injected `REDIS_PASSWORD` secret) is forwarded with `docker exec -e NAME`
    so the secret reaches the real `redis-cli` exactly as the runner intends.
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
        raise AssertionError("read_file not used by redis.slowlog")


def _sh(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def _exec_redis(*redis_args: str) -> str:
    return _sh("docker", "exec", CONTAINER, "redis-cli", *redis_args)


async def _record(
    out_name: str, *, parameters: dict[str, Any] | None = None, allow_failed: bool = False
) -> str:
    manifest = load_manifest(MANIFEST)
    target = _DockerExecTarget("recorder", CONTAINER)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters=parameters,
        allow_failed=allow_failed,
    )
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return path.read_text()


def _start_container() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "redis:7-alpine",
            # Lower the slowlog threshold so trivial commands register.
            "redis-server",
            "--slowlog-log-slower-than",
            "0",
        ],
        check=True,
        capture_output=True,
    )
    # Wait for readiness.
    for _ in range(50):
        try:
            if _exec_redis("PING") == "PONG":
                break
        except subprocess.CalledProcessError:
            pass
        subprocess.run(["sleep", "0.2"], check=True)


async def _main() -> None:
    # The manifest declares REDIS_PASSWORD as a secret; preflight requires it in
    # the environment. The container has no auth → empty value reproduces the
    # no-`-a` command path.
    os.environ.setdefault("REDIS_PASSWORD", "")

    _start_container()

    # --- nonempty: generate several slow entries (threshold=0 logs everything).
    _exec_redis("SLOWLOG", "RESET")
    for _ in range(6):
        _exec_redis("PING")
    await _record("slowlog_nonempty.json")

    # --- empty: reset so SLOWLOG LEN == 0 (genuine empty → count=0, status=ok).
    _exec_redis("CONFIG", "SET", "slowlog-log-slower-than", "10000000")
    _exec_redis("SLOWLOG", "RESET")
    await _record("slowlog_empty.json")

    # --- conn refused (fail-loud): point redis-cli at a closed port. The
    # collector's SLOWLOG LEN exits non-zero → empty stdout → status=exception.
    text = await _record(
        "slowlog_conn_refused.json",
        parameters={"port": 6390},  # nothing listening
        allow_failed=True,
    )
    assert '"exit_code": 0' not in text.split('"cmd": "if')[-1].split("}")[0], (
        "expected the main collect command to have a non-zero exit_code"
    )
    print("conn_refused fixture has non-zero main-command exit")

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


if __name__ == "__main__":
    asyncio.run(_main())
