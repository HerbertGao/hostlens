"""`ApprovalGate` — the human-in-the-loop write gate for `hostlens fix`.

This gate is **strictly separate** from `ToolContext.ApprovalService` (which
stays a permanent `NoopApprovalService` so agent-surface handlers never
trigger approval — M9 invariant 3). `ApprovalGate` lives in `remediation/`
and is used by the Executor/CLI only.

**The gate only ever authorizes all-`low`-risk plans.** A plan containing any
`risk_level ∈ {"medium","high"}` step is propose-only and is diverted to a
runbook by `hostlens fix` **before** reaching this gate (risk-tiered
execution) — so the gate never sees an elevated plan and carries no high-risk
double-confirmation logic. high-risk's safety guarantee is the stronger "never
executed by the tool", not "double-confirm then execute".

Approval rules (spec §需求:ApprovalGate 必须交互确认或 --yes 且与 ToolContext 分离):

- Interactive (TTY): `y/N`; `--yes` skips the prompt and authorizes directly.
- Non-interactive (no TTY): missing `--yes` → reject (exit 1, never silently
  execute); with `--yes` the plan is authorized.

A rejection is signalled by raising `ApprovalRejected`; the CLI maps it to
exit code 1 with a `approval-rejected:` stderr prefix (distinct from
`execution-failed:` so scripts can tell a safety-gate refusal from a runtime
failure).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from hostlens.remediation.models import RemediationPlan

__all__ = ["ApprovalGate", "ApprovalRejected"]


class ApprovalRejected(Exception):  # noqa: N818 - intentional control-flow name (no "Error" suffix)
    """The approval gate refused to authorize execution.

    `reason` is a short machine-stable token (`non_interactive_no_yes` or
    `user_declined`) the CLI can render after the `approval-rejected:` prefix.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ApprovalGate:
    """Resolve whether a plan may execute.

    `prompt` reads a single line (defaults to `input`); `is_tty` reports
    interactivity (defaults to `sys.stdin.isatty`). Both are injectable so the
    gate is fully testable without a real terminal.
    """

    assume_yes: bool
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty()
    prompt: Callable[[str], str] = input

    def authorize(self, plan: RemediationPlan) -> None:
        """Return normally iff execution is authorized; raise
        `ApprovalRejected` otherwise. Never executes anything itself.

        Only all-`low` plans reach here (medium/high diverge to a runbook
        upstream), so there is a single decision: interactive y/N (or `--yes`)
        / non-interactive requires `--yes`.
        """
        interactive = self.is_tty()

        if not interactive:
            if not self.assume_yes:
                raise ApprovalRejected(
                    "non_interactive_no_yes",
                    "non-interactive session requires --yes to execute",
                )
            return

        # Interactive. --yes skips the y/N prompt; otherwise ask it.
        if not self.assume_yes:
            answer = self.prompt("Execute this remediation plan? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise ApprovalRejected("user_declined", "user declined the plan")
