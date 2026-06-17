"""Offline severity tests for `linux.cpu.top_processes` (D-7 os-shell convention).

`calibrate-top-processes-transient-cpu`: the top-processes inspector now gates a
high `%cpu` reading on the process having lived at least `min_etimes` seconds
(`int(p.etimes) >= min_etimes`), suppressing the `%cpu = cputime/realtime`
artefact of a just-spawned / self-spawned CPU-bound process (the real-machine
`bandwagon` `journalctl pid 33948` 100% CPU false alarm).

Per the os-shell fixture convention ([[project_d7_os_shell_fixture_convention]])
these run the **real** `InspectorRunner` against a `_CaptureTarget` that answers
the `command -v` binary probe, hand-crafts the `ps` table stdout the collector
pipeline would emit on the host, and records the exact rendered collect command
so it can be asserted byte-for-byte (command-string lock — the collector shell is
not offline-validated, its correctness is pinned by the locked command string +
the real-host Demo Path, not by these fixtures).

The two load-bearing anchors:

  * artefact-suppression: `cpu_pct >= 90` but `etimes < min_etimes` → ZERO
    findings (a young / transient process is not a fault),
  * sustained occupancy: `etimes >= min_etimes` AND `cpu_pct >= 90` → a
    `critical` finding (and `[70, 90)` → `warning`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hostlens"
    / "inspectors"
    / "builtin"
    / "linux"
    / "cpu_top_processes.yaml"
)

# The exact collect command the renderer produces from the manifest (no Jinja2
# substitution — the collect block is a static shell snippet). Locking it here
# means any drift in the collector pipeline (e.g. dropping the `etimes` field)
# fails this test loudly rather than silently changing what we feed the parser.
_EXPECTED_COLLECT = "ps -eo pid,pcpu,pmem,etimes,comm --sort=-pcpu --no-headers | head -n 10"

_PROBE_PREFIX = "command -v "


class _CaptureTarget:
    """Offline target: answers binary probes, returns canned `ps` table stdout
    for the main collect command, and records every rendered command into
    `commands`.

    `command -v <bin>` probes succeed with a synthetic path (so the capability
    preflight passes for the `[ps]` requires_binaries); everything else is the
    inspector's main command and returns `main_stdout` (the table rows the
    collector would print on a host in the authored process state).
    """

    type = "local"

    def __init__(self, name: str, *, main_stdout: str) -> None:
        self.name = name
        self.capabilities: set[Capability] = {Capability.SHELL}
        self._main_stdout = main_stdout
        self.commands: list[str] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        self.commands.append(cmd)
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        return ExecResult(
            exit_code=0,
            stdout=self._main_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


def _logger() -> Any:
    return structlog.get_logger("test-cpu-top-processes")


async def _run(main_stdout: str) -> tuple[_CaptureTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST)
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())
    target = _CaptureTarget("cpu-host", main_stdout=main_stdout)
    result = await runner.run(manifest, target)
    return target, result


def _assert_collect_command_locked(target: _CaptureTarget) -> None:
    """The rendered main collect command must match the manifest byte-for-byte
    (command-string lock — probes are filtered out). The field list MUST carry
    `etimes` in fixed column order."""
    main_cmds = [c for c in target.commands if not c.startswith(_PROBE_PREFIX)]
    assert main_cmds == [_EXPECTED_COLLECT], main_cmds
    assert "ps -eo pid,pcpu,pmem,etimes,comm" in main_cmds[0]


# --------------------------------------------------------------------------- #
# Artefact anchor: cpu_pct high but etimes < min_etimes → zero findings
# --------------------------------------------------------------------------- #


async def test_young_high_cpu_process_yields_no_finding() -> None:
    # A just-spawned journalctl read ~100% CPU (cputime/realtime artefact) but
    # has only lived 1s (etimes==1 < default min_etimes==10), so the age gate
    # suppresses it entirely — the report is NOT warned/critical.
    target, result = await _run("33948 100 0.5 1 journalctl\n")

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert result.output == {
        "rows": [
            {
                "pid": "33948",
                "cpu_pct": "100",
                "mem_pct": "0.5",
                "etimes": "1",
                "comm": "journalctl",
            },
        ],
    }
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Sustained anchor: etimes >= min_etimes gates the cpu thresholds
# --------------------------------------------------------------------------- #


async def test_long_lived_high_cpu_process_is_critical() -> None:
    # mysqld has lived 86400s (>= min_etimes 10) at 97.5% CPU (>= 90) → critical.
    target, result = await _run("4242 97.5 12.3 86400 mysqld\n")

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "Process mysqld (pid 4242) is using 97.5% CPU"),
    ]


async def test_long_lived_elevated_cpu_process_is_warning() -> None:
    # python3 has lived 3600s (>= min_etimes 10) at 75.0% CPU (in [70, 90)) →
    # warning (not critical).
    target, result = await _run("4310 75.0 3.1 3600 python3\n")

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("warning", "Process python3 (pid 4310) CPU usage elevated at 75.0%"),
    ]


# --------------------------------------------------------------------------- #
# Fail-loud guard: empty output (non-procps `ps` errored, its non-zero exit
# masked by `| head`) must NOT be a silent `status=ok` 0-finding "all clear".
# `output_schema` `minItems: 1` collapses the empty row set to `status=exception`
# (a working procps host always has >=1 process).
# --------------------------------------------------------------------------- #


async def test_empty_collection_is_exception_not_silent_ok() -> None:
    # A non-procps `ps -eo ...,etimes,...` errors and prints nothing; `| head`
    # masks the non-zero exit, so the runner sees empty stdout. Without the
    # `minItems: 1` guard this would be `status=ok` with 0 findings (false "all
    # clear"); with it the empty row set fails output_schema validation.
    _target, result = await _run("")

    assert result.status == "exception"
    assert result.findings == []
