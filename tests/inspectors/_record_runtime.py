"""One-shot fixture recorder for the runtime (JVM + Go) inspectors.

`add-runtime-inspectors`: `jvm.heap`, `jvm.gc`, `jvm.threads`,
`go.goroutines`, `go.heap`.

These probe a JVM (jstat / jcmd) or a Go pprof endpoint (curl). Per the
established os-shell convention (design D-7, see `_record_security_pkg.py`) we
do NOT need a real JVM / Go host to record fixtures: we drive the **real**
`InspectorRunner` against a `_CaptureTarget` that

  * answers `command -v X` binary probes with a synthetic path (satisfying the
    jstat / jcmd / curl preflight),
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted ``main_stdout`` (+ optional non-zero exit code) for
    the rendered collect command,

while recording every exact rendered command into a sink. The command strings
are captured verbatim from the real renderer (never hand-written), so the
fixture can never drift from what `ReplayTarget` looks up at snapshot time
(byte-level match). The per-scenario stdout / exit_code is the scenario data we
author — it is the final JSON the collector pipeline emits on a host in the
given state (a dev machine could record this against a real JVM / real pprof,
but the recorded artefact is still a "command string → final JSON" pair).

`main_exit_code != 0` + empty stdout models the collector's fail-loud path
(`|| exit 1`): the runner sees a non-zero exit with empty stdout, the JSON
parser raises, and the inspector lands `status=exception` (the false-negative
guard for an absent pid / unreachable pprof endpoint). The collector's internal
shell correctness (jstat column-name anchoring D-4, jvm.gc window differencing
D-5, pgrep / curl fail-loud guards D-2) runs only on a real target and is
locked at the command-string level by the verbatim capture, NOT executed during
replay.

The JVM inspectors are recorded with `parameters={"pid": 4242}` (the direct
attach path; the rendered command embeds `4242`). The snapshot test replays
with the SAME parameters so the rendered command matches byte-for-byte. `jvm.gc`
declares a sampling_window, so a frozen clock is injected (matching the snapshot
test) — its command does not actually interpolate `window_start`, but the runner
requires a clock for sampling_window inspectors.

Run it to (re)write the fixtures:

    python tests/inspectors/_record_runtime.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "

# Fixed UTC instant the runner renders sampling_window inspectors against. The
# snapshot test injects the SAME instant so any window-derived rendering matches
# the recorded command byte-for-byte.
FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)

# JVM inspectors are recorded via the direct-attach path (pid set), so pgrep is
# never rendered and the command embeds this pid literally.
_JVM_PARAMS: dict[str, Any] = {"pid": 4242}


def frozen_clock() -> datetime:
    return FROZEN_DT


class _CaptureTarget:
    """Generation-only target: returns canned stdout/exit and records commands.

    Mirrors `_record_security_pkg._CaptureTarget`: `command -v X` probes return
    a synthetic path (satisfying jstat / jcmd / curl preflight), file probes
    return empty, and the rendered collect command gets the canned
    ``main_stdout`` / ``main_exit_code``. An exception scenario returns a
    non-zero exit + empty stdout (the collector's fail-loud path).
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        sink: list[dict[str, Any]],
        main_exit_code: int = 0,
        main_stderr: str = "",
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._main_exit_code = main_exit_code
        self._main_stderr = main_stderr
        self._sink = sink

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            stdout, stderr, code = f"/usr/bin/{binary}\n", "", 0
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout, stderr, code = "", "", 0
        else:
            stdout, stderr, code = self._main_stdout, self._main_stderr, self._main_exit_code
        self._sink.append(
            {
                "cmd": cmd,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": code,
                "duration_seconds": 0.0,
            }
        )
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


@dataclass(frozen=True)
class _Scenario:
    inspector: str  # manifest file stem
    domain: str  # "jvm" | "go" — builtin subdir + fixture subdir
    out_name: str  # fixture basename
    main_stdout: str  # the JSON the collector pipeline would emit
    expect_findings: bool
    parameters: dict[str, Any] = field(default_factory=dict)
    main_exit_code: int = 0
    main_stderr: str = ""
    expect_status: str = "ok"


_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- jvm.heap ------------------------------------------------------- #
    _Scenario(
        inspector="heap",
        domain="jvm",
        out_name="heap_critical.json",
        parameters=_JVM_PARAMS,
        main_stdout='{"heap_used_pct":96.0}',  # 96 >= 95 → critical
        expect_findings=True,
    ),
    _Scenario(
        inspector="heap",
        domain="jvm",
        out_name="heap_ok.json",
        parameters=_JVM_PARAMS,
        main_stdout='{"heap_used_pct":50.0}',  # 50 < 90 → no finding
        expect_findings=False,
    ),
    _Scenario(
        inspector="heap",
        domain="jvm",
        out_name="heap_unreachable.json",
        parameters=_JVM_PARAMS,
        # pid absent / cross-user attach → jstat non-zero → `|| exit 1` →
        # empty stdout + exit 1 → JSONDecodeError → status=exception.
        main_stdout="",
        main_exit_code=1,
        main_stderr="jstat: could not attach\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- jvm.gc (sampling_window) --------------------------------------- #
    _Scenario(
        inspector="gc",
        domain="jvm",
        out_name="gc_pressure.json",
        parameters=_JVM_PARAMS,
        # 8 >= 5 (full_gc_threshold) → warning fires.
        main_stdout='{"full_gc_delta":8,"gc_time_pct":2.0}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="gc",
        domain="jvm",
        out_name="gc_ok.json",
        parameters=_JVM_PARAMS,
        # 0 < 5 and 0.5 < 10 → no finding.
        main_stdout='{"full_gc_delta":0,"gc_time_pct":0.5}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="gc",
        domain="jvm",
        out_name="gc_unreachable.json",
        parameters=_JVM_PARAMS,
        main_stdout="",
        main_exit_code=1,
        main_stderr="jstat: could not attach\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- jvm.threads ---------------------------------------------------- #
    _Scenario(
        inspector="threads",
        domain="jvm",
        out_name="threads_spike.json",
        parameters=_JVM_PARAMS,
        # 700 >= 500 (max_threads) → warning fires; blocked=3 rides as evidence.
        main_stdout='{"thread_total":700,"blocked_total":3}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="threads",
        domain="jvm",
        out_name="threads_ok.json",
        parameters=_JVM_PARAMS,
        main_stdout='{"thread_total":100,"blocked_total":0}',  # 100 < 500
        expect_findings=False,
    ),
    _Scenario(
        inspector="threads",
        domain="jvm",
        out_name="threads_unreachable.json",
        parameters=_JVM_PARAMS,
        main_stdout="",
        main_exit_code=1,
        main_stderr="jcmd: could not attach\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- go.goroutines -------------------------------------------------- #
    _Scenario(
        inspector="goroutines",
        domain="go",
        out_name="goroutines_leak.json",
        # 50000 > 10000 (threshold) → warning fires.
        main_stdout='{"goroutines":50000}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="goroutines",
        domain="go",
        out_name="goroutines_ok.json",
        main_stdout='{"goroutines":100}',  # 100 <= 10000
        expect_findings=False,
    ),
    _Scenario(
        inspector="goroutines",
        domain="go",
        out_name="goroutines_unreachable.json",
        # pprof port down → curl -fsS non-zero → `|| exit 1` → empty + exit 1
        # → JSONDecodeError → status=exception.
        main_stdout="",
        main_exit_code=1,
        main_stderr="curl: (7) connection refused\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- go.heap -------------------------------------------------------- #
    _Scenario(
        inspector="heap",
        domain="go",
        out_name="heap_pressure.json",
        # 2_000_000_000 > 1_073_741_824 (threshold_bytes, 1 GiB) → warning.
        main_stdout='{"heap_inuse_bytes":2000000000,"heap_alloc_bytes":1500000000}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="heap",
        domain="go",
        out_name="heap_ok.json",
        main_stdout='{"heap_inuse_bytes":100000000,"heap_alloc_bytes":80000000}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="heap",
        domain="go",
        out_name="heap_unreachable.json",
        main_stdout="",
        main_exit_code=1,
        main_stderr="curl: (7) connection refused\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # HeapAlloc absent but HeapInuse present: a healthy endpoint with a usable
    # primary reading must NOT surface status=exception just because the
    # SUPPLEMENTAL alloc evidence is missing (the collector omits the optional
    # key; output_schema requires only heap_inuse_bytes). Below threshold → ok,
    # no finding. (Real Go MemStats always emits both; this locks the graceful
    # degrade — Cursor Bugbot review on go/heap.yaml.)
    _Scenario(
        inspector="heap",
        domain="go",
        out_name="heap_inuse_only_ok.json",
        main_stdout='{"heap_inuse_bytes":50000000}',
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("runtime-record")
    manifest = load_manifest(_BUILTIN_ROOT / scenario.domain / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger, clock=frozen_clock)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        main_exit_code=scenario.main_exit_code,
        main_stderr=scenario.main_stderr,
        sink=recorded,
    )
    result = await runner.run(manifest, target, scenario.parameters or None)

    assert result.status == scenario.expect_status, (
        f"{scenario.out_name}: status={result.status} (want {scenario.expect_status}) "
        f"error={result.error}"
    )
    if scenario.expect_findings:
        assert result.findings, (
            f"{scenario.out_name}: expected a finding but got none — check main_stdout"
        )
    else:
        assert not result.findings, (
            f"{scenario.out_name}: expected no finding but got {result.findings}"
        )

    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in recorded:
        if entry["cmd"] in seen:
            continue
        seen.add(entry["cmd"])
        commands.append(entry)

    fixture = {
        "impersonate": "local",
        "capabilities": sorted(cap_values),
        "commands": commands,
        "files": {},
    }
    out_dir = _FIXTURE_ROOT / scenario.domain
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)


if __name__ == "__main__":
    asyncio.run(_main())
