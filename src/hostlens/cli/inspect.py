"""``hostlens inspect`` Typer command — single-Inspector run + report render.

Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``.

The command is the M1 end-to-end pipeline: TargetRegistry → InspectorRegistry
→ ``InspectorRunner.run`` → ``Report.from_inspector_results`` → markdown/json
render. It is a **read-only** command and tolerates ``EUID==0`` (matches the
posture of ``hostlens inspectors list/show`` and ``hostlens target list``).

Exit code contract (closed 4-value set, priority ``3 > 2 > 1 > 0``):

- ``0`` healthy: ``InspectorResult.status == "ok"`` AND no critical finding
- ``1`` business critical: ``status == "ok"`` AND ≥1 ``severity == "critical"``
- ``2`` runner failure: ``status != "ok"`` (timeout / target_unreachable /
       requires_unmet / exception); also Report ValidationError from
       finished_at < started_at (system clock skew, not user-controlled)
- ``3`` usage error: target / inspector unknown, ``--parameters`` parse
       failure, ``--output`` write failure, Typer usage error rewritten by
       the click-UsageError wrapper, ``--timeout`` out of [1, 300]

stdout / stderr separation: rendered Report → stdout (or ``--output``
file); errors / warnings → stderr; **no** Python traceback ever reaches
the user (CLI boundary wraps unexpected exceptions as
``internal: <kind>: <msg>``).

``--timeout`` injection path (security-critical): the runner contract
keeps ``InspectorRunner.run`` signature stable (no timeout kwarg). When
the operator passes ``--timeout`` the CLI rebuilds a new ``CollectSpec``
via ``CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds":
cli_timeout})`` and clones the manifest with ``model_copy(update=...)``.
The construction goes through Pydantic Field validation (``ge=1, le=300``)
as a defense-in-depth second gate behind the CLI [1, 300] check. Direct
``manifest.collect.model_copy(update=...)`` is forbidden — Pydantic v2
``model_copy(update=...)`` skips field validation and would silently
admit out-of-range timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import structlog
import typer
from pydantic import ValidationError

from hostlens.agent.planner import PlannerResult
from hostlens.cli._intent import (
    RichLiveObserver,
    render_intent_report,
    run_intent_diagnosis,
)
from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import ConfigError, InspectorError, TargetError
from hostlens.core.logging import configure_logging
from hostlens.inspectors.registry import InspectorRegistry, build_registry_from_search_paths
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import CollectSpec, InspectorManifest
from hostlens.reporting import ReportStore, render_json, render_markdown
from hostlens.reporting.models import Report, ReportStatus
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import load_targets_config
from hostlens.targets.registry import TargetRegistry, build_registry_from_config

__all__ = ["inspect_cmd"]


_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 300

# Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``
# §需求:`hostlens inspect` 必须以 stdout/stderr 分离 与 默认 stdout 模式工作
# ("warning - 如 evidence 字节数 > 8MB"). Threshold is fixed (no flag) for M1;
# `docs/operations/inspect.md` Known accepted risks documents the policy.
_LARGE_REPORT_BYTES = 8 * 1024 * 1024  # 8 MiB


# --------------------------------------------------------------------------- #
# Parameter parsing helpers
# --------------------------------------------------------------------------- #


def _parse_parameters_option(raw: str | None) -> dict[str, Any]:
    """Parse the ``--parameters`` value into a dict.

    Accepts two shapes (spec §需求:`--parameters` 双语法):

    - Inline JSON: starts with ``{`` — parsed via ``json.loads`` and must
      decode to a dict.
    - File ref: starts with ``@`` — remainder is a path; file content is
      read then JSON-parsed.

    Any failure raises ``typer.Exit(code=3)`` after emitting a single
    stderr line with the documented prefix. ``None`` returns ``{}`` so
    callers can pass the result straight to ``InspectorRunner.run``.
    """

    if raw is None:
        return {}

    if raw.startswith("@"):
        path = Path(raw[1:])
        try:
            text = path.read_text()
        except OSError as exc:
            typer.echo(f"failed to read --parameters file: {exc}", err=True)
            raise typer.Exit(code=3) from exc
        except UnicodeDecodeError as exc:
            typer.echo(
                f"failed to read --parameters file: not valid UTF-8 ({exc})",
                err=True,
            )
            raise typer.Exit(code=3) from exc
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            typer.echo(f"invalid --parameters: {exc}", err=True)
            raise typer.Exit(code=3) from exc
    elif raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            typer.echo(f"invalid --parameters: {exc}", err=True)
            raise typer.Exit(code=3) from exc
    else:
        typer.echo(
            "invalid --parameters: must start with '{' (inline JSON) or '@' (file path)",
            err=True,
        )
        raise typer.Exit(code=3)

    if not isinstance(parsed, dict):
        typer.echo(
            f"invalid --parameters: expected JSON object, got {type(parsed).__name__}",
            err=True,
        )
        raise typer.Exit(code=3)
    return parsed


def _validate_timeout(value: int | None) -> int | None:
    """Reject ``--timeout`` values outside [1, 300]; pass-through otherwise.

    Mirrors ``CollectSpec.timeout_seconds = Field(ge=1, le=300)``. We
    validate at the CLI boundary so the operator sees a clear error
    message before the runner is invoked; the CollectSpec rebuild step
    is the defense-in-depth second gate (and the only one a unit test
    can hit by monkey-patching this function).
    """

    if value is None:
        return None
    if value < _TIMEOUT_MIN or value > _TIMEOUT_MAX:
        typer.echo("invalid --timeout: must be in [1, 300]", err=True)
        raise typer.Exit(code=3)
    return value


def _apply_timeout_override(
    manifest: InspectorManifest, cli_timeout: int | None
) -> InspectorManifest:
    """Return a manifest clone whose ``collect.timeout_seconds == cli_timeout``.

    When ``cli_timeout is None`` the original manifest is returned (same
    reference), keeping the registry entry untouched. When set, a new
    ``CollectSpec`` is constructed via the ``model_dump`` + ``CollectSpec``
    pipeline so Pydantic ``Field(ge=1, le=300)`` validation fires — this
    is the second gate behind ``_validate_timeout`` (a unit test
    monkey-patches the CLI check to confirm CollectSpec rejects 9999).
    """

    if cli_timeout is None:
        return manifest
    new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout})
    return manifest.model_copy(update={"collect": new_collect})


# --------------------------------------------------------------------------- #
# Target / Inspector lookup
# --------------------------------------------------------------------------- #


def _load_target_registry() -> TargetRegistry:
    """Assemble the TargetRegistry from the configured ``targets.yaml``.

    Catches the documented ``ConfigError`` / ``TargetError`` /
    ``ValidationError`` set used elsewhere in the CLI (target.py).
    Failure here is exit 3 (parameter/configuration error class) with a
    single-line stderr prefix; the user can fix the underlying config
    and re-run.
    """

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens inspect: configuration error: {exc}", err=True)
        raise typer.Exit(code=3) from exc

    try:
        config = load_targets_config(settings.targets_config_path)
        return build_registry_from_config(config, settings)
    except (ConfigError, TargetError, ValidationError) as exc:
        typer.echo(f"hostlens inspect: failed to load targets: {exc}", err=True)
        raise typer.Exit(code=3) from exc


def _resolve_target(registry: TargetRegistry, name: str) -> ExecutionTarget:
    """Look up ``name`` in ``registry``; emit stderr hint + exit 3 when missing.

    The hint string format is spec-locked (`target not found: <name>;
    run 'hostlens target list' ...`) — tests grep for the prefix.
    """

    try:
        return registry.get(name)
    except KeyError as exc:
        typer.echo(
            f"target not found: {name}; run 'hostlens target list' to see registered targets",
            err=True,
        )
        raise typer.Exit(code=3) from exc


def _resolve_inspector(name: str) -> InspectorManifest:
    """Look up ``name`` in the assembled InspectorRegistry.

    The spec also says load errors at the user-path layer must surface,
    but ``hostlens inspect`` is a single-inspector entry point — if the
    inspector the user asked for **is** the broken one, the loader will
    have skipped it and ``registry.get`` raises ``inspector_not_found``,
    which is the same exit-3 surface the test scenario asserts. Per-file
    load errors on **other** manifests are emitted to stderr by
    ``hostlens inspectors list`` / ``doctor``, not here, to keep the
    inspect command focused on the requested run.
    """

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens inspect: configuration error: {exc}", err=True)
        raise typer.Exit(code=3) from exc

    try:
        result = build_registry_from_search_paths(
            settings.inspectors_search_paths,
            settings=settings,
        )
    except InspectorError as exc:
        typer.echo(f"hostlens inspect: inspector registry error: {exc}", err=True)
        raise typer.Exit(code=3) from exc

    try:
        return result.registry.get(name)
    except InspectorError as exc:
        # ``kind`` is always ``inspector_not_found`` here per
        # InspectorRegistry.get contract; surface a stable hint string.
        typer.echo(
            f"inspector not found: {name}; "
            "run 'hostlens inspectors list' to see available inspectors",
            err=True,
        )
        raise typer.Exit(code=3) from exc


# --------------------------------------------------------------------------- #
# Runner dispatch + Report construction
# --------------------------------------------------------------------------- #


async def _dispatch(
    manifest: InspectorManifest,
    target: ExecutionTarget,
    parameters: dict[str, Any],
    *,
    allow_privileged: bool,
    target_registry: TargetRegistry,
) -> InspectorResult:
    """Run the inspector via ``InspectorRunner.run``.

    Wrapping ``asyncio.run`` here keeps the Typer command body synchronous
    (Typer doesn't support async commands natively). ``settings`` is
    re-loaded to wire structlog properly; the registry is passed by the
    caller so the inspector / target lookups share one Settings instance.

    Structlog is reconfigured to emit to ``sys.stderr`` here (rather than
    the ``PrintLoggerFactory`` default of stdout) so the rendered Report
    on stdout stays free of ``inspector_started`` / ``inspector_finished``
    log events. The spec requires strict stdout / stderr separation
    (§需求:`hostlens inspect` 必须以 stdout/stderr 分离 与 默认 stdout 模式工作).
    """

    settings = load_settings()
    configure_logging(settings.log_mode)
    _redirect_structlog_to_stderr()
    logger = structlog.get_logger(__name__)
    runner = InspectorRunner(target_registry, settings=settings, logger=logger)
    return await runner.run(
        manifest,
        target,
        parameters,
        allow_privileged=allow_privileged,
    )


def _redirect_structlog_to_stderr() -> None:
    """Re-bind the structlog logger factory to write to ``sys.stderr`` and
    raise the level filter to WARNING so info / debug events do not pollute
    the user-facing stderr stream.

    ``configure_logging`` constructs the chain with
    ``structlog.PrintLoggerFactory()`` (default ``sys.stdout``) and a
    permissive wrapper (``make_filtering_bound_logger(0)`` — admits every
    level). For the inspect CLI we need:

    1. **Renderer → stderr** so the Report on stdout stays clean.
    2. **WARNING+ filter** so per-spec §需求:`hostlens inspect` 必须以
       stdout/stderr 分离 ("stderr 必须为空 (无错误时)"), the happy-path
       ``inspector_started`` / ``inspector_finished`` info events do not
       fire at all. Errors / warnings (which are legitimately user-facing)
       still surface.

    The restoration in the ``inspect_cmd`` ``finally`` block undoes this
    so neighbouring commands in the same process keep the global
    configuration set by ``configure_logging``.
    """

    current = structlog.get_config()
    structlog.configure(
        processors=current["processors"],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        context_class=current["context_class"],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def _build_report(
    target_name: str,
    target_type: str,
    inspector_result: InspectorResult,
    started_at: datetime,
    finished_at: datetime,
) -> Report:
    """Wrap ``Report.from_inspector_results`` with a CLI-friendly error path.

    Two failure modes are mapped:

    - ``ValidationError`` (e.g. ``finished_at < started_at`` when the
      system clock went backwards mid-run) → **exit 2**. The user has
      no control over the wall clock; this is a runtime/environment
      failure category.
    - ``ValueError`` (raised by ``from_inspector_results`` when
      ``inspector_results`` is empty) → **exit 3** per
      ``inspect-cli-command/spec.md`` §需求:`hostlens inspect` 退出码
      ("``Report.from_inspector_results`` 触发空 inspector_results 的
      invariant ValueError" 归 exit 3 usage path). The M1 CLI path
      always passes a single-element list so this branch is currently
      dead code, but the spec contract is honoured for M2 Planner
      Agent's future multi-inspector dispatch.
    """

    try:
        return Report.from_inspector_results(
            target_name,
            [inspector_result],
            intent=None,
            started_at=started_at,
            finished_at=finished_at,
            target_type=target_type,
        )
    except ValidationError as exc:
        typer.echo(f"internal: report validation failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(f"invalid: empty inspector_results ({exc})", err=True)
        raise typer.Exit(code=3) from exc


def _persist_report(report: Report) -> bool:
    """Save ``report`` to the default ``ReportStore`` for ``--persist``.

    Returns ``True`` when the report degraded to an orphan file (main store
    unwritable) so the caller can raise a non-zero exit while still having
    the report on disk; ``False`` on a clean insert. Writing the **local**
    store is not a remote-state change, so no ``--yes`` / approval gate
    applies (range note in the report-persistence spec).

    The store path resolves ``$XDG_DATA_HOME/hostlens/reports.db`` (default
    ``~/.local/share/hostlens/reports.db``) — the same knob ``hostlens
    reports`` reads, so a persisted run is immediately listable.
    """

    result = asyncio.run(ReportStore().save(report))
    if result.stored_as_orphan:
        typer.echo(
            f"warning: report store unavailable; wrote orphan file "
            f"{result.orphan_path} (run_id={result.run_id})",
            err=True,
        )
        return True
    return False


def _compute_exit_code(inspector_result: InspectorResult) -> int:
    """Map ``InspectorResult`` to the closed 4-value exit code set.

    Priority within this function is ``2 > 1 > 0`` (runner failure
    dominates critical findings). Exit 3 is owned by the caller paths
    (parameter / configuration errors) so it never appears here.
    """

    if inspector_result.status != "ok":
        return 2
    if any(f.severity == "critical" for f in inspector_result.findings):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Output rendering
# --------------------------------------------------------------------------- #


def _render_report(report: Report, fmt: str) -> str:
    """Dispatch to the markdown / json renderer.

    Both renderers internally call ``redact_report_for_render`` so the
    CLI never has to think about secret masking — by the time the bytes
    reach stdout (or the output file), OPERABILITY §7.2 defaults have
    been applied.
    """

    if fmt == "md":
        return render_markdown(report)
    # ``--format`` is constrained to ``{"md", "json"}`` by Typer; reaching
    # this branch with any other value is a CLI-routing bug.
    return render_json(report)


def _maybe_warn_large_report(report: Report) -> None:
    """Emit a single-line stderr warning when the report's evidence
    payload exceeds ``_LARGE_REPORT_BYTES``.

    Documented in ``docs/operations/inspect.md`` "Known accepted risks"
    (large reports warn but do not fail) and the report-data-model
    proposal's Failure Modes section. Exit code is not affected; the
    full Report still renders to stdout or ``--output``. The warning
    deliberately routes through ``typer.echo(..., err=True)`` so it
    cannot land on stdout and contaminate the rendered Report.
    """

    evidence_bytes = report.total_evidence_bytes()
    if evidence_bytes > _LARGE_REPORT_BYTES:
        size_mib = evidence_bytes / 1024 / 1024
        threshold_mib = _LARGE_REPORT_BYTES // 1024 // 1024
        typer.echo(
            f"warning: report evidence is {size_mib:.1f} MiB "
            f"(threshold {threshold_mib} MiB); output may be large",
            err=True,
        )


def _emit_output(rendered: str, output: str | None) -> None:
    """Write ``rendered`` to ``output`` (if given) or stdout.

    ``--output`` failures map to exit 3 (parameter / configuration error
    class) with the documented stderr prefix; stdout stays silent in
    that path so a partial file-or-stdout interleave never happens.

    ``output`` is accepted as ``str`` (Typer parameter type) rather than
    ``Path`` to keep the Typer Option default expression compatible with
    ruff B008 (which treats ``Path``-annotated defaults as suspect even
    when the default itself is ``typer.Option(...)``). We coerce to
    ``Path`` here for the actual write.
    """

    if output is None:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return

    out_path = Path(output)
    try:
        out_path.write_text(rendered if rendered.endswith("\n") else rendered + "\n")
    except OSError as exc:
        typer.echo(f"failed to write output: {exc}", err=True)
        raise typer.Exit(code=3) from exc


# --------------------------------------------------------------------------- #
# Intent (Planner Agent) path
# --------------------------------------------------------------------------- #


def _load_inspector_registry(settings: Settings) -> InspectorRegistry:
    """Assemble the InspectorRegistry for the Planner Agent's tool context.

    Per-file load errors are surfaced by ``hostlens inspectors list`` /
    ``doctor``; here a registry-level ``InspectorError`` is a usage/config
    failure (exit 3) with a single stderr line.
    """

    try:
        result = build_registry_from_search_paths(
            settings.inspectors_search_paths,
            settings=settings,
        )
    except InspectorError as exc:
        typer.echo(f"hostlens inspect: inspector registry error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    return result.registry


def _run_intent(target: str, intent: str, fmt: str, output: str | None, persist: bool) -> None:
    """Assemble + run the Planner → Diagnostician pipeline for ``--intent``.

    Live progress (the ``RichLiveObserver``) streams to stderr for BOTH the
    Planner and Diagnostician stages; the rendered ``Report`` goes to stdout (or
    ``--output``). The structlog reconfiguration is done inside the same
    restored-config guard as the ``--inspector`` path (the caller's
    ``try/finally`` in ``inspect_cmd``).

    ``run_intent_diagnosis`` returns a first-class ``Report`` (assembled from the
    per-run collector's full ``InspectorResult`` snapshot, with the
    Diagnostician's hypotheses / narrative projected in) or ``None`` on the
    **no-result** path (the collector is empty — zero ``InspectorResult``). On
    no-result there is no ``Report`` to render or persist (fabricating an empty
    skeleton is forbidden), so the CLI emits a one-line degrade note, writes
    nothing to stdout, skips persistence, and exits 2.

    When ``persist`` is set and a ``Report`` was produced, it is saved to the
    local ``ReportStore`` before rendering (so it is on disk even if a later
    render/emit step fails); an orphan degradation escalates the exit code to 2.

    Exit code (spec §需求, priority 3>2>1>0), mapped by
    ``_compute_intent_report_exit_code``:
      0 status=ok + no critical finding / 1 status=ok + ≥1 critical finding /
      2 status ∈ degraded set (``degraded_*`` / ``empty_response`` / ``partial``)
      / collector empty (no result) / persist orphan-degraded /
      3 backend not configured (ConfigError) / --output write failure.
    """

    # The whole body runs inside one boundary try so the assembly phase
    # (load_settings / inspector registry load / backend assembly / id stamping)
    # can never leak a Python traceback past the CLI surface — only the backend
    # ConfigError has a more specific (doctor-pointing) message, kept as an inner
    # handler. A fail-loud assembly error (e.g. the id-consistency invariant in
    # ``_assemble_report``) is caught by the blanket ``except Exception`` →
    # ``internal: ...`` → exit 2.
    try:
        settings = load_settings()
        configure_logging(settings.log_mode)
        _redirect_structlog_to_stderr()
        logger = structlog.get_logger(__name__)

        target_registry = _load_target_registry()
        # Resolve target up front so an unknown target fails fast (exit 3) before
        # any backend / Agent assembly.
        target_obj = _resolve_target(target_registry, target)
        inspector_registry = _load_inspector_registry(settings)

        observer = RichLiveObserver()
        try:
            result = asyncio.run(
                run_intent_diagnosis(
                    settings,
                    target,
                    intent,
                    target_registry,
                    inspector_registry,
                    logger,
                    target_type=target_obj.type,
                    observer=observer,
                )
            )
        except ConfigError as exc:
            # Two assembly-time ConfigError sources reach here: create_backend
            # (no/invalid backend block — raised with kind=None) and the lazy
            # DiagnosticianAgent prompt loader (kind="diagnostician_prompt_missing").
            # Only the former warrants the backend-specific "run doctor" hint; any
            # other kind gets a generic configuration-error message (both exit 3).
            if exc.kind is None:
                typer.echo(
                    f"hostlens inspect: backend not configured ({exc}); run 'hostlens doctor'",
                    err=True,
                )
            else:
                typer.echo(
                    f"hostlens inspect: configuration error ({exc})",
                    err=True,
                )
            raise typer.Exit(code=3) from exc
        finally:
            # fail-loud loop paths don't emit RunFinalized, so close the Live
            # region here regardless of success / degrade / raise.
            observer.close()

        if result is None:
            # No-result path: the collector was empty (zero InspectorResult — the
            # Planner never successfully ran an inspector, e.g. failed_api_unavailable
            # before any tool call, or the model never called run_inspector). No
            # Report → nothing to render or persist. Emit a one-line degrade reason,
            # leave stdout empty (no skeleton), skip persist, exit 2.
            typer.echo(
                "hostlens inspect: degraded run (no inspector results collected); "
                "no report produced",
                err=True,
            )
            raise typer.Exit(code=2)

        # Persist before rendering so the report is on disk even if a later
        # render/emit step fails (mirrors the --inspector path). A no-result run
        # (result is None) never reaches here, so persistence is never silently
        # skipped as a fake success.
        persist_failed = False
        orphaned = False
        if persist:
            try:
                orphaned = _persist_report(result)
            except Exception as exc:
                kind = type(exc).__name__
                typer.echo(f"internal: failed to persist report: {kind}: {exc}", err=True)
                persist_failed = True

        rendered = render_intent_report(result, fmt)
        _emit_output(rendered, output)

        exit_code = _compute_intent_report_exit_code(result)
        if (orphaned or persist_failed) and exit_code in (0, 1):
            exit_code = 2
        if exit_code != 0:
            status = result.meta.status if result.meta is not None else "unknown"
            if exit_code == 2 and status in _REPORT_DEGRADED_STATUSES:
                typer.echo(
                    f"hostlens inspect: degraded run (status={status})",
                    err=True,
                )
            raise typer.Exit(code=exit_code)
    except typer.Exit:
        # Re-raise verbatim so the explicit exit codes set above and inside
        # _resolve_target / _emit_output drive the exit status.
        raise
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        typer.echo("internal: cancelled: intent run interrupted", err=True)
        raise typer.Exit(code=2) from exc
    except ConfigError as exc:
        # Configuration errors from load_settings / inspector registry / agent
        # prompt loading (the backend-config ConfigError is handled by the more
        # specific inner branch above and re-raised as typer.Exit).
        typer.echo(f"hostlens inspect: configuration error ({exc})", err=True)
        raise typer.Exit(code=3) from exc
    except Exception as exc:
        # CLI boundary: any unexpected error (incl. non-retriable backend errors
        # passed through from a loop, e.g. CassetteMiss; incl. the fail-loud
        # id-consistency invariant raised by ``_assemble_report`` on a dangling
        # hypothesis reference) becomes one ``internal: <kind>: <msg>`` line —
        # never a Python traceback.
        kind = type(exc).__name__
        typer.echo(f"internal: {kind}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


# ``Report.meta.status`` values that map to the degraded exit code 2. Drawn from
# ReportStatus: every degraded_* plus empty_response PLUS ``partial`` — the
# latter is the new behavior vs the old DiagnosticianResult mapping
# (DiagnosticianResult.status is never ``partial``, but ``Report.meta.status``
# can be: ``_derive_report_status`` produces it when an inspector ran non-ok,
# e.g. target_unreachable). ``stored_as_orphan`` is not in this set — an orphan
# degradation escalates the exit code in _run_intent, not here.
# (``failed_api_unavailable`` is not a ReportStatus value — the no-result path
# handles it via a None Report in _run_intent.)
_REPORT_DEGRADED_STATUSES: frozenset[str] = frozenset(
    {
        "partial",
        "degraded_no_planner",
        "degraded_rate_limited",
        "degraded_token_budget",
        "degraded_max_turns",
        "empty_response",
    }
)


def _compute_intent_report_exit_code(report: Report) -> int:
    """Map the assembled ``--intent`` ``Report`` to the closed 4-value exit set.

    ``meta.status == ok`` + no critical finding → 0; ``meta.status == ok`` + ≥1
    critical finding → 1; any degraded ``meta.status`` (``degraded_*`` /
    ``empty_response`` / ``partial``) → 2. Exit 3 is owned by the caller paths
    (mutual-exclusion / backend config / --output write). Persist-orphan
    escalation to 2 is applied by the caller (_run_intent).

    The ``partial`` inclusion is the deliberate new behavior relative to the old
    ``_compute_diag_exit_code`` (which mapped a ``DiagnosticianResult.status``
    that is never ``partial``): a loop that finalized ok can still yield a
    ``partial`` Report when an inspector ran non-ok (``_derive_report_status``),
    and that degradation must surface as exit 2.

    Critical detection is on ``Report.findings`` (all id-bearing, flattened by
    the factory). ``meta`` is always present on the assembled --intent Report
    (the factory writes it); a None meta would be a legacy schema-1.0 load, not
    reachable here, so it is treated as a non-degraded ok for safety.
    """

    status = report.meta.status if report.meta is not None else ReportStatus.OK
    if status in _REPORT_DEGRADED_STATUSES:
        return 2
    if any(f.severity == "critical" for f in report.findings):
        return 1
    return 0


def _compute_intent_exit_code(result: PlannerResult) -> int:
    """Map ``PlannerResult`` to the closed 4-value exit set (design D-6).

    ``ok`` + no critical → 0; ``ok`` + ≥1 critical finding → 1; any non-``ok``
    terminal_status (degraded / failed / empty) → 2. Exit 3 is owned by the
    caller paths (mutual-exclusion / backend config / --output write).
    """

    if result.loop_result.terminal_status != "ok":
        return 2
    if any(f.severity == "critical" for f in result.findings):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Typer command
# --------------------------------------------------------------------------- #


def inspect_cmd(
    target: str = typer.Argument(
        ...,
        help="Target name (from `hostlens target list`).",
    ),
    inspector: str | None = typer.Option(
        None,
        "--inspector",
        "-i",
        help="Inspector name (from `hostlens inspectors list`). Mutually exclusive with --intent.",
    ),
    intent: str | None = typer.Option(
        None,
        "--intent",
        help="Natural-language inspection intent (drives the Planner Agent). "
        "Mutually exclusive with --inspector.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the rendered Report to FILE instead of stdout.",
    ),
    fmt: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: 'md' or 'json'.",
        click_type=click.Choice(["md", "json"]),
    ),
    parameters: str | None = typer.Option(
        None,
        "--parameters",
        "-p",
        help=(
            'Inspector parameters: inline JSON (\'{"k": "v"}\') or '
            "file reference ('@./params.json')."
        ),
    ),
    allow_privileged: bool = typer.Option(
        False,
        "--allow-privileged",
        help="Permit Inspectors with privilege!=none to run (opt-in).",
    ),
    timeout: int | None = typer.Option(
        None,
        "--timeout",
        help=(
            "Override manifest collect.timeout_seconds (integer in [1, 300]). "
            "Default: respect manifest value."
        ),
    ),
    persist: bool = typer.Option(
        False,
        "--persist",
        help=(
            "Persist the Report to the local SQLite store so "
            "`hostlens reports list/show/diff` can consume it. Supported on both "
            "--inspector (mechanical Report) and --intent (the Agent path now "
            "assembles a faithful Report from the run's inspector results, with "
            "root-cause hypotheses). A --intent no-result run (no inspectors "
            "collected) produces no Report and is not persisted."
        ),
    ),
) -> None:
    """Run one Inspector (or an --intent inspection) against one target and render a Report.

    Both --inspector and --intent now produce a first-class Report; the --intent
    Agent path assembles it from the run's collected inspector results and
    projects the Diagnostician's root-cause hypotheses into it.

    Exit codes:
      0: healthy (status ok, all findings <= warning)
      1: critical finding present (status ok)
      2: runner / degraded failure — --inspector: status != ok (timeout /
         target_unreachable / requires_unmet / exception); --intent:
         meta.status is degraded_* / empty_response / partial, or the run
         produced no inspector results (no-result), or a persist orphan
         degradation
      3: usage error (unknown target/inspector, bad --parameters, --output
         write failure, backend not configured for --intent)

    The body runs inside ``_with_restored_structlog_config`` so the stderr
    redirect installed by ``_dispatch`` never leaks past the command's
    lifetime. Without this guard the global ``structlog`` config keeps a
    reference to the redirect target (``sys.stderr`` at call time); when
    the CLI is exercised under pytest's ``CliRunner`` that stream is a
    capture buffer which gets closed at test teardown, and subsequent
    commands in the same process (the in-process pytest run) raise
    ``ValueError: I/O operation on closed file`` on the next log emit.
    """

    saved_structlog_config = structlog.get_config()
    try:
        # ---- 0a. --inspector / --intent mutual exclusion (design D-4) --- #
        # Exactly one must be provided. Both-missing / both-set are usage
        # errors (exit 3) raised explicitly here — not via Click usage
        # rewriting — with a single stderr line and no traceback.
        if inspector is None and intent is None:
            typer.echo(
                "hostlens inspect: must provide exactly one of --inspector or --intent",
                err=True,
            )
            raise typer.Exit(code=3)
        if inspector is not None and intent is not None:
            typer.echo(
                "hostlens inspect: --inspector and --intent are mutually exclusive", err=True
            )
            raise typer.Exit(code=3)

        if intent is not None:
            # --timeout only applies to the --inspector path (it overrides the
            # manifest collect timeout); the Agent's tool timeouts are fixed by
            # ToolSpec, so we ignore it here and warn (design D-6) — not error.
            if timeout is not None:
                typer.echo(
                    "hostlens inspect: --timeout has no effect with --intent; ignored",
                    err=True,
                )
            # --persist is now supported on the --intent path: it produces a
            # faithful first-class Report (assembled from the per-run collector's
            # InspectorResult snapshot), so it can be saved to the local store.
            # A no-result run (empty collector) produces no Report and is not
            # persisted (handled inside _run_intent).
            _run_intent(target, intent, fmt, output, persist)
            return

        # ---- 0. Validate / parse parameters at the CLI boundary --------- #
        # ``--format`` Choice rejection lands as a Typer UsageError handled
        # by the click-UsageError wrapper in __init__.py; we still defend
        # against an unexpected raw value here in case the wrapper grows.
        if fmt not in ("md", "json"):
            typer.echo(f"invalid --format: {fmt!r}; must be 'md' or 'json'", err=True)
            raise typer.Exit(code=3)

        timeout = _validate_timeout(timeout)
        parsed_parameters = _parse_parameters_option(parameters)

        # ---- 1. Resolve target + inspector ------------------------------ #
        # The 0a mutual-exclusion gate guarantees inspector is set on this path
        # (the intent branch returned above).
        assert inspector is not None
        target_registry = _load_target_registry()
        target_obj = _resolve_target(target_registry, target)
        manifest = _resolve_inspector(inspector)
        try:
            manifest_for_run = _apply_timeout_override(manifest, timeout)
        except ValidationError as exc:
            # Defense-in-depth: the CLI ``_validate_timeout`` boundary
            # already rejects out-of-range values, so this branch only
            # fires if a future regression bypasses that gate. We map
            # the Pydantic error to exit 3 (usage error class) with a
            # one-line stderr message — never let the traceback leak.
            errors = exc.errors()
            detail = errors[0].get("msg", str(exc)) if errors else str(exc)
            typer.echo(
                f"invalid --timeout: violates CollectSpec field constraint ({detail})",
                err=True,
            )
            raise typer.Exit(code=3) from exc

        # ---- 2. Run inspector ------------------------------------------- #
        started_at = datetime.now(UTC)
        try:
            inspector_result = asyncio.run(
                _dispatch(
                    manifest_for_run,
                    target_obj,
                    parsed_parameters,
                    allow_privileged=allow_privileged,
                    target_registry=target_registry,
                )
            )
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            # Spec §需求 (`hostlens inspect` 必须以 stdout/stderr 分离 +
            # "不输出 Python traceback"): wrap interruption as a one-line
            # internal error, never let the asyncio cancel surface raw.
            typer.echo("internal: cancelled: inspector run interrupted", err=True)
            raise typer.Exit(code=2) from exc
        except typer.Exit:
            # Re-raise typer.Exit verbatim so nested layers can drive the
            # exit code (e.g. _build_report ValidationError → exit 2).
            raise
        except Exception as exc:
            # Runner contract says it never raises business exceptions, but
            # programming bugs (e.g. a future regression) must still be
            # surfaced as a one-line error rather than a Python traceback.
            # ``except Exception`` is the documented CLI boundary catch (spec
            # §需求: 不输出 Python traceback) — every internal failure becomes
            # a single stderr line ``internal: <kind>: <msg>``.
            kind = type(exc).__name__
            typer.echo(f"internal: {kind}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        finished_at = datetime.now(UTC)

        # ---- 3. Build Report ------------------------------------------- #
        report = _build_report(target, target_obj.type, inspector_result, started_at, finished_at)

        # ---- 3b. Persist (opt-in, --inspector path only) --------------- #
        # Save before rendering so the report is on disk even if a later
        # render/emit step fails. A persist failure / orphan degradation
        # escalates the exit code to 2 only when the inspector-derived code is
        # 0 — a critical finding's exit 1 is preserved (see step 5) — and never
        # drops the report.
        persist_failed = False
        orphaned = False
        try:
            orphaned = _persist_report(report) if persist else False
        except Exception as exc:
            # CLI boundary catch — same documented pattern as the inspector
            # dispatch / intent paths. The store re-raises when both the SQLite
            # db and the orphan dir are unwritable (`OSError`), surfaces a
            # corrupt / programming `sqlite3.Error` it deliberately won't
            # masquerade as an orphan (a damaged `reports.db` makes
            # `PRAGMA journal_mode=WAL` raise `sqlite3.DatabaseError`), or any
            # other failure. Translate all of them to a single stderr line so
            # no Python traceback reaches the user; escalate to exit 2 below
            # without clobbering a non-zero business code.
            kind = type(exc).__name__
            typer.echo(f"internal: failed to persist report: {kind}: {exc}", err=True)
            persist_failed = True

        # ---- 4. Render + emit ------------------------------------------ #
        _maybe_warn_large_report(report)
        rendered = _render_report(report, fmt)
        _emit_output(rendered, output)

        # ---- 5. Exit ---------------------------------------------------- #
        exit_code = _compute_exit_code(inspector_result)
        if (orphaned or persist_failed) and exit_code == 0:
            exit_code = 2
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        # Restore the structlog global config snapshot taken on entry so
        # the stderr-bound logger factory installed by ``_dispatch`` (see
        # ``_redirect_structlog_to_stderr``) does not outlive this command
        # invocation. ``structlog.configure`` accepts the same keyword set
        # ``get_config`` returns, so the snapshot round-trips cleanly.
        structlog.configure(**saved_structlog_config)
