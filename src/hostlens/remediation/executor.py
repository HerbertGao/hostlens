"""Deterministic remediation `Executor` — sequential precheck/forward/verify
with reverse-order rollback (M9 P2).

The `Executor` consumes an already-validated `RemediationPlan` and a live
`ExecutionTarget`, running `steps` strictly in order. It is **deterministic**
(no LLM, no randomness), holds **no `LLMBackend`**, is **not** registered in
any `ToolRegistry` and is **not** projected to any surface adapter — it is a
CLI-triggered write subsystem (analogous to Notifier; M9 invariant 2).

Execution semantics (spec §需求:Executor 必须确定性顺序执行 …):

- A phase (precheck / forward / verify / rollback) **succeeds iff its
  `ExecResult.exit_code == 0`**. The decision branch only writes
  `exit_code != 0`; `None` (no OS exit code — timeout *or* dropped
  connection) is automatically not-successful via `None != 0`. `timed_out`
  is **not** part of the success decision (already covered by
  `exit_code is None`); it is recorded only to let the audit consumer tell
  "timed out" from "connection dropped".
- `ExecutionTarget.exec` raises `TargetError` on transport-level failure
  (auth / connection / SFTP). Every phase's `exec` call is wrapped: a raised
  `TargetError` is caught, treated as "phase did not advance", recorded, and
  **never** allowed to surface as a traceback. Any **other** exec exception
  (`OSError` from fd exhaustion, decode errors, a target-library bug) is
  absorbed the same way — exec never raises out of a phase. Only
  `asyncio.CancelledError` / `KeyboardInterrupt` (`BaseException`) propagate.

This module also defines the shared execution-result model consumed by the
audit subsystem, so Executor output and audit input stay type-identical.

dry-run vs real execution share this **same** orchestration; they diverge
only at the injected `CommandRunner` (dry-run records the command without
calling the real `exec`). The real `target.exec` wiring is task 5 — this
module ships the dry-run runner and the runner Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from hostlens.core.exceptions import TargetError
from hostlens.remediation.models import RemediationPlan, RemediationStep
from hostlens.targets.base import ExecResult, ExecutionTarget

__all__ = [
    "CommandRunner",
    "DryRunCommandRunner",
    "Executor",
    "PhaseOutcome",
    "PlanExecutionResult",
    "RealCommandRunner",
    "RollbackOutcome",
    "StepOutcome",
    "StepStatus",
]


# A phase of a step. `rollback` is included so a single enum classifies every
# command we run; the failure three-state (precheck-blocked / forward-failed /
# verify-failed) lives on `StepStatus`.
Phase = Literal["precheck", "forward", "verify", "rollback"]

StepStatus = Literal[
    "succeeded",
    "precheck-blocked",
    "forward-failed",
    "verify-failed",
    "skipped",
]
"""Per-step terminal classification.

- `succeeded`: precheck (if any) / forward / verify all `exit_code == 0`.
- `precheck-blocked`: precheck did not advance — forward never ran.
- `forward-failed`: forward did not advance (`exit_code` may be null / timed
  out / transport error). forward did NOT change state → this step is not
  rolled back.
- `verify-failed`: forward advanced but verify did not — forward already
  changed state → this step IS rolled back.
- `skipped`: a later step that was never reached because an earlier step
  aborted the plan.
"""

RollbackStatus = Literal["rolled-back", "rollback-failed", "rollback-unavailable"]


@runtime_checkable
class CommandRunner(Protocol):
    """Injectable command-execution boundary.

    The dry-run and real paths share the whole `Executor` orchestration and
    diverge only here: `DryRunCommandRunner` records the command without
    touching the target, while `RealCommandRunner` (task 5) calls the live
    `ExecutionTarget.exec`.

    A runner returns an `ExecResult` on a completed command (success or
    non-zero exit) and **raises `TargetError`** on transport-level failure —
    mirroring `ExecutionTarget.exec`'s own contract so the orchestration's
    catch logic is identical for both runners.
    """

    async def run(self, cmd: str, *, timeout: int) -> ExecResult: ...

    def record(self, phase: Phase, cmd: str) -> None: ...


@dataclass(frozen=True)
class DryRunCommandRunner:
    """`CommandRunner` that records each command and returns a synthetic
    success `ExecResult` without ever calling the real target.

    It mutates the world in **no** way: it appends to an internal log and
    reports `exit_code == 0` so the orchestration walks the full happy path
    (every step "succeeds", no rollback) and emits the complete command
    sequence that a real run *would* execute. Tests asserting "real exec is
    never called in dry-run" inject a real-runner spy and assert this one is
    used instead.
    """

    recorded: list[tuple[Phase, str]] = field(default_factory=list)

    async def run(self, cmd: str, *, timeout: int) -> ExecResult:
        # Phase is attached by the Executor via `record`; `run` only needs to
        # report a deterministic success so the happy path is fully walked.
        return ExecResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    def record(self, phase: Phase, cmd: str) -> None:
        self.recorded.append((phase, cmd))


@dataclass(frozen=True)
class RealCommandRunner:
    """`CommandRunner` backed by a live `ExecutionTarget`.

    Per task 5 (dry-run-first discipline) this is the real-exec boundary; the
    orchestration is identical to the dry-run path. It is defined here (not
    wired into the default `Executor` construction) so the type exists and is
    type-checked; the CLI selects it for non-dry-run runs in group B/C.
    """

    target: ExecutionTarget

    async def run(self, cmd: str, *, timeout: int) -> ExecResult:
        return await self.target.exec(cmd, timeout=timeout)

    def record(self, phase: Phase, cmd: str) -> None:
        # Real execution does not need a recorded command log; the audit
        # subsystem captures real outcomes. No-op keeps the Executor from
        # branching on the concrete runner type.
        pass


@dataclass(frozen=True)
class PhaseOutcome:
    """Outcome of one phase command. `result` is the `ExecResult` if `exec`
    completed; `transport_error` is a short string if `exec` raised
    `TargetError` (transport-level failure). Exactly one is non-None."""

    phase: Phase
    cmd: str
    result: ExecResult | None
    transport_error: str | None

    @property
    def advanced(self) -> bool:
        """True iff this phase succeeded — `exit_code == 0`. A transport error
        (result is None) or any non-zero / `None` exit code is not-advanced."""
        return self.result is not None and self.result.exit_code == 0


@dataclass(frozen=True)
class StepOutcome:
    """Terminal classification of one step plus every phase it actually ran."""

    index: int
    description: str
    risk_level: str
    status: StepStatus
    phases: list[PhaseOutcome]


@dataclass(frozen=True)
class RollbackOutcome:
    """Result of attempting to roll back one previously-advanced step."""

    index: int
    description: str
    status: RollbackStatus
    phase: PhaseOutcome | None


@dataclass(frozen=True)
class PlanExecutionResult:
    """Full result of executing a plan: per-step outcomes, rollback outcomes
    (reverse order), and whether the plan succeeded overall.

    Consumed by the audit subsystem (result record) — the audit module reads
    these fields directly so Executor output and audit input never drift."""

    steps: list[StepOutcome]
    rollbacks: list[RollbackOutcome]
    succeeded: bool
    rollback_complete: bool


class Executor:
    """Deterministic sequential remediation executor.

    Depends only on a `RemediationPlan` and a `CommandRunner` (the injected
    exec boundary). Holds no backend, is not in any registry. Construct with a
    `DryRunCommandRunner` for dry-run; with a `RealCommandRunner` for real
    execution — the orchestration is identical.
    """

    def __init__(self, plan: RemediationPlan, runner: CommandRunner) -> None:
        self._plan = plan
        self._runner = runner

    async def execute(self, *, per_step_timeout: int = 30) -> PlanExecutionResult:
        """Run every step in order; on the first step that does not advance,
        stop and roll back in reverse order. Returns a full result; never
        raises for command/transport failures (those become recorded
        outcomes)."""
        steps = self._plan.steps
        outcomes: list[StepOutcome] = []
        # Index of the step whose `forward` already changed remote state and
        # therefore must itself be rolled back. -1 means "no forward advanced".
        last_forwarded = -1
        failed_at: int | None = None
        rollback_includes_self = False

        for index, step in enumerate(steps):
            outcome = await self._run_step(index, step, per_step_timeout)
            outcomes.append(outcome)
            if outcome.status == "succeeded":
                last_forwarded = index
                continue
            # Step did not advance — stop the plan.
            failed_at = index
            rollback_includes_self = outcome.status == "verify-failed"
            break

        # Mark any steps never reached as skipped.
        if failed_at is not None:
            for skipped_index in range(failed_at + 1, len(steps)):
                outcomes.append(
                    StepOutcome(
                        index=skipped_index,
                        description=steps[skipped_index].description,
                        risk_level=steps[skipped_index].risk_level,
                        status="skipped",
                        phases=[],
                    )
                )

        if failed_at is None:
            return PlanExecutionResult(
                steps=outcomes, rollbacks=[], succeeded=True, rollback_complete=True
            )

        rollback_boundary = failed_at if rollback_includes_self else last_forwarded
        rollbacks = await self._rollback(steps, rollback_boundary, per_step_timeout)
        rollback_complete = all(r.status != "rollback-failed" for r in rollbacks)
        return PlanExecutionResult(
            steps=outcomes,
            rollbacks=rollbacks,
            succeeded=False,
            rollback_complete=rollback_complete,
        )

    async def _run_step(self, index: int, step: RemediationStep, timeout: int) -> StepOutcome:
        phases: list[PhaseOutcome] = []

        if step.precheck_cmd is not None:
            precheck = await self._run_phase("precheck", step.precheck_cmd, timeout)
            phases.append(precheck)
            if not precheck.advanced:
                return self._step_outcome(index, step, "precheck-blocked", phases)

        forward = await self._run_phase("forward", step.forward_cmd, timeout)
        phases.append(forward)
        if not forward.advanced:
            return self._step_outcome(index, step, "forward-failed", phases)

        verify = await self._run_phase("verify", step.verify_cmd, timeout)
        phases.append(verify)
        if not verify.advanced:
            return self._step_outcome(index, step, "verify-failed", phases)

        return self._step_outcome(index, step, "succeeded", phases)

    async def _rollback(
        self, steps: list[RemediationStep], boundary: int, timeout: int
    ) -> list[RollbackOutcome]:
        """Reverse-order rollback of steps 0..boundary (inclusive). Each step:
        `rollback_cmd is None` → `rollback-unavailable` and continue; a failing
        rollback (including exec raising) → `rollback-failed` and continue."""
        rollbacks: list[RollbackOutcome] = []
        for index in range(boundary, -1, -1):
            step = steps[index]
            if step.rollback_cmd is None:
                rollbacks.append(
                    RollbackOutcome(
                        index=index,
                        description=step.description,
                        status="rollback-unavailable",
                        phase=None,
                    )
                )
                continue
            phase = await self._run_phase("rollback", step.rollback_cmd, timeout)
            status: RollbackStatus = "rolled-back" if phase.advanced else "rollback-failed"
            rollbacks.append(
                RollbackOutcome(
                    index=index, description=step.description, status=status, phase=phase
                )
            )
        return rollbacks

    async def _run_phase(self, phase: Phase, cmd: str, timeout: int) -> PhaseOutcome:
        self._runner.record(phase, cmd)
        try:
            result = await self._runner.run(cmd, timeout=timeout)
        except TargetError as exc:
            return PhaseOutcome(
                phase=phase, cmd=cmd, result=None, transport_error=_transport_summary(exc)
            )
        except Exception as exc:
            # `ExecutionTarget.exec` contractually raises only `TargetError`,
            # but `LocalTarget.exec` (and future targets) can leak `OSError`
            # (fd / memory exhaustion), decode errors, or a library bug. This
            # is a write-path subsystem serving forward/verify AND reverse
            # rollback — any exec exception escaping here would print a Rich
            # traceback, strand a written intent without a result, or abort a
            # mid-flight reverse rollback. Absorb every non-exec-fatal
            # exception as a recorded "phase did not advance" outcome.
            # `except Exception` deliberately does NOT catch
            # `asyncio.CancelledError` / `KeyboardInterrupt` (both
            # `BaseException`) — those must propagate for cancellation.
            return PhaseOutcome(
                phase=phase,
                cmd=cmd,
                result=None,
                transport_error=f"{type(exc).__name__}: {exc}",
            )
        return PhaseOutcome(phase=phase, cmd=cmd, result=result, transport_error=None)

    @staticmethod
    def _step_outcome(
        index: int, step: RemediationStep, status: StepStatus, phases: list[PhaseOutcome]
    ) -> StepOutcome:
        return StepOutcome(
            index=index,
            description=step.description,
            risk_level=step.risk_level,
            status=status,
            phases=phases,
        )


def _transport_summary(exc: TargetError) -> str:
    """A short, non-traceback summary of a transport-level `TargetError` for
    audit. Includes the structured `kind`; never embeds raw secrets (extras
    are not interpolated)."""
    return f"transport_error:{exc.kind}"
