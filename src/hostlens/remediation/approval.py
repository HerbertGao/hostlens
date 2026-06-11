"""`ApprovalGate` — the human-in-the-loop write gate for `hostlens fix`
(M9 P2).

This gate is **strictly separate** from `ToolContext.ApprovalService` (which
stays a permanent `NoopApprovalService` so agent-surface handlers never
trigger approval — M9 invariant 3). `ApprovalGate` lives in `remediation/`,
is used by the Executor/CLI only, and is the extension point for P3 remote
(Lark) approval.

Approval rules (spec §需求:ApprovalGate …):

- `--yes` covers **only** the ordinary `y/N`; it **never** covers the
  high-risk second confirmation phrase (the high-risk double-confirm is a gate
  that `--yes` cannot bypass — symmetric across interactive / non-interactive).
- Interactive (TTY): `y/N`; a plan containing a `risk_level == "high"` step
  additionally requires a **second** confirmation — the user must type the
  confirmation phrase (the plan's `finding_id`, per design Open Questions:
  forces the operator to read which plan they are approving).
- With `--yes`: an ordinary plan skips `y/N`; a high-risk plan still walks the
  second phrase.
- Non-interactive (no TTY): missing `--yes` → reject (exit 1, never silently
  execute). With `--yes` an ordinary plan is approved, but a high-risk plan is
  **rejected even with `--yes`** (cannot type the phrase) — forcing the most
  dangerous class to have a human present.

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

    `reason` is a short machine-stable token (e.g. `non_interactive_no_yes`,
    `high_risk_non_interactive`, `user_declined`, `phrase_mismatch`) the CLI
    can render after the `approval-rejected:` prefix.
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
        `ApprovalRejected` otherwise. Never executes anything itself."""
        high_risk = any(step.risk_level == "high" for step in plan.steps)
        interactive = self.is_tty()

        if not interactive:
            if not self.assume_yes:
                raise ApprovalRejected(
                    "non_interactive_no_yes",
                    "non-interactive session requires --yes to execute",
                )
            if high_risk:
                # --yes never bypasses high-risk double-confirm; there is no
                # way to read the phrase without a TTY → reject.
                raise ApprovalRejected(
                    "high_risk_non_interactive",
                    "plan contains a high-risk step; high-risk requires interactive "
                    "double-confirmation and cannot be approved with --yes in a "
                    "non-interactive session",
                )
            return

        # Interactive. --yes skips the first y/N; otherwise ask it.
        if not self.assume_yes:
            answer = self.prompt("Execute this remediation plan? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise ApprovalRejected("user_declined", "user declined the plan")

        # High-risk double-confirmation is never skipped by --yes.
        if high_risk:
            phrase = self.prompt(
                f"High-risk plan. Type the finding id to confirm ({plan.finding_id!r}): "
            ).strip()
            if phrase != plan.finding_id:
                raise ApprovalRejected(
                    "phrase_mismatch",
                    "confirmation phrase did not match the plan finding id",
                )
