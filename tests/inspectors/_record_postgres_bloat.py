"""One-shot fixture recorder for `postgres.bloat_tables` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against a throwaway
`postgres:16` docker container that ships its own `psql` + server. The local
machine has no `psql`; the container does, so a docker-exec `ExecutionTarget`
lets the runner render and dispatch the *real* command and capture the *real*
JSON output — zero drift, no `psql` install.

Usage (container `hl-pg` already running with the scenario databases seeded):

    PGPASSWORD=<throwaway-pw> .venv-impl/bin/python tests/inspectors/_record_postgres_bloat.py

(``<throwaway-pw>`` is whatever password the ephemeral ``hl-pg`` container was
started with — it is injected via the env and **redacted** from recorded
stdout/stderr, so the committed fixtures never contain any plaintext password.)

This module is intentionally NOT collected by pytest (filename has no `test_`
prefix) — it is a manual fixture-generation helper kept beside the snapshot
test for reproducibility (see the snapshot test's module docstring).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult

CONTAINER = "hl-pg"
MANIFEST = Path("src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/postgres_bloat_tables")


class _DockerExecTarget:
    """`ExecutionTarget` that runs `exec` inside a docker container via `sh -c`.

    Recording-only. Satisfies the `ExecutionTarget` Protocol (`name` / `type` /
    `capabilities` / `exec` / `read_file`). `env` (carrying the injected
    `PGPASSWORD` secret) is forwarded into the container with `docker exec -e
    NAME` so the secret reaches the real `psql` exactly as the runner intends —
    never spliced into the command string.
    """

    type = "local"

    def __init__(self, name: str, container: str) -> None:
        self.name = name
        self.container = container
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        # Run as the `postgres` OS user inside the container so the local-socket
        # peer auth maps to the `postgres` DB role — mirrors a real host where
        # the operator runs psql as a DB-capable user (the manifest itself
        # carries no hardcoded `-U`; connection identity is the caller's).
        argv = ["docker", "exec", "-u", "postgres"]
        for key in env or {}:
            argv += ["-e", key]
        argv += [self.container, "sh", "-c", cmd]
        # Merge the injected secret env over the local environment so `docker`
        # stays on PATH while `docker exec -e PGPASSWORD` forwards the secret
        # into the container (its value is read from this process env).
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
        raise AssertionError("read_file not used by postgres.bloat_tables")


async def _record(dbname: str, out_name: str) -> None:
    manifest = load_manifest(MANIFEST)
    target = _DockerExecTarget("recorder", CONTAINER)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters={"dbname": dbname},
    )
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")


async def _main() -> None:
    await _record("bloatdb", "bloated.json")
    await _record("healthydb", "healthy.json")
    await _record("emptydb", "empty.json")


if __name__ == "__main__":
    asyncio.run(_main())
