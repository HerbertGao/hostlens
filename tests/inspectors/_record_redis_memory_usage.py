"""One-shot fixture recorder for `redis.memory_usage` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose redis
services (see `tests/inspectors/compose/docker-compose.yml`). Each container
ships its own `redis-cli` + server, so a docker-exec `ExecutionTarget` lets the
runner render and dispatch the *real* command and capture the *real* JSON output
— zero drift, no local `redis-cli` install. Readiness is polled via the compose
healthcheck (`wait_healthy`), never a fixed `sleep` (design D-5).

Usage (this script manages the compose lifecycle itself):

    .venv-impl/bin/python tests/inspectors/_record_redis_memory_usage.py

Records (into tests/inspectors/fixtures/redis/):
  * memory_usage_healthy.json          — healthy redis, no auth (HOSTLENS_REDIS_PASSWORD=""),
    default thresholds → no finding (status=ok).
  * memory_usage_finding_trigger.json  — the FRESH (low-usage) `redis-abnormal`
    instance (maxmemory 2mb, so used_pct is a real number, not null) recorded
    with a lowered warn_used_pct so the wiring fires a *warning* at a usage level
    that is healthy under the defaults (validates finding wiring ONLY; NOT a
    semantic-abnormal fixture). The healthy `redis` service has no maxmemory →
    used_pct null → it can never trigger a pct finding, so a maxmemory instance
    is required to exercise the wiring.
  * memory_usage_semantic_abnormal.json — the SAME `redis-abnormal` instance after
    being filled with real data until used_pct >= 95, so the DEFAULT thresholds
    fire a critical (real high-memory state — D-4).
  * memory_usage_conn_refused.json     — fail-loud path: redis-cli points at a
    closed port (6390), so `INFO memory` exits non-zero with empty stdout.
    Recorded with `allow_failed=True` (the failed run IS the point) → the runner
    collapses this to status=exception (the honesty regression lock).
  * memory_usage_special_char_pw.json  — auth instance whose password contains a
    space + glob metachar (`p w*d`), recorded with HOSTLENS_REDIS_PASSWORD set to
    that value. Proves the REDISCLI_AUTH env-remap channel does NOT word-split the
    password into bogus args (would be a bogus auth failure with unquoted `-a`).

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
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/redis/memory_usage.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/redis")

#: Password with a space AND a glob metachar — the word-split / unquoted-`-a`
#: regression payload. It must survive intact through the REDISCLI_AUTH env
#: channel (env values are never word-split), not through argv.
SPECIAL_PW = "p w*d"


async def _record(
    service: str,
    out_name: str,
    *,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", container_name(service))
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


def _exec(service: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container_name(service), *argv],
        capture_output=True,
        text=True,
    )


def _fill_to_critical(service: str) -> None:
    """Write real data into `redis-abnormal` until used_pct >= 95.

    The instance runs with `--maxmemory 2mb --maxmemory-policy noeviction`, so
    writing past the limit drives used_memory toward maxmemory (real high-memory
    state, NOT a lowered inspector threshold — D-4). `noeviction` means writes
    eventually start failing with OOM once the limit is hit; we stop as soon as
    INFO reports used_pct >= 96 (a margin above the 95 critical default).
    """

    padding = "x" * 4096
    for i in range(2000):
        # Ignore OOM write rejections under noeviction — they mean we're at the
        # limit, which is exactly the state we want to capture.
        _exec(service, "redis-cli", "SET", f"hostlens:fill:{i}", padding)
        if i % 25 != 0:
            continue
        info = _exec(service, "redis-cli", "INFO", "memory").stdout
        used = max_ = 0
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("used_memory:"):
                used = int(line.split(":", 1)[1])
            elif line.startswith("maxmemory:"):
                max_ = int(line.split(":", 1)[1])
        if max_ > 0 and (used / max_) * 100 >= 96.0:
            print(f"redis-abnormal filled: used={used} max={max_} pct={(used / max_) * 100:.2f}")
            return
    raise RuntimeError("redis-abnormal did not reach used_pct >= 96 within fill budget")


async def _record_redis_family() -> None:
    # --- healthy + finding-trigger: no-auth redis, export empty secret so
    # preflight's secret-presence gate passes and the collector takes its
    # no-auth branch (design D-3).
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""
    compose_up("redis")
    wait_healthy("redis")

    # healthy: default thresholds, fresh instance → no maxmemory → used_pct null
    # → no finding (genuine healthy → status=ok).
    await _record("redis", "memory_usage_healthy.json")

    # conn_refused (fail-loud): point redis-cli at a closed port. The collector's
    # INFO memory exits non-zero → empty stdout → status=exception.
    text = await _record(
        "redis",
        "memory_usage_conn_refused.json",
        parameters={"port": 6390},  # nothing listening
        allow_failed=True,
    )
    main = json.loads(text)["commands"][-1]
    assert main["exit_code"] != 0, "expected the main collect command to have a non-zero exit_code"
    print("conn_refused fixture has non-zero main-command exit")

    # --- special-char password: prove the REDISCLI_AUTH env remap does not
    # word-split a password containing a space + glob metachar. Set a real
    # requirepass on the healthy instance, then record with the matching secret.
    set_pw = _exec("redis", "redis-cli", "CONFIG", "SET", "requirepass", SPECIAL_PW)
    assert set_pw.returncode == 0, set_pw.stderr
    os.environ["HOSTLENS_REDIS_PASSWORD"] = SPECIAL_PW
    try:
        await _record("redis", "memory_usage_special_char_pw.json")
    finally:
        # Reset auth so the recorder env state cannot leak into a re-record.
        _exec(
            "redis",
            "redis-cli",
            "-a",
            SPECIAL_PW,
            "--no-auth-warning",
            "CONFIG",
            "SET",
            "requirepass",
            "",
        )


async def _record_abnormal_family() -> None:
    # --- redis-abnormal: a maxmemory=2mb instance so used_pct is a real number.
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""
    compose_up("redis-abnormal")
    wait_healthy("redis-abnormal")

    # finding-trigger: FRESH (low-usage) abnormal instance with a lowered warn
    # threshold (and a high critical, kept below 100 so the fresh ~few-pct usage
    # falls in [warn, critical) → a *warning*). Validates wiring ONLY — at the
    # default thresholds this same usage produces no finding.
    await _record(
        "redis-abnormal",
        "memory_usage_finding_trigger.json",
        parameters={"warn_used_pct": 0.0, "critical_used_pct": 99.0},
    )

    # semantic-abnormal: fill the SAME instance to real >=95%, record at DEFAULT
    # thresholds → critical.
    _fill_to_critical("redis-abnormal")
    await _record("redis-abnormal", "memory_usage_semantic_abnormal.json")


async def _main() -> None:
    try:
        await _record_redis_family()
    finally:
        compose_down("redis")
    try:
        await _record_abnormal_family()
    finally:
        compose_down("redis-abnormal")


if __name__ == "__main__":
    asyncio.run(_main())
