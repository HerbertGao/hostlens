"""Executable contract checks for `ApprovalGate` (M9 risk-tiered execution).

Maps to spec §需求:ApprovalGate 必须交互确认或 --yes (且与 ToolContext 分离). The gate
only ever authorizes all-`low` plans (medium/high diverge to a runbook upstream
in `hostlens fix`), so it carries no high-risk double-confirmation. Each path is
driven with injected `is_tty` / `prompt` callables: non-interactive refusal, the
ordinary y/N (and `--yes` skip), and the rejection-reason tokens the CLI maps to
the `approval-rejected:` prefix.
"""

from __future__ import annotations

import pytest

from hostlens.remediation.approval import ApprovalGate, ApprovalRejected
from hostlens.remediation.models import RemediationPlan, RemediationStep


def _step() -> RemediationStep:
    return RemediationStep(
        description="d",
        precheck_cmd=None,
        forward_cmd="fw",
        rollback_cmd="rb",
        verify_cmd="vf",
        risk_level="low",
    )


def _plan(*, finding_id: str = "disk-full") -> RemediationPlan:
    return RemediationPlan(
        finding_id=finding_id,
        target_name="t",
        rationale="r",
        steps=[_step()],
        estimated_duration_seconds=1,
    )


class _Prompter:
    """Records prompts and replays scripted answers in order."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, message: str) -> str:
        self.prompts.append(message)
        if not self._answers:
            raise AssertionError("prompt called more times than scripted answers")
        return self._answers.pop(0)


def _never_prompt(message: str) -> str:
    raise AssertionError(f"prompt must not be called; got: {message!r}")


# --------------------------------------------------------------------------- #
# Non-interactive
# --------------------------------------------------------------------------- #


def test_non_interactive_without_yes_rejected() -> None:
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: False, prompt=_never_prompt)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(_plan())
    assert exc.value.reason == "non_interactive_no_yes"


def test_non_interactive_with_yes_ordinary_plan_authorized() -> None:
    gate = ApprovalGate(assume_yes=True, is_tty=lambda: False, prompt=_never_prompt)
    # Returns normally (no exception) — and never prompts in a non-TTY.
    gate.authorize(_plan())


# --------------------------------------------------------------------------- #
# Interactive — ordinary y/N
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_interactive_ordinary_yes_authorizes(answer: str) -> None:
    prompter = _Prompter([answer])
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: True, prompt=prompter)
    gate.authorize(_plan())
    assert len(prompter.prompts) == 1  # only the y/N prompt


@pytest.mark.parametrize("answer", ["n", "N", "", "no", "maybe"])
def test_interactive_ordinary_decline_rejected(answer: str) -> None:
    prompter = _Prompter([answer])
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: True, prompt=prompter)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(_plan())
    assert exc.value.reason == "user_declined"


def test_interactive_with_yes_ordinary_skips_prompt() -> None:
    gate = ApprovalGate(assume_yes=True, is_tty=lambda: True, prompt=_never_prompt)
    gate.authorize(_plan())  # --yes skips y/N; low-only gate has no second prompt


# --------------------------------------------------------------------------- #
# rejection vs execution-failure are distinguishable (token / type)
# --------------------------------------------------------------------------- #


def test_rejection_carries_machine_stable_reason_token() -> None:
    # The CLI renders `approval-rejected: <reason>` — the reason must be a
    # stable token (not free text) so scripts can branch on it.
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: False, prompt=_never_prompt)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(_plan())
    assert exc.value.reason in {"non_interactive_no_yes", "user_declined"}
    # ApprovalRejected is its own type, distinct from any execution error path.
    assert isinstance(exc.value, ApprovalRejected)


# --------------------------------------------------------------------------- #
# ToolContext.ApprovalService stays a permanent Noop (strict separation)
# --------------------------------------------------------------------------- #


async def test_toolcontext_approval_service_is_still_noop() -> None:
    from hostlens.core.exceptions import ToolPolicyViolation
    from hostlens.tools.base import NoopApprovalService

    service = NoopApprovalService()
    # The agent-surface approval service permanently refuses (raises a policy
    # violation rather than ever granting) — real approval lives only in
    # remediation/ApprovalGate, never via ToolContext.
    with pytest.raises(ToolPolicyViolation):
        await service.request_approval("anything", "because")
