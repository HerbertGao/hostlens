"""Executable contract checks for `ApprovalGate` (M9 P2, group C).

Maps to spec §需求:ApprovalGate 必须交互确认或 --yes, high-risk 强制人眼在场, 且与
ToolContext 分离. Every safety-gate path is driven with injected `is_tty` /
`prompt` callables so the test really exercises the branch (no real TTY, no
vacuous asserts): non-interactive refusal, the `--yes`-cannot-bypass-high-risk
gate (interactive AND non-interactive), the ordinary y/N, the high-risk second
confirmation phrase, and the rejection-reason tokens the CLI maps to the
`approval-rejected:` prefix.
"""

from __future__ import annotations

import pytest

from hostlens.remediation.approval import ApprovalGate, ApprovalRejected
from hostlens.remediation.models import RemediationPlan, RemediationStep


def _step(*, risk_level: str = "low", rollback: str | None = "rb") -> RemediationStep:
    return RemediationStep(
        description="d",
        precheck_cmd="pc" if risk_level == "high" else None,
        forward_cmd="fw",
        rollback_cmd=rollback,
        verify_cmd="vf",
        risk_level=risk_level,  # type: ignore[arg-type]
    )


def _plan(*, high: bool = False, finding_id: str = "disk-full") -> RemediationPlan:
    steps = [_step()]
    if high:
        steps.append(_step(risk_level="high"))
    return RemediationPlan(
        finding_id=finding_id,
        target_name="t",
        rationale="r",
        steps=steps,
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


def test_non_interactive_with_yes_high_risk_rejected() -> None:
    # --yes does NOT suffice for a high-risk plan in a non-interactive session:
    # the phrase cannot be typed, so it must be refused (exit 1 at the CLI).
    gate = ApprovalGate(assume_yes=True, is_tty=lambda: False, prompt=_never_prompt)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(_plan(high=True))
    assert exc.value.reason == "high_risk_non_interactive"


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
    gate.authorize(_plan())  # --yes skips y/N; no high-risk -> no second prompt


# --------------------------------------------------------------------------- #
# Interactive — high-risk double confirmation
# --------------------------------------------------------------------------- #


def test_interactive_high_risk_double_confirm_both_pass() -> None:
    plan = _plan(high=True, finding_id="disk-full")
    # First y/N, then the finding-id phrase.
    prompter = _Prompter(["y", "disk-full"])
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: True, prompt=prompter)
    gate.authorize(plan)
    assert len(prompter.prompts) == 2


def test_interactive_high_risk_phrase_mismatch_rejected() -> None:
    plan = _plan(high=True, finding_id="disk-full")
    prompter = _Prompter(["y", "wrong-phrase"])
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: True, prompt=prompter)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(plan)
    assert exc.value.reason == "phrase_mismatch"


def test_interactive_high_risk_first_decline_short_circuits() -> None:
    # Declining the first y/N must reject before the phrase is ever asked.
    plan = _plan(high=True)
    prompter = _Prompter(["n"])
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: True, prompt=prompter)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(plan)
    assert exc.value.reason == "user_declined"
    assert len(prompter.prompts) == 1  # phrase never asked


def test_interactive_yes_does_not_bypass_high_risk_phrase() -> None:
    # --yes skips the first y/N but the SECOND confirmation phrase still runs.
    plan = _plan(high=True, finding_id="disk-full")
    prompter = _Prompter(["disk-full"])  # only the phrase is asked
    gate = ApprovalGate(assume_yes=True, is_tty=lambda: True, prompt=prompter)
    gate.authorize(plan)
    assert len(prompter.prompts) == 1
    assert "disk-full" in prompter.prompts[0]  # phrase prompt references finding id


def test_interactive_yes_high_risk_wrong_phrase_still_rejected() -> None:
    plan = _plan(high=True, finding_id="disk-full")
    prompter = _Prompter(["nope"])
    gate = ApprovalGate(assume_yes=True, is_tty=lambda: True, prompt=prompter)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(plan)
    assert exc.value.reason == "phrase_mismatch"


# --------------------------------------------------------------------------- #
# rejection vs execution-failure are distinguishable (token / type)
# --------------------------------------------------------------------------- #


def test_rejection_carries_machine_stable_reason_token() -> None:
    # The CLI renders `approval-rejected: <reason>` — the reason must be a
    # stable token (not free text) so scripts can branch on it.
    gate = ApprovalGate(assume_yes=False, is_tty=lambda: False, prompt=_never_prompt)
    with pytest.raises(ApprovalRejected) as exc:
        gate.authorize(_plan())
    assert exc.value.reason in {
        "non_interactive_no_yes",
        "high_risk_non_interactive",
        "user_declined",
        "phrase_mismatch",
    }
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
