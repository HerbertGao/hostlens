"""Human-readable remediation runbook renderer (M9 risk-tiered execution).

A `RemediationPlan` whose steps include any `risk_level ∈ {"medium","high"}`
is **propose-only**: Hostlens does not execute it. Instead this module renders
the plan into a Markdown runbook a human runs themselves on the target. The
render is **deterministic** (Jinja2, no LLM, no randomness, no IO beyond the
in-memory template) and **pure**: it imports no `Executor` / `CommandRunner`,
calls no `ExecutionTarget.exec`, writes no audit log, and touches no Notifier —
its only effect is returning a string.

Commands are passed through `core/redact.py` `redact_text` (best-effort: covers
`key=value` / `Bearer` / JWT / `sk-`). The same residual leak as the audit /
preview paths applies — flag-form secrets (`mysql -p<pw>`) are **not** covered;
plan authors must inject secrets via `ExecutionTarget.exec`'s `env` rather than
inline command text. Because a runbook is local-only output (never pushed to
any channel), this residual surface is strictly smaller than a remote-approval
card would be.

Only the four command fields are redacted; `rationale` / `description` (LLM-
authored free text) are rendered verbatim — same as the CLI preview path
(`cli/fix.py` `_preview`), so this is a consistent, pre-existing residual, not
a new one. A plan author who pastes a secret into the prose rationale leaks it
in both surfaces; that is out of scope for this renderer.
"""

from __future__ import annotations

import jinja2

from hostlens.core.redact import redact_text
from hostlens.remediation.models import RemediationPlan

__all__ = ["render_runbook"]

_TEMPLATE_NAME = "runbook.md.j2"


def _build_environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("hostlens.remediation", "templates"),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["redact"] = redact_text
    return env


_ENV = _build_environment()


def render_runbook(plan: RemediationPlan) -> str:
    """Render `plan` to a human-readable Markdown runbook string.

    Deterministic: the same plan always renders byte-identically. Raises
    `jinja2.TemplateError` on a render fault — the caller (CLI) maps that to a
    non-zero exit and **never** falls back to executing the plan (fail-closed).
    """

    return _ENV.get_template(_TEMPLATE_NAME).render(plan=plan)
