"""M2 default ToolSpec batch + `register_default_tools` assembly point.

This module is the single place where the M2 first-batch ToolSpecs are
declared (`run_inspector` / `list_inspectors` / `list_targets`) and the
single place where they are registered (`register_default_tools`).

Per CLAUDE.md §4.10 and design.md §D-3, `@tool` is a pure spec factory:
decoration does NOT mutate any module-level registry — assembly is
explicit, called once at agent loop startup.

Per design.md §D-11, `register_default_tools` is intentionally
non-idempotent: a duplicate call on the same registry raises
`ToolError`. Tests that need a clean registry must allocate a fresh
`ToolRegistry()`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel

from hostlens.tools.base import ToolContext
from hostlens.tools.decorators import tool
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.list_inspectors import (
    InspectorSummary,
    ListInspectorsInput,
    ListInspectorsOutput,
)
from hostlens.tools.schemas.list_targets import (
    CAPABILITY_ALLOWLIST,
    ListTargetsInput,
    ListTargetsOutput,
    TargetSummary,
    scrub_inventory_string,
)
from hostlens.tools.schemas.run_inspector import (
    FindingSummary,
    RunInspectorInput,
    RunInspectorOutput,
)

__all__ = [
    "list_inspectors",
    "list_targets",
    "register_default_tools",
    "run_inspector",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read `name` from `obj` whether it is a Pydantic model, dataclass,
    plain object, or any `collections.abc.Mapping` (covers `dict`,
    `os._Environ`, and other mapping types). M1 hasn't shipped the real
    `TargetRegistry` / `InspectorRegistry` summary types yet, so handlers
    accept any structurally-compatible object and fall back to mapping
    lookup. This is the only place we accept heterogeneity — once M1
    lands, summaries become typed and this helper can be deleted.
    """
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _scrub_capabilities(raw: list[str]) -> list[str]:
    """Filter capabilities down to `CAPABILITY_ALLOWLIST`.

    Non-allowlisted tokens (e.g. `"internal_admin_root"`) are silently
    dropped to prevent leaking internal capability names to the agent
    surface. Order of the input list is preserved for the survivors.
    """
    return [c for c in raw if c in CAPABILITY_ALLOWLIST]


# ---------------------------------------------------------------------------
# Handler: run_inspector
# ---------------------------------------------------------------------------


async def run_inspector_handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
    """M2 stub handler. M1 will replace this once `ExecutionTarget` +
    `Inspector` ship. For now we return a single placeholder finding so
    the demo path can exercise registry dispatch + adapter projection
    end-to-end.
    """
    # We deliberately ignore the registries: M1 isn't here yet. Touching
    # them is reserved for the M1 integration step.
    del ctx
    return RunInspectorOutput(
        target_name=args.target_name,
        inspector_name=args.inspector_name,
        findings=[
            FindingSummary(
                severity="info",
                message=(
                    f"stub finding for inspector {args.inspector_name!r} "
                    f"on target {args.target_name!r} (M1 handler not yet shipped)"
                ),
                evidence={},
            )
        ],
    )


# ---------------------------------------------------------------------------
# Handler: list_inspectors
# ---------------------------------------------------------------------------


async def list_inspectors_handler(
    args: ListInspectorsInput, ctx: ToolContext
) -> ListInspectorsOutput:
    """Read inspector summaries from `ctx.inspector_registry` and apply
    optional `tag` / `target_kind` filters.

    The handler trusts `ctx.inspector_registry.list_summaries()` to
    return objects exposing `name` / `version` / `description` / `tags`
    / `compatible_target_kinds` attributes (or matching dict keys).
    """
    raw_summaries = ctx.inspector_registry.list_summaries()
    summaries: list[InspectorSummary] = []
    for raw in raw_summaries:
        tags = list(_attr(raw, "tags", []))
        compatible = list(_attr(raw, "compatible_target_kinds", []))

        if args.tag is not None and args.tag not in tags:
            continue
        if args.target_kind is not None and args.target_kind not in compatible:
            continue

        summaries.append(
            InspectorSummary(
                name=str(_attr(raw, "name", "")),
                version=str(_attr(raw, "version", "")),
                description=str(_attr(raw, "description", "")),
                tags=tags,
                compatible_target_kinds=compatible,
            )
        )
    return ListInspectorsOutput(inspectors=summaries)


# ---------------------------------------------------------------------------
# Handler: list_targets
# ---------------------------------------------------------------------------


_STRING_FIELDS_FOR_SCRUB: tuple[str, ...] = (
    "name",
    "display_name",
    "description",
)


async def list_targets_handler(args: ListTargetsInput, ctx: ToolContext) -> ListTargetsOutput:
    """Project each raw target config down to a redacted `TargetSummary`.

    Every string field (`name` / `display_name` / `description` plus
    every `capabilities[*]` / `tags[*]`) MUST pass through
    `scrub_inventory_string` before reaching the agent surface; the
    handler NEVER calls `model_dump` on raw target config (that would
    leak credentials by definition).

    Skip semantics: if any scrubbed string field returns `None`, the
    whole target is dropped from the output (a half-revealed target is
    a worse leak than a missing row). A structured warning is logged
    with the reason code (e.g. `sensitive_substring_in_display_name`)
    but NOT the offending field value.
    """
    raw_targets = ctx.target_registry.list_summaries()
    summaries: list[TargetSummary] = []

    for raw in raw_targets:
        enabled = bool(_attr(raw, "enabled", True))
        if not enabled and not args.include_disabled:
            continue

        kind = _attr(raw, "kind", None)
        if kind not in ("local", "ssh", "docker", "k8s"):
            # Do NOT echo the raw `kind` value — it could leak attacker-
            # controlled config substrings into structured logs, contrary to
            # this handler's "reason code only, never the offending value"
            # contract. The reason code is enough to diagnose; full value
            # lives in the config file the caller already owns.
            ctx.logger.warning(
                "list_targets_skip",
                reason="unsupported_kind",
                kind_type=type(kind).__name__,
            )
            continue

        # Scrub scalar string fields. A None result means: drop this target.
        scrubbed: dict[str, str | None] = {}
        skip_this_target = False
        for field_name in _STRING_FIELDS_FOR_SCRUB:
            value = _attr(raw, field_name, None)
            if value is None:
                scrubbed[field_name] = None
                continue
            if not isinstance(value, str):
                # Hard reject non-strings on string-typed fields. The
                # spec assumes raw configs use strings here; anything
                # else is a config bug, not a leak.
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="non_string_field",
                    field_kind=field_name,
                )
                skip_this_target = True
                break
            cleaned = scrub_inventory_string(value, field_kind=field_name)
            if cleaned is None:
                ctx.logger.warning(
                    "list_targets_skip",
                    reason=f"sensitive_substring_in_{field_name}",
                )
                skip_this_target = True
                break
            scrubbed[field_name] = cleaned

        if skip_this_target:
            continue

        # Scrub list-of-string fields.
        raw_tags = list(_attr(raw, "tags", []))
        clean_tags: list[str] = []
        skip_for_tags = False
        for tag in raw_tags:
            if not isinstance(tag, str):
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="non_string_field",
                    field_kind="tags",
                )
                skip_for_tags = True
                break
            cleaned_tag = scrub_inventory_string(tag, field_kind="tags")
            if cleaned_tag is None:
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="sensitive_substring_in_tags",
                )
                skip_for_tags = True
                break
            clean_tags.append(cleaned_tag)
        if skip_for_tags:
            continue

        raw_caps = list(_attr(raw, "capabilities", []))
        clean_caps_pre_allowlist: list[str] = []
        skip_for_caps = False
        for cap in raw_caps:
            if not isinstance(cap, str):
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="non_string_field",
                    field_kind="capabilities",
                )
                skip_for_caps = True
                break
            cleaned_cap = scrub_inventory_string(cap, field_kind="capabilities")
            if cleaned_cap is None:
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="sensitive_substring_in_capabilities",
                )
                skip_for_caps = True
                break
            clean_caps_pre_allowlist.append(cleaned_cap)
        if skip_for_caps:
            continue

        # Strip non-allowlisted capability tokens silently (e.g.
        # "internal_admin_root").
        allowlisted_caps = _scrub_capabilities(clean_caps_pre_allowlist)

        # `scrubbed["name"]` is `None` only if the raw config had no
        # name; in that case the target is unidentifiable, skip it.
        clean_name = scrubbed.get("name")
        if clean_name is None:
            ctx.logger.warning("list_targets_skip", reason="missing_name")
            continue

        summaries.append(
            TargetSummary(
                name=clean_name,
                kind=kind,
                display_name=scrubbed.get("display_name"),
                description=scrubbed.get("description"),
                capabilities=allowlisted_caps,
                tags=clean_tags,
                enabled=enabled,
            )
        )

    return ListTargetsOutput(targets=summaries)


# ---------------------------------------------------------------------------
# ToolSpec definitions (pure spec factories — no global state mutated).
# ---------------------------------------------------------------------------

# Type alias matching the `@tool` decorator's narrow handler shape.
# Concrete handlers are typed against their specific input/output Pydantic
# models for IDE / mypy support inside the function body; we cast back to
# the broad shape at decoration time because `Callable` is contravariant
# in its argument types (a `RunInspectorInput`-typed handler is not a
# structural subtype of a `BaseModel`-typed handler). Runtime correctness
# is enforced by `ToolSpec`'s field validators, not by static types.
_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


run_inspector = tool(
    name="run_inspector",
    version="1.0.0",
    input_schema=RunInspectorInput,
    output_schema=RunInspectorOutput,
    agent_description=(
        "Run one inspector against one target and return the inspector's "
        "findings. Use this after picking a target with `list_targets` and "
        "an inspector with `list_inspectors`."
    ),
    mcp_description=(
        "Run one read-only inspector against one target. Output may "
        "contain process / port / connection metadata."
    ),
    cli_help=None,
    surfaces={"agent"},
    side_effects="read",
    sensitive_output=True,
    timeout=30.0,
)(cast(_BroadHandler, run_inspector_handler))


list_inspectors = tool(
    name="list_inspectors",
    version="1.0.0",
    input_schema=ListInspectorsInput,
    output_schema=ListInspectorsOutput,
    agent_description=(
        "List available inspectors with optional filtering by tag or "
        "compatible target kind. Use this to discover which inspectors "
        "can run against the targets you already know about."
    ),
    mcp_description=(
        "List available inspectors (project metadata). Each entry "
        "carries name / version / description / tags / compatible target "
        "kinds. No secrets."
    ),
    cli_help=None,
    surfaces={"agent"},
    side_effects="none",
    sensitive_output=False,
    timeout=5.0,
)(cast(_BroadHandler, list_inspectors_handler))


list_targets = tool(
    name="list_targets",
    version="1.0.0",
    input_schema=ListTargetsInput,
    output_schema=ListTargetsOutput,
    agent_description=(
        "List configured targets (hosts / containers / pods) with only "
        "the fields safe to expose: name / kind / capabilities / tags. "
        "Credentials and connection strings are never returned."
    ),
    mcp_description=(
        "List configured targets with a redacted summary (no "
        "credentials / hosts / ports). Even the redacted shape reveals "
        "environment structure — gate MCP exposure accordingly."
    ),
    cli_help=None,
    surfaces={"agent"},
    side_effects="none",
    sensitive_output=True,
    timeout=5.0,
)(cast(_BroadHandler, list_targets_handler))


# ---------------------------------------------------------------------------
# Explicit assembly
# ---------------------------------------------------------------------------


def register_default_tools(registry: ToolRegistry) -> None:
    """Register the M2 first-batch ToolSpecs into `registry`.

    Non-idempotent: calling twice on the same registry raises
    `ToolError` because `ToolRegistry.register` rejects duplicate names.
    Callers that need a clean state must allocate a fresh
    `ToolRegistry()`.
    """
    registry.register(run_inspector)
    registry.register(list_inspectors)
    registry.register(list_targets)
