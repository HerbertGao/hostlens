"""MCP `propose_target_import` ToolSpec + `register_import_propose_tool` assembly.

This module declares the read-only `propose_target_import` ToolSpec and the
single explicit assembly function that registers it. It mirrors
`tools/management_tools.py`:

- `@tool` is a pure spec factory: declaring a spec mutates **no**
  module-level registry (CLAUDE.md §4.10 rule 3).
- Handler dependencies arrive via **closure injection** through
  `ImportProposeToolDeps`, never through `ToolContext` (frozen at its
  ADR-008 six-field set) and never through a module-level singleton
  (design D4).

The tool projects proposal A's read-only `build_import_plan` orchestration
(source → promote → probe → classify) into an `ImportPlan` and **never**
writes `targets.yaml`: it is `side_effects="read"` (the probe is a read-only
remote exec; reading existing target names is a read), so it never touches
the adapter's write/destructive rejection gate (design D5) — propose-only,
the landing half stays in the local CLI `--from-plan --yes` (roadmap §5 /
M9 "give a plan, don't auto-execute").

`output_schema` is `ImportPlan` **directly** (not wrapped): the handler
returns an `ImportPlan`, which `McpToolsAdapter.dispatch` validates via
`isinstance(result, ImportPlan)` then serialises with `model_dump()`
(design D1). The output dict round-trips through `ImportPlan.model_validate`,
so a client can serialise it to YAML/JSON and hand it to
`hostlens target import --from-plan` to land locally (propose→land closure).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from hostlens.targets.import_plan import ImportPlan
from hostlens.targets.onboard import build_import_plan
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.propose_target_import import ProposeTargetImportInput

if TYPE_CHECKING:
    from hostlens.core.config import Settings
    from hostlens.targets.inventory.base import InventorySourceRegistry

__all__ = [
    "ImportProposeToolDeps",
    "build_propose_target_import_spec",
    "register_import_propose_tool",
]

# Ref-mode default probe concurrency, mirroring `cli/target.py:import_cmd`'s
# `probe_concurrency = concurrency if concurrency is not None else 10`. When the
# MCP caller omits `concurrency`, the handler derives this same default.
_DEFAULT_CONCURRENCY = 10


# ---------------------------------------------------------------------------
# Dependency container (design D4) — closure-injected, NOT a ToolContext field
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportProposeToolDeps:
    """Frozen dependency container for the `propose_target_import` tool.

    Carries the import-pipeline dependencies the handler needs that do **not**
    live on `ToolContext`. Injected once at serve assembly via
    `register_import_propose_tool(registry, deps=...)` and closure-bound into
    the handler.

    - `settings`: feeds `build_import_plan` (and through it `TargetProbe`). The
      probe resolves `password_env` / `passphrase_env` from the running
      process's `os.environ` (`onboard._resolve_probe_entry`), so the propose
      probe's credential view is the **`hostlens mcp serve` process view**: a
      cred-ful candidate whose env is unset on the MCP host honestly buckets
      into `failed_probe` (not a crash, not a false reachable).
    - `source_registry`: the inventory source registry (`default_source_registry()`).
    - `read_existing_names`: a **fresh-read** callable returning the current
      `targets.yaml` name set on every call (it re-reads
      `load_targets_config(settings.targets_config_path, expand_env=False)`),
      used to bucket already-managed candidates into `skipped`. It is NOT
      `target_registry.names()` — that reads the registry frozen once at serve
      boot, which misses targets a local CLI added during the serve lifetime.
      The read is read-only and does NOT change `side_effects="read"`.
    """

    settings: Settings
    source_registry: InventorySourceRegistry
    read_existing_names: Callable[[], set[str]]


# ---------------------------------------------------------------------------
# Broad handler alias (matches `@tool` contravariant handler shape; see
# management_tools.py / default_tools.py for the same cast rationale).
# ---------------------------------------------------------------------------

_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


# ---------------------------------------------------------------------------
# Handler (closure-bound to `deps` by the spec builder below)
# ---------------------------------------------------------------------------


async def _propose_target_import_handler(
    args: ProposeTargetImportInput, ctx: ToolContext, *, deps: ImportProposeToolDeps
) -> ImportPlan:
    """Run the read-only import pipeline and return the four-bucket plan.

    Fresh-reads the existing target names (`deps.read_existing_names`) so the
    `skipped` bucket reflects targets added since serve boot, then delegates to
    `build_import_plan` (source → promote → probe → classify). The handler
    **never** calls `save_targets_config` — propose-only.

    A bad `ref` raises `ConfigError` inside `build_import_plan`; it propagates
    to `McpToolsAdapter.dispatch`'s general `except`, which scrubs it into a
    structured error envelope (the handler does NOT catch it). An empty
    inventory naturally yields an empty `ImportPlan`. Non-`ssh_config`/`yaml`
    `source` and out-of-`[1,100]` `concurrency` never reach here: the adapter's
    input-validation step rejects them earlier (→ `TypeError` → MCP `isError`).
    """

    del ctx
    concurrency = args.concurrency if args.concurrency is not None else _DEFAULT_CONCURRENCY
    return await build_import_plan(
        args.ref,
        source=args.source,
        settings=deps.settings,
        existing_names=deps.read_existing_names(),
        concurrency=concurrency,
        registry=deps.source_registry,
    )


# ---------------------------------------------------------------------------
# Spec builder — closure-binds `deps` into the handler, then wraps it with
# `@tool`. Mirrors `build_run_schedule_now_spec` in management_tools.py.
# ---------------------------------------------------------------------------


def build_propose_target_import_spec(deps: ImportProposeToolDeps) -> ToolSpec:
    async def _handler(args: ProposeTargetImportInput, ctx: ToolContext) -> ImportPlan:
        return await _propose_target_import_handler(args, ctx, deps=deps)

    return tool(
        name="propose_target_import",
        version="1.0.0",
        input_schema=ProposeTargetImportInput,
        output_schema=ImportPlan,
        agent_description=(
            "Propose a target-import plan from an inventory ref (ssh_config / "
            "yaml). Parses, promotes, probes, and classifies candidates into "
            "to_add / skipped / failed_probe / invalid_candidate, returning the "
            "full ImportPlan WITHOUT writing targets.yaml. The plan is "
            "propose-only: landing it always happens locally via 'hostlens "
            "target import --from-plan --yes'."
        ),
        mcp_description=(
            "Propose (never land) a target-import plan from an inventory ref. "
            "Resolves the source (ssh_config / yaml, optionally pinned via "
            "source), promotes each candidate, probes reachability, and returns "
            "a four-bucket ImportPlan (to_add / skipped / failed_probe / "
            "invalid_candidate). This tool NEVER writes targets.yaml — landing "
            "is a separate local step ('hostlens target import --from-plan "
            "--yes' on a serialised copy of this plan). The output carries "
            "to_add host addresses (a lateral-movement map): treat it as "
            "sensitive. Reachability is judged from the MCP serve process's "
            "credential view — a cred-ful host whose password_env is unset on "
            "the serve host buckets into failed_probe, so for credentialed "
            "hosts prefer running 'hostlens target import <ref>' locally where "
            "the credential env resolves."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="read",
        sensitive_output=True,
        timeout=120.0,
    )(cast(_BroadHandler, _handler))


# ---------------------------------------------------------------------------
# Explicit assembly
# ---------------------------------------------------------------------------


def register_import_propose_tool(registry: ToolRegistry, *, deps: ImportProposeToolDeps) -> None:
    """Register the `propose_target_import` ToolSpec into `registry`.

    Closure-injects `deps` into the handler (design D4). Non-idempotent: a
    duplicate call on the same registry raises `ToolError` (duplicate name).
    """
    registry.register(build_propose_target_import_spec(deps))
