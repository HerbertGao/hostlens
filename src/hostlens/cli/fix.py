"""``hostlens fix <plan-file>`` Typer command — controlled remediation
execution (M9 P2).

Spec: ``openspec/changes/add-remediation-execution-workflow/specs/
remediation-execution-workflow/spec.md`` §需求:`hostlens fix` 必须默认 dry-run、
拒绝 root、解析 target、稳健处理输入错误 + §需求:dry-run 与真实执行必须共享同一编排.

This wires the group-A remediation subsystem (``Executor`` / ``ApprovalGate``
/ ``AuditLog``) into a **write-path, safety-sensitive** CLI command. The
orchestration order is fixed (both dry-run and real execution share it; they
diverge only at the injected ``CommandRunner`` and at whether ``audit.log`` is
written):

1. **EUID==0 refusal (earliest gate)** — before ``load_json`` / target
   resolution / any plan-content rendering. A plan command may contain a
   ``redact_text``-missed flag-form secret (``redis-cli -a <pw>`` /
   ``mysql -p<pw>`` / ``user:pw@host`` URL userinfo); refusing root only after
   the preview would already have printed plan content to stdout/stderr. So the
   very first thing this command does is refuse ``os.geteuid() == 0`` (dry-run
   included).
2. ``RemediationPlan.load_json`` — any failure (malformed JSON / duplicate key
   / schema violation / file absent / unreadable / empty) → single stderr line
   + exit 2, never a traceback.
2b. **risk-tiered divergence** — a plan with any ``medium``/``high`` step is
   propose-only: render a human runbook and exit 4 **before** target resolution
   / preview / approval / execution / audit. Only all-``low`` plans continue.
   ``--dry-run`` is a no-op for elevated plans (they never execute regardless).
3. target resolution — ``load_targets_config`` + ``build_registry_from_config``
   + ``registry.get(plan.target_name)`` (same path as ``hostlens inspect``).
   ``local`` is **not** implicitly present. The catch contract covers every
   resolution exception: ``registry.get`` → ``KeyError``; corrupt schema →
   ``pydantic.ValidationError``; YAML/env/docker config error → ``ConfigError``;
   unreadable / directory-form ``targets.yaml`` → ``OSError`` — all map to
   exit 3, no traceback.
4. preview — every step's ``precheck/forward/rollback/verify`` triplet, with
   commands best-effort redacted via ``core/redact.py`` ``redact_text``.
5. ``ApprovalGate`` — ``assume_yes`` from ``--yes`` and ``is_tty`` from
   ``sys.stdin.isatty()``. ``--yes`` never bypasses the high-risk double
   confirm (handled inside ``ApprovalGate``).
6. execution — ``DryRunCommandRunner`` (zero exec, no audit) in dry-run;
   ``RealCommandRunner`` + two-phase audit (intent before exec, result after)
   otherwise.

Exit code contract (project-wide ``3 > 2 > 1 > 0``, plus ``4``):

- ``0`` success.
- ``4`` plan contains ``medium``/``high`` steps → runbook rendered, **not
  executed** (a policy outcome, not an error; non-zero so scripts / Agents can
  tell "not executed, human must act" from "executed"). Decided at step 2b,
  before the 1/2/3 conditions of the execute path.
- ``1`` non-TTY without ``--yes`` / user declined / execution failure
  (including incomplete rollback). These share ``1`` but the
  stderr line carries a machine-parseable prefix: ``approval-rejected:`` for a
  safety-gate refusal vs ``execution-failed:`` for a runtime failure. The
  ``audit-*`` prefixes (intent / result write failure, precheck) are also exit
  1 — they are write-path failures of this command, not a malformed plan.
- ``2`` illegal plan (schema / duplicate key / malformed JSON / file IO).
- ``3`` configuration / target resolution error (consistent with ``inspect``).

stdout / stderr separation: the preview (which the operator reads to decide
approval) and the post-run summary go to stdout; every error / hint goes to
stderr; **no** Python traceback ever reaches the user.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import jinja2
import typer
from pydantic import ValidationError

from hostlens.core.config import load_settings
from hostlens.core.exceptions import ConfigError, TargetError
from hostlens.core.redact import redact_text
from hostlens.remediation.approval import ApprovalGate, ApprovalRejected
from hostlens.remediation.audit import AuditError, AuditLog
from hostlens.remediation.executor import (
    DryRunCommandRunner,
    Executor,
    PlanExecutionResult,
    RealCommandRunner,
)
from hostlens.remediation.models import RemediationPlan
from hostlens.remediation.runbook import render_runbook
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import load_targets_config
from hostlens.targets.registry import build_registry_from_config

__all__ = ["fix_cmd"]


# --------------------------------------------------------------------------- #
# Root refusal — the earliest gate (before load / target / any plan render)
# --------------------------------------------------------------------------- #


def _refuse_root() -> None:
    """Refuse to run as ``EUID==0`` — the earliest safety gate.

    Per CLAUDE.md §4.5 (write ops reject root) and the spec's earliest-gate
    requirement: this runs **before** ``load_json`` / target resolution / any
    plan-content rendering so a flag-form secret (which ``redact_text`` does
    not cover) can never reach stdout/stderr before the refusal. dry-run is
    refused too — the preview alone would leak plan commands.
    """

    if os.geteuid() == 0:
        typer.echo(
            "approval-rejected: refusing to run as root (EUID=0); run 'hostlens fix' "
            "as a non-privileged user — a sudo run would create root-owned audit "
            "files and may leak flag-form secrets in plan previews",
            err=True,
        )
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Plan loading (exit 2 on any input error)
# --------------------------------------------------------------------------- #


def _load_plan(plan_file: str) -> RemediationPlan:
    """Load + validate the plan from ``plan_file``; exit 2 on any input error.

    Reads the file then parses through ``RemediationPlan.load_json`` (which
    rejects duplicate JSON keys and runs full Pydantic validation). Every
    failure mode — file absent / unreadable / a directory / not UTF-8 / empty /
    malformed JSON / duplicate key (``ValueError``) / schema violation
    (``ValidationError``) — becomes a single stderr line + exit 2, never a
    traceback.
    """

    path = Path(plan_file)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"invalid plan: cannot read {plan_file}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except UnicodeDecodeError as exc:
        typer.echo(f"invalid plan: {plan_file} is not valid UTF-8 ({exc})", err=True)
        raise typer.Exit(code=2) from exc

    if raw.strip() == "":
        typer.echo(f"invalid plan: {plan_file} is empty", err=True)
        raise typer.Exit(code=2)

    try:
        return RemediationPlan.load_json(raw)
    except ValidationError as exc:
        typer.echo(
            f"invalid plan: {plan_file} violates the remediation plan schema "
            f"({exc.error_count()} error(s))",
            err=True,
        )
        raise typer.Exit(code=2) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        # ValueError covers the duplicate-key rejection raised by
        # ``_reject_duplicate_keys``; JSONDecodeError covers malformed JSON.
        typer.echo(f"invalid plan: {plan_file} is malformed JSON ({exc})", err=True)
        raise typer.Exit(code=2) from exc


# --------------------------------------------------------------------------- #
# Risk-tiered divergence — medium/high plans are propose-only (runbook, exit 4)
# --------------------------------------------------------------------------- #

_ELEVATED_RISK = frozenset({"medium", "high"})


def _has_elevated_risk(plan: RemediationPlan) -> bool:
    """True iff any step is `medium`/`high` — plan-level risk = max(step risk).

    A single elevated step routes the whole plan to the runbook: steps have
    ordering / rollback dependencies, so executing only the `low` steps while
    skipping an elevated one would break plan semantics. Conservative by design
    (any elevated → propose-only).
    """

    return any(step.risk_level in _ELEVATED_RISK for step in plan.steps)


def _render_runbook_and_exit(plan: RemediationPlan, *, out: str | None) -> None:
    """Render `plan` to a human runbook and exit 4 — never execute.

    This is the medium/high branch: zero `ExecutionTarget.exec`, zero audit,
    no target resolution, no approval gate. The render fault is fail-closed
    (exit 1, never fall through to execution). Exit 4 is a **policy** outcome
    (not an error) so scripts / Agents can mechanically tell "not executed,
    human must act" from "executed".
    """

    try:
        rendered = render_runbook(plan)
    except jinja2.TemplateError as exc:
        typer.echo(f"runbook-render-failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if out is not None:
        try:
            Path(out).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            typer.echo(f"runbook-write-failed: cannot write {out}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"runbook for {plan.finding_id} written to {out} (medium/high-risk plan — NOT executed)"
        )
    else:
        typer.echo(rendered)

    typer.echo(
        "not-executed: this plan contains medium/high-risk steps and is "
        "propose-only — run the runbook manually on the target after review",
        err=True,
    )
    raise typer.Exit(code=4)


# --------------------------------------------------------------------------- #
# Target resolution (exit 3 on any config / resolution error)
# --------------------------------------------------------------------------- #


def _resolve_target(target_name: str) -> ExecutionTarget:
    """Resolve ``target_name`` to a live ``ExecutionTarget``; exit 3 on error.

    Same path as ``hostlens inspect``: ``load_targets_config`` +
    ``build_registry_from_config`` + ``registry.get``. ``local`` is **not**
    implicitly present — an unknown name is a usage/config error (exit 3).

    The catch set covers the full resolution exception surface:

    - ``KeyError`` — ``registry.get`` miss (the name is not registered).
    - ``ValidationError`` — a structurally corrupt ``targets.yaml`` (unknown
      type / SSH field violation / name-pattern mismatch) that
      ``build_registry_from_config`` rejects with Pydantic, not ``ConfigError``.
    - ``ConfigError`` — YAML syntax / env-placeholder / docker-host-scheme
      errors raised by the loader.
    - ``OSError`` — ``targets.yaml`` unreadable or a directory
      (``PermissionError`` / ``IsADirectoryError``), which ``read_text`` raises
      before ``load_targets_config`` can wrap it.

    All map to exit 3 with a single stderr line, never a traceback.
    """

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens fix: configuration error: {exc}", err=True)
        raise typer.Exit(code=3) from exc

    try:
        config = load_targets_config(settings.targets_config_path)
        registry = build_registry_from_config(config, settings)
        return registry.get(target_name)
    except KeyError as exc:
        typer.echo(
            f"target not found: {target_name}; run 'hostlens target list' to see "
            "registered targets (the plan's target_name must be registered in "
            "targets.yaml — 'local' is not implicit)",
            err=True,
        )
        raise typer.Exit(code=3) from exc
    except (TargetError, ConfigError, ValidationError, OSError) as exc:
        typer.echo(f"hostlens fix: failed to resolve target: {exc}", err=True)
        raise typer.Exit(code=3) from exc


# --------------------------------------------------------------------------- #
# Preview (stdout; commands best-effort redacted)
# --------------------------------------------------------------------------- #


def _preview(plan: RemediationPlan, *, dry_run: bool) -> None:
    """Print the plan's per-step triplet to stdout, commands redacted.

    ``redact_text`` masks ``key=value`` / ``Bearer`` / JWT / ``sk-`` forms;
    CLI flag-form secrets are a known residual leak (documented in the audit /
    proposal Security sections), which is exactly why the root refusal runs
    before this preview. ``estimated_duration_seconds`` is shown for reference
    only — no hard timeout is derived from it.
    """

    mode = "dry-run (no commands will be executed)" if dry_run else "execution"
    typer.echo(f"remediation plan: {plan.finding_id} -> target {plan.target_name} [{mode}]")
    typer.echo(f"rationale: {plan.rationale}")
    typer.echo(f"estimated_duration_seconds: {plan.estimated_duration_seconds}")
    typer.echo(f"steps: {len(plan.steps)}")
    for index, step in enumerate(plan.steps):
        typer.echo(f"  [{index}] {step.description} (risk={step.risk_level})")
        if step.precheck_cmd is not None:
            typer.echo(f"      precheck: {redact_text(step.precheck_cmd)}")
        typer.echo(f"      forward:  {redact_text(step.forward_cmd)}")
        typer.echo(f"      verify:   {redact_text(step.verify_cmd)}")
        if step.rollback_cmd is not None:
            typer.echo(f"      rollback: {redact_text(step.rollback_cmd)}")
        else:
            typer.echo("      rollback: <none> (rollback-unavailable on failure)")


# --------------------------------------------------------------------------- #
# Result summary (stdout) + exit-code computation
# --------------------------------------------------------------------------- #


def _summarize_result(result: PlanExecutionResult) -> None:
    """Print a compact per-step + rollback summary of a real run to stdout."""

    typer.echo(
        f"execution: succeeded={result.succeeded} rollback_complete={result.rollback_complete}"
    )
    for step in result.steps:
        typer.echo(f"  [{step.index}] {step.description}: {step.status}")
    for rollback in result.rollbacks:
        typer.echo(f"  rollback[{rollback.index}] {rollback.description}: {rollback.status}")


# --------------------------------------------------------------------------- #
# Real execution path (audit two-phase + RealCommandRunner)
# --------------------------------------------------------------------------- #


def _execute_real(plan: RemediationPlan, target: ExecutionTarget) -> None:
    """Run the plan for real: precheck audit → intent → execute → result audit.

    Audit timing (spec §需求:audit 必须 … 不可写不静默):

    - ``precheck_writable()`` first — an unwritable audit directory is an
      execution-front safety gate (exit 1, zero side effects).
    - ``write_intent`` next — a failure here means exec has not started (zero
      side effects), so abort + exit 1.
    - ``execute`` — the real ``RealCommandRunner`` drives ``ExecutionTarget.exec``;
      the ``Executor`` never raises for command/transport failures (those become
      recorded outcomes), so this call returns a full ``PlanExecutionResult``.
    - ``write_result`` last — a failure here means side effects already
      happened; surface loudly (exit 1) rather than swallow.

    Execution failure (``succeeded=False`` or ``rollback_complete=False``) is
    exit 1 with an ``execution-failed:`` prefix, distinct from the
    ``approval-rejected:`` safety-gate prefix.
    """

    audit = AuditLog()
    try:
        audit.precheck_writable()
    except AuditError as exc:
        typer.echo(f"audit-precheck: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        audit.write_intent(plan)
    except AuditError as exc:
        # Intent failed before exec started → zero side effects, abort.
        typer.echo(f"audit-intent: {exc} (no command was executed)", err=True)
        raise typer.Exit(code=1) from exc

    runner = RealCommandRunner(target)
    executor = Executor(plan, runner)
    result = asyncio.run(executor.execute())

    try:
        audit.write_result(plan, result)
    except AuditError as exc:
        # Result failed after exec completed → side effects already happened.
        typer.echo(
            f"audit-result: {exc}; side effects HAVE OCCURRED but the audit result "
            "record was not persisted",
            err=True,
        )
        _summarize_result(result)
        raise typer.Exit(code=1) from exc

    _summarize_result(result)

    if not result.succeeded or not result.rollback_complete:
        detail = "plan did not succeed"
        if not result.rollback_complete:
            detail += "; rollback incomplete (some steps may remain partially applied)"
        typer.echo(f"execution-failed: {detail}", err=True)
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Dry-run execution path (DryRunCommandRunner, zero exec, no audit)
# --------------------------------------------------------------------------- #


def _execute_dry_run(plan: RemediationPlan) -> None:
    """Walk the full orchestration with zero exec and no audit write.

    The ``DryRunCommandRunner`` records each command and reports synthetic
    success, so the whole happy path is walked (every step "succeeds", no
    rollback) — but ``ExecutionTarget.exec`` is never called and ``audit.log``
    is never touched. The command sequence the operator inspects is surfaced by
    ``_preview`` (called earlier); this function only walks the orchestration
    (asserted by tests via ``runner.recorded``) and prints the command count.
    """

    runner = DryRunCommandRunner()
    executor = Executor(plan, runner)
    asyncio.run(executor.execute())
    typer.echo(
        f"dry-run complete: {len(runner.recorded)} command(s) would run; "
        "nothing was executed and no audit record was written"
    )


# --------------------------------------------------------------------------- #
# Typer command
# --------------------------------------------------------------------------- #


def fix_cmd(
    plan_file: str = typer.Argument(
        ...,
        help="Path to an approved remediation plan JSON file.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Force preview only — never execute, never write audit; overrides "
        "--yes / interactive confirm.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Approve execution non-interactively (skips the y/N prompt) for a "
        "low-risk plan. Has no effect under --dry-run, and cannot execute a "
        "medium/high-risk plan (those are propose-only — rendered as a runbook).",
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        help="For a medium/high-risk plan, write the runbook to this file "
        "instead of stdout. Ignored for low-risk plans (which execute).",
    ),
) -> None:
    """Execute an approved remediation plan (or preview it with ``--dry-run``).

    The command is write-path and safety-sensitive. Orchestration order is
    fixed: EUID==0 refusal (earliest gate) → load plan → **risk-tiered
    divergence** (medium/high → runbook, exit 4) → resolve target → preview →
    approval gate → execute. Only all-low plans reach target resolution and
    beyond; dry-run and real execution share that tail and diverge only at
    whether ``ExecutionTarget.exec`` is really called and ``audit.log`` written.

    A plan with any ``medium``/``high`` step is **propose-only**: it is rendered
    as a human runbook (stdout, or ``--out <file>``) and exits 4 — never
    executed, no approval, no audit. The AI does not perform consequential
    fixes whose business blast radius it cannot own; a human runs the runbook.

    ``--dry-run`` is a force-preview flag (default off) for the all-low
    execution path: pure preview, zero execution, no audit. Without ``--dry-run``
    the ``ApprovalGate`` decides — TTY prompts y/N, a non-TTY without ``--yes``
    is refused (exit 1), and ``--yes`` authorizes execution.

    Exit codes (project-wide ``3 > 2 > 1 > 0``, plus ``4``):
      0: success (or a completed dry-run preview)
      4: medium/high plan → runbook rendered, NOT executed (policy outcome)
      1: non-TTY without --yes / user declined / execution failure (incl.
         incomplete rollback) / audit write failure. The stderr line carries a
         machine-parseable prefix: ``approval-rejected:`` (safety-gate refusal)
         vs ``execution-failed:`` (runtime failure) vs ``audit-*:`` (audit write
         failure).
      2: illegal plan (schema / duplicate key / malformed JSON / file IO)
      3: configuration / target resolution error
    """

    # ---- 1. EUID==0 refusal — the earliest gate (before any plan render) -- #
    _refuse_root()

    # ---- 2. Load + validate the plan (exit 2 on input error) ------------- #
    plan = _load_plan(plan_file)

    # ---- 3. Risk-tiered divergence: medium/high → runbook (exit 4) ------- #
    #
    # A plan with any medium/high step is propose-only: render a runbook and
    # exit before target resolution / preview / approval / execution / audit.
    # --dry-run is a no-op here (these never execute regardless).
    if _has_elevated_risk(plan):
        _render_runbook_and_exit(plan, out=out)

    # ---- 4. Resolve target (exit 3; only all-low plans reach here) -------- #
    target = _resolve_target(plan.target_name)

    # ---- 4. Preview (stdout; commands best-effort redacted) -------------- #
    _preview(plan, dry_run=dry_run)

    # ---- 5. dry-run overrides --yes: preview only, zero exec, no audit --- #
    if dry_run:
        _execute_dry_run(plan)
        return

    # ---- 6. Approval gate (--yes never bypasses high-risk double-confirm) - #
    gate = ApprovalGate(assume_yes=yes, is_tty=sys.stdin.isatty)
    try:
        gate.authorize(plan)
    except ApprovalRejected as exc:
        typer.echo(f"approval-rejected: {exc.reason}: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except (EOFError, KeyboardInterrupt) as exc:
        # The interactive prompt reads stdin via input(); Ctrl-D (EOFError) /
        # Ctrl-C (KeyboardInterrupt — a BaseException, so listed explicitly)
        # abort the prompt. Aborting approval is a refusal: map to the same
        # approval-rejected exit-1 contract rather than letting a bare
        # traceback escape past main().
        typer.echo("approval-rejected: aborted: approval prompt interrupted", err=True)
        raise typer.Exit(code=1) from exc

    # ---- 7. Real execution (two-phase audit + RealCommandRunner) --------- #
    _execute_real(plan, target)
