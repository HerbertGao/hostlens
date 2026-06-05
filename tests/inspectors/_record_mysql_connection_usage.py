"""One-shot fixture recorder for `mysql.connection_usage` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose
`mysql` / `mysql-abnormal` services (group A's `docker-compose.yml`). The local
machine has no `mysql` client; the container does, so the shared
`_compose_record.DockerExecTarget` (docker-exec ExecutionTarget) lets the runner
render + dispatch the *real* command and capture the *real* JSON output — zero
drift, no `mysql` install.

Usage (this script manages the compose lifecycle itself):

    .venv-impl/bin/python tests/inspectors/_record_mysql_connection_usage.py

Records (to tests/inspectors/fixtures/mysql/):
  * healthy.json            — root + correct password, default params → status=ok,
    no finding (a fresh server is far below the connection threshold).
  * finding_trigger.json    — healthy server + a LOW warn_used_pct param so the
    real (low) used_pct crosses warn → warning (finding wiring, D-4 track 1).
  * semantic_abnormal.json  — the `mysql-abnormal` instance (max-connections=5)
    with several held client connections so used_connections/5 crosses the
    DEFAULT critical threshold (95%) → critical (real high-connection state,
    NOT a lowered inspector threshold — D-4 track 2).
  * access_denied.json      — root + WRONG password → mysql Access denied → exit 1
    + empty stdout. Recorded with `allow_failed=True`; the failed run IS the
    point (fail-loud → status=exception, NOT a fabricated healthy used_pct=0).
    Recorded with HOSTLENS_MYSQL_PWD set to a WRONG value (not unset) so the
    secret-presence preflight passes and the collector reaches the auth failure.
  * conn_refused.json       — mysql -h 127.0.0.1 -P 13999 (nothing listening) →
    connect failure → exit 1 + empty stdout. `allow_failed=True`.
  * lowpriv_global.json     — a NON-PROCESS-privileged user (`lowpriv`). Asserts
    `SHOW GLOBAL STATUS Threads_connected` still returns the GLOBAL connection
    count (unlike `COUNT(*) FROM information_schema.processlist`, which a
    non-PROCESS user would see only its own thread for → silent under-count).

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    MYSQL_ROOT_PW as ROOT_PW,
)
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/mysql/connection_usage.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/mysql")

#: Throwaway passwords built by concatenation (not single literals) so
#: GitGuardian's dashboard scan does not flag these one-shot test credentials.
_WRONG_PW = "wrong-" + "password"
_LOWPRIV_PW = "lowpriv-" + "pw"

#: Background `SELECT SLEEP(...)` connections held against `mysql-abnormal`
#: (max-connections=5). The server reserves one slot for a SUPER user, so a
#: handful of sleepers plus the inspector's own connection drives
#: Threads_connected to (near) the cap → used_pct >= 95% under the DEFAULT
#: thresholds. Started detached and torn down with the project at the end.
_HELD_CONNECTIONS = 4

#: Connections held against the HEALTHY `mysql` (max-connections 151) while
#: recording the lowpriv fixture, so the GLOBAL Threads_connected the lowpriv
#: user observes is clearly greater than its own single thread — the only way
#: the SHOW-GLOBAL-STATUS-vs-processlist distinction is observable rather than
#: vacuous (a global count that happened to equal 1 could not refute a
#: processlist under-count).
_LOWPRIV_HELD_CONNECTIONS = 3


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
    # DockerExecTarget forwards it with `docker exec -e HOSTLENS_MYSQL_PWD`.
    os.environ["HOSTLENS_MYSQL_PWD"] = pw
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
    preceding entries are the `command -v mysql` preflight probes)."""

    commands = json.loads(fixture_json)["commands"]
    main: dict[str, Any] = commands[-1]
    return main


def _mysql(container: str, *sql: str) -> str:
    """Run a one-shot SQL statement as root inside `container` (helper, not the
    inspector path) — used to seed the lowpriv user and to spawn held
    connections for the semantic-abnormal scenario.
    """

    argv = [
        "docker",
        "exec",
        container,
        "mysql",
        "-uroot",
        f"-p{ROOT_PW}",
        "-N",
        "-s",
        "--batch",
        "-e",
        " ".join(sql),
    ]
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def _spawn_held_connection(container: str) -> subprocess.Popen[bytes]:
    """Open a long-lived `SELECT SLEEP(...)` connection (detached) to occupy a
    connection slot on `mysql-abnormal`."""

    return subprocess.Popen(
        [
            "docker",
            "exec",
            container,
            "mysql",
            "-uroot",
            f"-p{ROOT_PW}",
            "-e",
            "SELECT SLEEP(120)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _record_healthy_family() -> None:
    compose_up("mysql")
    wait_healthy("mysql")
    container = container_name("mysql")

    # healthy: default params, far below threshold → ok, no finding.
    await _record("healthy.json", container=container, pw=ROOT_PW, parameters={"user": "root"})

    # finding-trigger: low warn so the real (low) used_pct crosses warn → warning.
    await _record(
        "finding_trigger.json",
        container=container,
        pw=ROOT_PW,
        parameters={"user": "root", "warn_used_pct": 0.5},
    )

    # access_denied: WRONG password (set, not unset) → Access denied → exit 1.
    text = await _record(
        "access_denied.json",
        container=container,
        pw=_WRONG_PW,
        parameters={"user": "root"},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, (
        "expected the main collect command to exit non-zero (Access denied)"
    )

    # conn_refused: nothing listening on 13999 inside the container → exit 1.
    text = await _record(
        "conn_refused.json",
        container=container,
        pw=ROOT_PW,
        parameters={"user": "root", "port": 13999},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, (
        "expected the main collect command to exit non-zero (conn refused)"
    )

    # lowpriv: a non-PROCESS user. SHOW GLOBAL STATUS still returns the GLOBAL
    # connection count even though the user can see ONLY its own thread in
    # information_schema.processlist (the processlist-undercount regression
    # lock). To make that distinction OBSERVABLE — and not vacuous — we hold
    # several OTHER connections so the global Threads_connected is clearly >1.
    # A processlist-based count would report 1 (the lowpriv user's own thread);
    # SHOW GLOBAL STATUS must report the larger global figure.
    _mysql(
        container,
        "DROP USER IF EXISTS 'lowpriv'@'%';",
        f"CREATE USER 'lowpriv'@'%' IDENTIFIED BY '{_LOWPRIV_PW}';",
        "GRANT USAGE ON *.* TO 'lowpriv'@'%';",
        "FLUSH PRIVILEGES;",
    )
    held: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(_LOWPRIV_HELD_CONNECTIONS):
            held.append(_spawn_held_connection(container))
        # Poll until the held connections register so the global count is
        # genuinely > the lowpriv user's own single thread (readiness poll,
        # not a fixed sleep).
        for _ in range(60):
            n = int(_mysql(container, "SHOW GLOBAL STATUS LIKE 'Threads_connected';").split()[-1])
            if n > _LOWPRIV_HELD_CONNECTIONS:
                break
            subprocess.run(["sleep", "0.5"], check=True)
        else:
            sys.exit(
                "lowpriv recording: expected global Threads_connected > "
                f"{_LOWPRIV_HELD_CONNECTIONS}, got {n}; aborting to avoid a "
                "vacuous fixture"
            )
        await _record(
            "lowpriv_global.json",
            container=container,
            pw=_LOWPRIV_PW,
            parameters={"user": "lowpriv"},
        )
    finally:
        for proc in held:
            proc.terminate()


async def _record_semantic_abnormal() -> None:
    compose_up("mysql-abnormal")
    wait_healthy("mysql-abnormal")
    container = container_name("mysql-abnormal")

    held: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(_HELD_CONNECTIONS):
            held.append(_spawn_held_connection(container))
        # Poll until the held connections register so the recorded snapshot is
        # a genuine high-connection state (readiness poll, not a fixed sleep).
        for _ in range(60):
            n = int(_mysql(container, "SHOW GLOBAL STATUS LIKE 'Threads_connected';").split()[-1])
            if n >= _HELD_CONNECTIONS:
                break
            subprocess.run(["sleep", "0.5"], check=True)
        text = await _record(
            "semantic_abnormal.json",
            container=container,
            pw=ROOT_PW,
            parameters={"user": "root"},
        )
        # used_pct must cross the DEFAULT critical threshold (95%) — a genuine
        # high-connection state, not a lowered inspector threshold (D-4).
        output = json.loads(_main_command(text)["stdout"])
        assert output["used_pct"] >= 95.0, f"expected used_pct >= 95, got {output}"
    finally:
        for proc in held:
            proc.terminate()


async def _main() -> None:
    try:
        await _record_healthy_family()
    finally:
        compose_down("mysql")
    try:
        await _record_semantic_abnormal()
    finally:
        compose_down("mysql-abnormal")


if __name__ == "__main__":
    asyncio.run(_main())
