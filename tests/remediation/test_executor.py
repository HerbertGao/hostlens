"""Executable contract checks for the remediation `Executor` (M9 P2, group C).

Each test maps to a `remediation-execution-workflow` spec scenario under
§需求:Executor 必须确定性顺序执行 … / §需求:precheck 失败 … / §需求:任一步未成功
推进必须倒序回滚 …. The orchestration is exercised through an **injected**
`CommandRunner` (`_ScriptedRunner`) that returns scripted `ExecResult`s (or
raises `TargetError`) keyed by `(phase, step_index)`, so every success /
failure / transport-error / timeout branch is driven deterministically without
a real target — and we assert the exact command sequence + rollback boundary
+ three-state classification, never a vacuous "it ran".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from hostlens.core.exceptions import TargetError
from hostlens.remediation.executor import (
    DryRunCommandRunner,
    Executor,
    PlanExecutionResult,
    RealCommandRunner,
)
from hostlens.remediation.models import RemediationPlan, RemediationStep
from hostlens.targets.base import ExecResult

# --------------------------------------------------------------------------- #
# Fixtures: plans + a scripted, fully-deterministic CommandRunner
# --------------------------------------------------------------------------- #


def _step(
    *,
    description: str,
    risk_level: str = "low",
    precheck: str | None = None,
    forward: str,
    verify: str,
    rollback: str | None = "echo rollback",
) -> RemediationStep:
    return RemediationStep(
        description=description,
        precheck_cmd=precheck,
        forward_cmd=forward,
        rollback_cmd=rollback,
        verify_cmd=verify,
        risk_level=risk_level,  # type: ignore[arg-type]
    )


def _plan(steps: list[RemediationStep], *, finding_id: str = "f") -> RemediationPlan:
    return RemediationPlan(
        finding_id=finding_id,
        target_name="prod-web-01",
        rationale="r",
        steps=steps,
        estimated_duration_seconds=1,
    )


def _ok(exit_code: int = 0, *, timed_out: bool = False) -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        timed_out=timed_out,
    )


@dataclass
class _ScriptedRunner:
    """`CommandRunner` keyed by command string: any command in `outcomes`
    returns/raises its scripted outcome, every other command returns a
    success `ExecResult`. Records `(cmd, timeout)` of every call in order so
    tests assert the exact command sequence, per-call timeout, AND that a
    command (e.g. a blocked step's forward) was never invoked. Keying by
    command avoids brittle positional script indexing."""

    outcomes: dict[str, ExecResult | TargetError] = field(default_factory=dict)
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def run(self, cmd: str, *, timeout: int) -> ExecResult:
        self.calls.append((cmd, timeout))
        item = self.outcomes.get(cmd, _ok())
        if isinstance(item, TargetError):
            raise item
        return item

    def record(self, phase: str, cmd: str) -> None:
        pass


# --------------------------------------------------------------------------- #
# 6.1 全成功顺序执行
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_all_steps_succeed_in_order_no_rollback() -> None:
    plan = _plan(
        [
            _step(description="s0", precheck="pc0", forward="fw0", verify="vf0"),
            _step(description="s1", precheck="pc1", forward="fw1", verify="vf1"),
        ]
    )
    runner = _ScriptedRunner()  # everything succeeds
    result = await Executor(plan, runner).execute()

    assert result.succeeded is True
    assert result.rollback_complete is True
    assert result.rollbacks == []
    assert [s.status for s in result.steps] == ["succeeded", "succeeded"]
    # Exact ordered command sequence: each step precheck→forward→verify.
    assert [c[0] for c in runner.calls] == ["pc0", "fw0", "vf0", "pc1", "fw1", "vf1"]


@pytest.mark.asyncio
async def test_per_step_timeout_passed_through_to_runner() -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0")])
    runner = _ScriptedRunner()
    await Executor(plan, runner).execute(per_step_timeout=17)
    assert {c[1] for c in runner.calls} == {17}


# --------------------------------------------------------------------------- #
# 6.1 precheck 失败中止不碰 forward + precheck-blocked
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_precheck_failure_aborts_without_forward_and_rolls_back_prior() -> None:
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(description="s1", precheck="pc1", forward="fw1", verify="vf1"),
        ]
    )
    # s0 all-ok, s1 precheck fails -> forward of s1 never runs.
    runner = _ScriptedRunner(outcomes={"pc1": _ok(exit_code=1)})
    result = await Executor(plan, runner).execute()

    assert result.succeeded is False
    assert result.steps[1].status == "precheck-blocked"
    # s1's forward_cmd must NOT appear in the call sequence.
    assert "fw1" not in [c[0] for c in runner.calls]
    # Prior advanced step (s0) is rolled back.
    assert [(r.index, r.status) for r in result.rollbacks] == [(0, "rolled-back")]
    assert "rb0" in [c[0] for c in runner.calls]


# --------------------------------------------------------------------------- #
# 6.1 forward-failed 倒序 rollback(不含本步)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_forward_failure_rolls_back_prior_reverse_not_self() -> None:
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(description="s1", forward="fw1", verify="vf1", rollback="rb1"),
            _step(description="s2", forward="fw2", verify="vf2", rollback="rb2"),
        ]
    )
    # s0 ok, s1 ok, s2 forward fails.
    runner = _ScriptedRunner(outcomes={"fw2": _ok(exit_code=2)})
    result = await Executor(plan, runner).execute()

    assert result.steps[2].status == "forward-failed"
    # Reverse-order rollback of s1 then s0; NOT s2 (its forward never completed).
    assert [(r.index, r.status) for r in result.rollbacks] == [
        (1, "rolled-back"),
        (0, "rolled-back"),
    ]
    rollback_cmds = [c[0] for c in runner.calls if c[0].startswith("rb")]
    assert rollback_cmds == ["rb1", "rb0"]
    assert "rb2" not in rollback_cmds


# --------------------------------------------------------------------------- #
# 6.1 verify-failed 倒序含本步
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verify_failure_rolls_back_including_self() -> None:
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(description="s1", forward="fw1", verify="vf1", rollback="rb1"),
        ]
    )
    # s0 ok; s1 forward ok, verify fails.
    runner = _ScriptedRunner(outcomes={"vf1": _ok(exit_code=1)})
    result = await Executor(plan, runner).execute()

    assert result.steps[1].status == "verify-failed"
    # forward of s1 already changed state -> s1 itself IS rolled back, then s0.
    assert [(r.index, r.status) for r in result.rollbacks] == [
        (1, "rolled-back"),
        (0, "rolled-back"),
    ]
    assert [c[0] for c in runner.calls if c[0].startswith("rb")] == ["rb1", "rb0"]


# --------------------------------------------------------------------------- #
# 6.1 rollback-unavailable 不中断
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rollback_unavailable_high_risk_does_not_interrupt() -> None:
    # P1a: rollback_cmd=None requires high-risk; high-risk requires precheck.
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(
                description="s1",
                risk_level="high",
                precheck="pc1",
                forward="fw1",
                verify="vf1",
                rollback=None,
            ),
            _step(description="s2", forward="fw2", verify="vf2", rollback="rb2"),
        ]
    )
    # s0 ok; s1 (high) precheck+forward+verify ok; s2 forward fails.
    runner = _ScriptedRunner(outcomes={"fw2": _ok(exit_code=1)})
    result = await Executor(plan, runner).execute()

    assert result.steps[2].status == "forward-failed"
    # Reverse: s1 has no rollback_cmd -> rollback-unavailable (continue), then s0.
    assert [(r.index, r.status) for r in result.rollbacks] == [
        (1, "rollback-unavailable"),
        (0, "rolled-back"),
    ]
    # rollback-unavailable does not abort: s0's rollback still ran.
    assert "rb0" in [c[0] for c in runner.calls]
    # rollback_complete stays True (unavailable is not a failed rollback).
    assert result.rollback_complete is True


# --------------------------------------------------------------------------- #
# 6.1 单 rollback 失败继续倒序
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_rollback_failure_continues_reverse() -> None:
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(description="s1", forward="fw1", verify="vf1", rollback="rb1"),
            _step(description="s2", forward="fw2", verify="vf2", rollback="rb2"),
        ]
    )
    # s0 ok, s1 ok, s2 forward fails; rollback rb1 fails, rb0 ok.
    runner = _ScriptedRunner(outcomes={"fw2": _ok(exit_code=5), "rb1": _ok(exit_code=1)})
    result = await Executor(plan, runner).execute()

    assert [(r.index, r.status) for r in result.rollbacks] == [
        (1, "rollback-failed"),
        (0, "rolled-back"),
    ]
    # A failed rollback does not abort the remaining reverse walk.
    assert [c[0] for c in runner.calls if c[0].startswith("rb")] == ["rb1", "rb0"]
    # CLI-facing flag: execution failed AND rollback incomplete.
    assert result.succeeded is False
    assert result.rollback_complete is False


# --------------------------------------------------------------------------- #
# 6.1 「已成功推进」= precheck/forward/verify 三阶段 exit_code==0
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_advancement_requires_all_three_phases_zero() -> None:
    # A step whose precheck is non-zero is NOT advanced (forward never runs).
    plan = _plan([_step(description="s0", precheck="pc0", forward="fw0", verify="vf0")])
    runner = _ScriptedRunner(outcomes={"pc0": _ok(exit_code=1)})
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "precheck-blocked"
    assert [c[0] for c in runner.calls] == ["pc0"]  # only precheck ran


# --------------------------------------------------------------------------- #
# 6.2 ExecResult / 阶段成功判定
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nonzero_exit_is_failure() -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0")])
    runner = _ScriptedRunner(outcomes={"fw0": _ok(exit_code=7)})
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "forward-failed"
    assert result.steps[0].phases[-1].result is not None
    assert result.steps[0].phases[-1].result.exit_code == 7
    # First-step forward failure: nothing advanced -> empty rollback set,
    # vacuously complete (all([]) is True).
    assert result.rollbacks == []
    assert result.rollback_complete is True


@pytest.mark.asyncio
async def test_exit_code_none_dropped_connection_is_failure() -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0")])
    # exit_code=None, timed_out=False -> dropped connection.
    dropped = _ok().model_copy(update={"exit_code": None})
    runner = _ScriptedRunner(outcomes={"fw0": dropped})
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "forward-failed"
    phase = result.steps[0].phases[-1]
    assert phase.result is not None
    assert phase.result.exit_code is None
    assert phase.result.timed_out is False
    assert result.rollbacks == []
    assert result.rollback_complete is True


@pytest.mark.asyncio
async def test_timed_out_is_failure_via_exit_code_none_not_misjudged() -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0")])
    # timed_out=True requires exit_code=None (ExecResult invariant).
    timed = ExecResult(exit_code=None, stdout="", stderr="", duration_seconds=0.0, timed_out=True)
    runner = _ScriptedRunner(outcomes={"fw0": timed})
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "forward-failed"
    phase = result.steps[0].phases[-1]
    assert phase.result is not None
    # timed_out distinguishes timeout from dropped connection.
    assert phase.result.timed_out is True
    assert phase.result.exit_code is None
    assert result.rollbacks == []
    assert result.rollback_complete is True


@pytest.mark.asyncio
async def test_exec_raising_target_error_caught_as_failure_no_traceback() -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0", rollback="rb0")])
    # forward raises TargetError (transport-level failure).
    runner = _ScriptedRunner(
        outcomes={"fw0": TargetError(kind="ssh_connection_lost", target="prod-web-01")}
    )
    # Must NOT raise — the TargetError is caught and recorded.
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "forward-failed"
    phase = result.steps[0].phases[-1]
    assert phase.result is None
    assert phase.transport_error == "transport_error:ssh_connection_lost"
    assert result.rollbacks == []
    assert result.rollback_complete is True


# --------------------------------------------------------------------------- #
# F1: a NON-TargetError exec exception is absorbed (executor never raises)
# --------------------------------------------------------------------------- #


@dataclass
class _RaisingRunner:
    """`CommandRunner` that raises an arbitrary (non-TargetError) exception for
    one command and otherwise succeeds. Models `LocalTarget.exec` leaking
    `OSError` (fd exhaustion) or any target-library bug."""

    failing_cmd: str
    exc: BaseException
    calls: list[str] = field(default_factory=list)

    async def run(self, cmd: str, *, timeout: int) -> ExecResult:
        self.calls.append(cmd)
        if cmd == self.failing_cmd:
            raise self.exc
        return _ok()

    def record(self, phase: str, cmd: str) -> None:
        pass


@pytest.mark.parametrize(
    "exc",
    [OSError(24, "Too many open files"), RuntimeError("library bug")],
)
@pytest.mark.asyncio
async def test_forward_raising_non_target_error_absorbed_no_raise(
    exc: BaseException,
) -> None:
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0", rollback="rb0")])
    runner = _RaisingRunner(failing_cmd="fw0", exc=exc)
    # Must NOT raise — a non-TargetError exec exception is caught and recorded.
    result = await Executor(plan, runner).execute()
    assert result.steps[0].status == "forward-failed"
    phase = result.steps[0].phases[-1]
    assert phase.result is None
    # transport_error carries the exception type so audit can tell what failed.
    assert phase.transport_error is not None
    assert type(exc).__name__ in phase.transport_error
    # First step failed at forward -> nothing advanced -> empty rollback set.
    assert result.rollbacks == []
    assert result.rollback_complete is True


@pytest.mark.parametrize(
    "exc",
    [asyncio.CancelledError(), KeyboardInterrupt()],
)
@pytest.mark.asyncio
async def test_base_exception_propagates_not_absorbed(exc: BaseException) -> None:
    # The broad `except Exception` deliberately does NOT catch cancellation /
    # interrupt (both BaseException) — they must propagate out of execute() so
    # cooperative cancellation works and is never silently recorded as
    # forward-failed.
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0", rollback="rb0")])
    runner = _RaisingRunner(failing_cmd="fw0", exc=exc)
    with pytest.raises(type(exc)):
        await Executor(plan, runner).execute()


@pytest.mark.asyncio
async def test_rollback_raising_non_target_error_continues_reverse_no_raise() -> None:
    plan = _plan(
        [
            _step(description="s0", forward="fw0", verify="vf0", rollback="rb0"),
            _step(description="s1", forward="fw1", verify="vf1", rollback="rb1"),
            _step(description="s2", forward="fw2", verify="vf2", rollback="rb2"),
        ]
    )

    # s0 + s1 advance; s2 forward fails -> reverse rollback of s1 then s0.
    # rb1 raises a NON-TargetError (e.g. fd exhaustion during rollback) -> the
    # reverse walk must NOT abort: rb0 still runs.
    @dataclass
    class _RollbackRaiser:
        calls: list[str] = field(default_factory=list)

        async def run(self, cmd: str, *, timeout: int) -> ExecResult:
            self.calls.append(cmd)
            if cmd == "fw2":
                return _ok(exit_code=1)
            if cmd == "rb1":
                raise OSError(24, "Too many open files")
            return _ok()

        def record(self, phase: str, cmd: str) -> None:
            pass

    runner = _RollbackRaiser()
    result = await Executor(plan, runner).execute()

    assert result.steps[2].status == "forward-failed"
    # rb1 raised -> rollback-failed, but the reverse walk continued to rb0.
    assert [(r.index, r.status) for r in result.rollbacks] == [
        (1, "rollback-failed"),
        (0, "rolled-back"),
    ]
    assert [c for c in runner.calls if c.startswith("rb")] == ["rb1", "rb0"]
    assert result.rollback_complete is False


# --------------------------------------------------------------------------- #
# Executor independence (spec: no LLM / not in registry)
# --------------------------------------------------------------------------- #


def test_executor_depends_only_on_plan_and_runner() -> None:
    # Constructor signature is exactly (plan, runner) — no backend / registry.
    plan = _plan([_step(description="s0", forward="fw0", verify="vf0")])
    executor = Executor(plan, DryRunCommandRunner())
    assert not hasattr(executor, "backend")
    assert not hasattr(executor, "llm_backend")


# --------------------------------------------------------------------------- #
# 6.7 dry-run shares orchestration: zero real exec
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dry_run_records_full_sequence_without_real_exec() -> None:
    plan = _plan(
        [
            _step(description="s0", precheck="pc0", forward="fw0", verify="vf0"),
            _step(description="s1", forward="fw1", verify="vf1"),
        ]
    )
    runner = DryRunCommandRunner()
    result: PlanExecutionResult = await Executor(plan, runner).execute()
    # Happy path fully walked, every command recorded, nothing rolled back.
    assert result.succeeded is True
    assert [(ph, cmd) for ph, cmd in runner.recorded] == [
        ("precheck", "pc0"),
        ("forward", "fw0"),
        ("verify", "vf0"),
        ("forward", "fw1"),
        ("verify", "vf1"),
    ]


@pytest.mark.asyncio
async def test_real_runner_delegates_to_target_exec() -> None:
    """`RealCommandRunner.run` calls `ExecutionTarget.exec` with the cmd +
    per-call timeout (no rewriting)."""

    @dataclass
    class _SpyTarget:
        name: str = "prod-web-01"
        type: str = "local"
        capabilities: frozenset[object] = field(default_factory=frozenset)
        seen: list[tuple[str, int]] = field(default_factory=list)

        async def exec(
            self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
        ) -> ExecResult:
            self.seen.append((cmd, timeout))
            return _ok()

        async def read_file(self, path: str) -> bytes:
            return b""

    target = _SpyTarget()
    runner = RealCommandRunner(target)  # type: ignore[arg-type]
    out = await runner.run("echo hi", timeout=9)
    assert out.exit_code == 0
    assert target.seen == [("echo hi", 9)]


# --------------------------------------------------------------------------- #
# 6.6 真实执行(local target + 临时目录, 安全可逆)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_real_local_execution_full_success(tmp_path: object) -> None:
    """precheck → forward → verify all run for real against a LocalTarget;
    a reversible mv to a backup file is performed and verified."""
    from pathlib import Path

    from hostlens.targets.local import LocalTarget

    work = Path(str(tmp_path))
    src = work / "config.conf"
    bak = work / "config.conf.bak"
    src.write_text("original")

    plan = _plan(
        [
            _step(
                description="back up config",
                precheck=f"test -f {src}",
                forward=f"mv {src} {bak}",
                verify=f"test -f {bak}",
                rollback=f"mv {bak} {src}",
            )
        ]
    )
    runner = RealCommandRunner(LocalTarget("local"))
    result = await Executor(plan, runner).execute()

    assert result.succeeded is True
    assert result.rollbacks == []
    # Real side effect happened: file was moved to the backup path.
    assert bak.exists() and not src.exists()
    assert bak.read_text() == "original"


@pytest.mark.asyncio
async def test_real_local_execution_verify_failure_triggers_real_rollback(tmp_path: object) -> None:
    """forward really moves the file, verify fails (asserts a path that does
    not exist), and the real rollback mv restores the original state."""
    from pathlib import Path

    from hostlens.targets.local import LocalTarget

    work = Path(str(tmp_path))
    src = work / "config.conf"
    bak = work / "config.conf.bak"
    src.write_text("original")

    plan = _plan(
        [
            _step(
                description="move then verify-fail",
                precheck=f"test -f {src}",
                forward=f"mv {src} {bak}",
                # verify checks for a file that was never created -> fails.
                verify=f"test -f {work / 'never-created'}",
                rollback=f"mv {bak} {src}",
            )
        ]
    )
    runner = RealCommandRunner(LocalTarget("local"))
    result = await Executor(plan, runner).execute()

    assert result.succeeded is False
    assert result.steps[0].status == "verify-failed"
    # verify-failed includes self -> rollback ran and restored the source.
    assert [(r.index, r.status) for r in result.rollbacks] == [(0, "rolled-back")]
    assert result.rollback_complete is True
    assert src.exists() and not bak.exists()
    assert src.read_text() == "original"


@pytest.mark.asyncio
async def test_real_local_execution_two_phase_audit(tmp_path: object) -> None:
    """A real LocalTarget run writes a two-phase intent+result audit record
    reflecting the real outcomes."""
    import json
    from pathlib import Path

    from hostlens.remediation.audit import AuditLog
    from hostlens.targets.local import LocalTarget

    work = Path(str(tmp_path))
    audit_file = work / "audit.log"
    flag = work / "applied"

    plan = _plan(
        [
            _step(
                description="touch a flag file",
                forward=f"touch {flag}",
                verify=f"test -f {flag}",
                rollback=f"rm -f {flag}",
            )
        ]
    )
    audit = AuditLog(path=audit_file)
    audit.precheck_writable()
    audit.write_intent(plan)
    runner = RealCommandRunner(LocalTarget("local"))
    result = await Executor(plan, runner).execute()
    audit.write_result(plan, result)

    assert result.succeeded is True
    assert flag.exists()  # real side effect
    lines = [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]
    assert [line["type"] for line in lines] == ["intent", "result"]
    assert lines[1]["succeeded"] is True
    assert lines[1]["steps"][0]["status"] == "succeeded"
