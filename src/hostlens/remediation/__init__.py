"""Controlled remediation (M9). P1a ships the frozen plan data contract.

`RemediationStep` / `RemediationPlan` are pure Pydantic v2 models — the SOT
consumed by the P1b Planner (structured output), P2 Executor (load + run), and
P3 Lark approval card. No execution logic lives here.
"""

from __future__ import annotations

from hostlens.remediation.models import RemediationPlan, RemediationStep, RiskLevel

__all__ = ["RemediationPlan", "RemediationStep", "RiskLevel"]
