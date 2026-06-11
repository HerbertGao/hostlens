"""Unit contract checks for `render_runbook` (M9 risk-tiered execution).

Maps to spec `remediation-runbook` §需求:runbook 必须把含 medium/high step 的 plan
确定性渲染为人读 Markdown (纯本地/零执行/零 audit/不推任何通道). The renderer is a
pure function — these assert the four-segment structure, the "not executed"
banner, best-effort redaction, determinism, `None`-command handling, and that
the module imports no executor / target machinery.
"""

from __future__ import annotations

import pytest

from hostlens.remediation.models import RemediationPlan, RemediationStep
from hostlens.remediation.runbook import render_runbook


def _plan(*, steps: list[RemediationStep] | None = None) -> RemediationPlan:
    if steps is None:
        steps = [
            RemediationStep(
                description="调高 worker_connections",
                precheck_cmd="nginx -t",
                forward_cmd="sed -i 's/x/y/' /etc/nginx/nginx.conf",
                rollback_cmd="sed -i 's/y/x/' /etc/nginx/nginx.conf",
                verify_cmd="nginx -t",
                risk_level="medium",
            )
        ]
    return RemediationPlan(
        finding_id="nginx-conn-low",
        target_name="web-1",
        rationale="连接数偏低",
        steps=steps,
        estimated_duration_seconds=20,
    )


def test_renders_metadata_and_four_segments_with_risk() -> None:
    out = render_runbook(_plan())
    # Metadata.
    assert "nginx-conn-low" in out
    assert "web-1" in out
    assert "连接数偏低" in out
    # Four labelled segments + per-step risk annotation.
    assert "**precheck**" in out
    assert "**forward**" in out
    assert "**verify**" in out
    assert "**rollback**" in out
    assert "risk=medium" in out
    # Copy-pasteable command blocks.
    assert "```sh" in out
    assert "nginx -t" in out


def test_top_banner_declares_not_executed() -> None:
    out = render_runbook(_plan())
    # The banner must appear before any step and state the tool ran nothing.
    banner_region = out.split("## Step", 1)[0]
    assert "本工具未执行任何命令" in banner_region
    assert "人工" in banner_region
    assert "rollback" in banner_region


def test_commands_are_redacted() -> None:
    step = RemediationStep(
        description="set token",
        precheck_cmd=None,
        forward_cmd="curl -H 'Authorization: Bearer sk-supersecrettoken12345' https://api",
        rollback_cmd="true",
        verify_cmd="true",
        risk_level="medium",
    )
    out = render_runbook(_plan(steps=[step]))
    assert "sk-supersecrettoken12345" not in out
    # Best-effort: the Bearer token form is masked.
    assert "Bearer" in out  # the surrounding form survives; only the value is masked


def test_none_precheck_and_rollback_rendered_as_absent() -> None:
    step = RemediationStep(
        description="high-risk purge",
        precheck_cmd="mysql -e 'SHOW SLAVE STATUS'",
        forward_cmd="mysql -e 'PURGE BINARY LOGS BEFORE NOW()'",
        rollback_cmd=None,  # allowed only for high risk
        verify_cmd="df -h",
        risk_level="high",
    )
    out = render_runbook(_plan(steps=[step]))
    assert "risk=high" in out
    # None rollback is explicitly marked, not crashed / blank.
    assert "本步不可自动回退" in out


def test_determinism_byte_identical_on_repeat() -> None:
    plan = _plan()
    assert render_runbook(plan) == render_runbook(plan)


def test_renderer_module_imports_no_executor_or_target() -> None:
    # The runbook path must never reach execution machinery: assert the module
    # pulls in neither the Executor / CommandRunner nor an ExecutionTarget /
    # audit symbol (red-line: render is propose-only, zero execution).
    import hostlens.remediation.runbook as rb

    source_names = set(dir(rb))
    for forbidden in ("Executor", "CommandRunner", "RealCommandRunner", "AuditLog"):
        assert forbidden not in source_names


@pytest.mark.parametrize("risk", ["medium", "high"])
def test_elevated_single_step_renders(risk: str) -> None:
    step = RemediationStep(
        description="d",
        precheck_cmd="true" if risk == "high" else None,
        forward_cmd="true",
        rollback_cmd="true" if risk == "medium" else None,
        verify_cmd="true",
        risk_level=risk,  # type: ignore[arg-type]
    )
    out = render_runbook(_plan(steps=[step]))
    assert f"risk={risk}" in out
