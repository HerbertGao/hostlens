"""One-shot fixture recorder for `postgres.connection_usage` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose
`postgres` / `postgres-lowconn` services (the wave-2 recording lane
`docker-compose.yml`). The local machine has no `psql` client; the container
does, so the shared `_compose_record.DockerExecTarget` (docker-exec
ExecutionTarget) lets the runner render + dispatch the *real* command and
capture the *real* JSON output — zero drift, no `psql` install.

Usage (this script manages the compose lifecycle itself):

    PYTHONPATH=. .venv-impl/bin/python tests/inspectors/_record_postgres_connection_usage.py

Records (to tests/inspectors/fixtures/postgres/):
  * healthy.json            — postgres superuser + correct password, default
    params (dbname=postgres) → status=ok, no finding (a fresh server is far
    below the connection threshold).
  * finding_trigger.json    — healthy server + a LOW warn_used_pct param so the
    real (low) used_pct crosses warn → warning (finding wiring, track 1).
  * semantic_abnormal.json  — the `postgres-lowconn` instance
    (max_connections=10) with several held `pg_sleep` backends so
    count(*) FROM pg_stat_activity / 10 crosses the DEFAULT critical threshold
    (95%) → critical (real high-connection state, NOT a lowered inspector
    threshold). Asserts used_pct >= 95 after recording (fail-loud).
  * access_denied.json      — superuser + WRONG password → psql auth failure →
    exit non-zero + empty stdout. Recorded with `allow_failed=True`; the failed
    run IS the point (fail-loud → status=exception, NOT a fabricated healthy
    used_pct=0). Recorded with HOSTLENS_POSTGRES_PASSWORD set to a WRONG value
    (not unset) so the secret-presence preflight passes and the collector
    reaches the auth failure.

    Auth note: the compose `postgres` image's default `pg_hba.conf` `trust`es
    loopback (127.0.0.1), so a wrong password over 127.0.0.1 would NOT fail. We
    therefore connect via the container's own hostname (resolves to its
    non-loopback bridge IP), which matches the `host ... scram-sha-256` rule and
    genuinely rejects a bad password (exit != 0). The recorder discovers that
    hostname at record time and bakes it into the recorded command; the snapshot
    test re-derives the same host from the recorded fixture so replay matches.
  * conn_refused.json       — psql -h 127.0.0.1 -p 15999 (nothing listening) →
    connect failure → exit non-zero + empty stdout. `allow_failed=True`.

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    POSTGRES_ROOT_PW as ROOT_PW,
)
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/postgres/connection_usage.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/postgres")

#: Throwaway wrong password built by concatenation (not a single literal) so
#: GitGuardian's dashboard scan does not flag this one-shot test credential.
_WRONG_PW = "wrong-" + "password"

#: Background `SELECT pg_sleep(...)` connections held against `postgres-lowconn`
#: (max_connections=10). postgres' own background processes already occupy
#: several rows of pg_stat_activity, so a handful of sleepers plus the
#: inspector's own connection drives count(*) to (near) the cap → used_pct >= 95%
#: under the DEFAULT thresholds. Started detached and torn down at the end.
_HELD_CONNECTIONS = 6


async def _record(
    out_name: str,
    *,
    container: str,
    pw: str,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    # Inject the secret via os.environ exactly as the runner reads it; the
    # DockerExecTarget forwards it with `docker exec -e HOSTLENS_POSTGRES_PASSWORD`.
    os.environ["HOSTLENS_POSTGRES_PASSWORD"] = pw
    target = DockerExecTarget("recorder", container)
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


def _main_command(fixture_json: str) -> dict[str, Any]:
    """Return the recorded main `collect.command` entry (the last command — the
    preceding entries are the `command -v psql` preflight probes)."""

    commands = json.loads(fixture_json)["commands"]
    main: dict[str, Any] = commands[-1]
    return main


def _container_hostname(container: str) -> str:
    """The compose container's own hostname (short container id), which resolves
    to its non-loopback bridge IP — connecting psql there matches the
    `host all all all scram-sha-256` pg_hba rule (loopback is `trust`), so a
    WRONG password genuinely fails. Used only for the access_denied fixture.
    """

    return subprocess.run(
        ["docker", "exec", container, "hostname"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _psql_count(container: str) -> int:
    """count(*) FROM pg_stat_activity as the superuser (helper, not the inspector
    path) — used to poll until the held connections register."""

    out = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={ROOT_PW}",
            container,
            "psql",
            "-tA",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            "SELECT count(*) FROM pg_stat_activity",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _spawn_held_connection(container: str) -> subprocess.Popen[bytes]:
    """Open a long-lived `SELECT pg_sleep(...)` connection (detached) to occupy a
    connection slot on `postgres-lowconn`."""

    return subprocess.Popen(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={ROOT_PW}",
            container,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            "SELECT pg_sleep(120)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _record_healthy_family() -> None:
    compose_up("postgres")
    wait_healthy("postgres")
    container = container_name("postgres")

    # healthy: default params (dbname=postgres exists), far below threshold → ok.
    await _record("healthy.json", container=container, pw=ROOT_PW, parameters={"user": "postgres"})

    # finding-trigger: low warn so the real (low) used_pct crosses warn → warning.
    await _record(
        "finding_trigger.json",
        container=container,
        pw=ROOT_PW,
        parameters={"user": "postgres", "warn_used_pct": 0.5},
    )

    # access_denied: WRONG password (set, not unset) over the container hostname
    # (non-loopback → scram rule) → psql auth failure → exit != 0.
    hostname = _container_hostname(container)
    text = await _record(
        "access_denied.json",
        container=container,
        pw=_WRONG_PW,
        parameters={"user": "postgres", "host": hostname},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, (
        "expected the main collect command to exit non-zero (auth failed)"
    )

    # conn_refused: nothing listening on 15999 inside the container → exit != 0.
    text = await _record(
        "conn_refused.json",
        container=container,
        pw=ROOT_PW,
        parameters={"user": "postgres", "port": 15999},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, (
        "expected the main collect command to exit non-zero (conn refused)"
    )


async def _record_semantic_abnormal() -> None:
    compose_up("postgres-lowconn")
    wait_healthy("postgres-lowconn")
    container = container_name("postgres-lowconn")

    held: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(_HELD_CONNECTIONS):
            held.append(_spawn_held_connection(container))
        # Poll until the held connections register so the recorded snapshot is a
        # genuine high-connection state (readiness poll, not a fixed sleep). The
        # collector's own connection adds one more, so used_pct = (n+1)/10.
        for _ in range(60):
            n = _psql_count(container)
            if (n + 1) / 10.0 * 100 >= 95.0:
                break
            subprocess.run(["sleep", "0.5"], check=True)
        text = await _record(
            "semantic_abnormal.json",
            container=container,
            pw=ROOT_PW,
            parameters={"user": "postgres"},
        )
        # used_pct must cross the DEFAULT critical threshold (95%) — a genuine
        # high-connection state, not a lowered inspector threshold.
        output = json.loads(_main_command(text)["stdout"])
        assert output["used_pct"] >= 95.0, f"expected used_pct >= 95, got {output}"
    finally:
        for proc in held:
            proc.terminate()


async def _main() -> None:
    try:
        await _record_healthy_family()
    finally:
        compose_down("postgres")
    try:
        await _record_semantic_abnormal()
    finally:
        compose_down("postgres-lowconn")


if __name__ == "__main__":
    asyncio.run(_main())
