"""One-shot fixture recorder for `redis.persistence` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose `redis`
service (`--save ""` → no RDB autosave, AOF off → aof_enabled=0). Each container
ships its own `redis-cli` + server, so a docker-exec `ExecutionTarget` lets the
runner render and dispatch the *real* command and capture the *real* JSON output
— zero drift, no local `redis-cli` install. Readiness is polled via the compose
healthcheck (`wait_healthy`), never a fixed `sleep` (design D-5).

Usage (this script manages the compose lifecycle itself):

    PYTHONPATH=. .venv-impl/bin/python tests/inspectors/_record_redis_persistence.py

Records (into tests/inspectors/fixtures/redis/):
  * persistence_healthy.json          — healthy redis, no auth, a HANDFUL of
    SETs (rdb_changes < warn_changes default) → no finding (status=ok).
  * persistence_conn_refused.json     — fail-loud path: redis-cli points at a
    closed port (6390), so `INFO persistence` exits non-zero with empty stdout.
    Recorded with `allow_failed=True` → the runner collapses to status=exception.
  * persistence_semantic_abnormal.json — AOF-OFF instance after ~500 SETs, so
    rdb_changes_since_last_save >= warn_changes default at the DEFAULT threshold
    → a warning (real RDB snapshot debt, an instant snapshot — D-4). aof_enabled=0.
  * persistence_aof_on.json           — SAME instance with `CONFIG SET appendonly
    yes` and ~500 SETs (rdb_changes >= warn_changes), so aof_enabled=1: the AOF
    premise gate (design D-8) suppresses the finding even though rdb_changes is
    over threshold. Proves "no finding" is contributed PURELY by the AOF gate,
    not by an under-threshold count (a vacuous pass).
  * persistence_special_char_pw.json  — AUTH instance whose password contains a
    space + glob metachar (`p w*d`), recorded with HOSTLENS_REDIS_PASSWORD set to
    that value, then a handful of SETs (rdb_changes < warn_changes) → healthy
    snapshot, status=ok, aof_enabled=0. This fixture exists so the leak-scan in
    `test_service_contract_crosscheck.py` is NON-VACUOUS for redis.persistence
    (task 6.4): the no-auth fixtures never inject any plaintext password value, so
    without an auth fixture the redaction scan would never see a value that could
    leak. The recorder asserts the recorded text does NOT contain `p w*d`, proving
    the collector's REDISCLI_AUTH env remap keeps the secret out of argv/output.

`warn_changes` default is pinned at 100 in the manifest; the recorder asserts the
recorded counts land on either side of it (healthy < 100 <= abnormal/aof_on), so
the threshold is bound to the REAL recorded量纲 (tasks 2.3), not chosen to force
a trigger.

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/redis/persistence.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/redis")

#: The manifest's frozen `warn_changes` default (design D-8 / tasks 2.3). Healthy
#: writes must land strictly below it; abnormal / aof_on writes must reach it.
WARN_CHANGES_DEFAULT = 100

#: How many keys to write for the over-threshold scenarios. 500 >> 100 leaves
#: ample margin so the assertion is non-flaky.
ABNORMAL_WRITES = 500
#: Healthy: a handful of keys, well below the default threshold.
HEALTHY_WRITES = 3

#: Password with a space AND a glob metachar — the redaction / non-vacuous-leak-
#: scan payload (task 6.4). Same value as
#: `_record_redis_memory_usage.SPECIAL_PW` and the crosscheck's
#: `_RECORDED_SECRET_VALUES` "p w*d" entry (kept in lock-step, defined locally to
#: avoid cross-recorder coupling).
SPECIAL_PW = "p w*d"


async def _record(
    out_name: str,
    *,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", container_name("redis"))
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
    return json.loads(path.read_text())


async def _exec(*argv: str) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        container_name("redis"),
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", errors="replace")


async def _write_keys(count: int, prefix: str) -> None:
    """Write `count` keys via redis-cli (each SET increments rdb_changes)."""

    for i in range(count):
        rc, _ = await _exec("redis-cli", "SET", f"hostlens:{prefix}:{i}", "x")
        assert rc == 0, f"SET {prefix}:{i} failed"


def _output_of(fixture: dict[str, Any]) -> dict[str, Any]:
    """Parse the collector's recorded stdout (the last command) as JSON output."""

    main = fixture["commands"][-1]
    assert main["exit_code"] == 0, f"main command failed: {main}"
    return json.loads(main["stdout"])  # type: ignore[no-any-return]


async def _record_family() -> None:
    # No-auth instance: export the declared secret as EMPTY so preflight's
    # secret-presence gate passes and the collector takes its no-auth branch.
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""

    # --- healthy: fresh instance + a handful of SETs → rdb_changes < default.
    compose_up("redis")
    try:
        wait_healthy("redis")
        await _write_keys(HEALTHY_WRITES, "healthy")
        healthy = _output_of(await _record("persistence_healthy.json"))
        assert healthy["aof_enabled"] == 0, healthy
        assert healthy["rdb_changes_since_last_save"] < WARN_CHANGES_DEFAULT, healthy
        print(
            f"healthy: rdb_changes={healthy['rdb_changes_since_last_save']} (< {WARN_CHANGES_DEFAULT})"
        )

        # conn_refused (fail-loud): point redis-cli at a closed port. INFO exits
        # non-zero → empty stdout → status=exception.
        refused = await _record(
            "persistence_conn_refused.json",
            parameters={"port": 6390},  # nothing listening
            allow_failed=True,
        )
        main = refused["commands"][-1]
        assert main["exit_code"] != 0, "expected the main collect command to have non-zero exit"
        print("conn_refused fixture has non-zero main-command exit")
    finally:
        compose_down("redis")

    # --- semantic-abnormal: fresh AOF-OFF instance + ~500 SETs → rdb_changes
    # >= default threshold → a warning at the DEFAULT (no override).
    compose_up("redis")
    try:
        wait_healthy("redis")
        await _write_keys(ABNORMAL_WRITES, "abnormal")
        abnormal = _output_of(await _record("persistence_semantic_abnormal.json"))
        assert abnormal["aof_enabled"] == 0, abnormal
        assert abnormal["rdb_changes_since_last_save"] >= WARN_CHANGES_DEFAULT, abnormal
        print(
            f"semantic_abnormal: aof_enabled=0 "
            f"rdb_changes={abnormal['rdb_changes_since_last_save']} (>= {WARN_CHANGES_DEFAULT})"
        )
    finally:
        compose_down("redis")

    # --- aof_on: fresh instance, switch AOF ON at runtime, then ~500 SETs so
    # rdb_changes >= default — but aof_enabled=1, so the AOF premise gate (D-8)
    # suppresses the finding. Same量纲 as abnormal; only aof_enabled differs.
    compose_up("redis")
    try:
        wait_healthy("redis")
        rc, _ = await _exec("redis-cli", "CONFIG", "SET", "appendonly", "yes")
        assert rc == 0, "CONFIG SET appendonly yes failed"
        await _write_keys(ABNORMAL_WRITES, "aofon")
        aof_on = _output_of(await _record("persistence_aof_on.json"))
        assert aof_on["aof_enabled"] == 1, aof_on
        assert aof_on["rdb_changes_since_last_save"] >= WARN_CHANGES_DEFAULT, aof_on
        print(
            f"aof_on: aof_enabled=1 "
            f"rdb_changes={aof_on['rdb_changes_since_last_save']} (>= {WARN_CHANGES_DEFAULT})"
        )
    finally:
        # Reset AOF off (defensive; compose_down -v destroys the container anyway).
        await _exec("redis-cli", "CONFIG", "SET", "appendonly", "no")
        compose_down("redis")

    # --- special-char password (task 6.4 non-vacuous leak scan): AUTH instance,
    # a handful of SETs (rdb_changes < default → healthy, status=ok), recorded
    # with the matching secret so the redaction guard sees a real injected value
    # that must NOT appear in the recorded fixture.
    compose_up("redis")
    try:
        wait_healthy("redis")
        # Write keys BEFORE enabling auth (the docker-exec SETs below run without
        # credentials); each SET still increments rdb_changes for the snapshot.
        await _write_keys(HEALTHY_WRITES, "authpw")
        set_pw = await _exec("redis-cli", "CONFIG", "SET", "requirepass", SPECIAL_PW)
        assert set_pw[0] == 0, f"CONFIG SET requirepass failed: {set_pw}"
        os.environ["HOSTLENS_REDIS_PASSWORD"] = SPECIAL_PW
        try:
            fixture = await _record("persistence_special_char_pw.json")
            special = _output_of(fixture)
            assert special["aof_enabled"] == 0, special
            assert special["rdb_changes_since_last_save"] < WARN_CHANGES_DEFAULT, special
            # The whole point: the injected password must be redacted out.
            assert SPECIAL_PW not in json.dumps(fixture), "plaintext password leaked into fixture"
            print(
                f"special_char_pw: aof_enabled=0 "
                f"rdb_changes={special['rdb_changes_since_last_save']} (< {WARN_CHANGES_DEFAULT}); "
                f"no plaintext '{SPECIAL_PW}' in fixture"
            )
        finally:
            os.environ["HOSTLENS_REDIS_PASSWORD"] = ""
    finally:
        compose_down("redis")


async def _main() -> None:
    await _record_family()


if __name__ == "__main__":
    asyncio.run(_main())
