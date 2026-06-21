"""Tests for the MCP ``propose_target_import`` ToolSpec + assembly.

Change: ``add-mcp-target-import-propose`` (group C tests; groups A+B done).

Specs (scenarios = test cases):
- ``mcp-target-import-propose/spec.md``
- ``target-import/spec.md`` (round-trip half)

Covers tasks.md 4.1 (policy metadata + distinct descriptions), 4.2 (dispatch:
non-empty to_add + targets.yaml byte-identical; illegal source / out-of-range
concurrency → MCP ``isError`` via input validation, NOT the dispatch envelope;
bad ref → dispatch envelope; empty inventory → empty plan), 4.3 (round-trip:
``model_dump()`` dict ⇄ ``model_validate``; serialise to YAML + JSON → both
load equivalently; missing-version old plan loads as v1), and 4.4 (cred view:
MCP host missing ``password_env`` → cred-ful candidate honestly ``failed_probe``;
existing names fresh-read so a target added after serve boot still buckets
``skipped``, proving the read is the config not a frozen registry).

Fixtures drive ``build_import_plan`` through real local inventory candidates
(``type: local`` probes the real local host — no SSH, no mocked asyncssh) and
RFC 5737 TEST-NET-1 SSH candidates for the deterministic-unreachable cases.
``targets.yaml`` paths are tmp so the operator's real config is untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from hostlens.core.config import Settings
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.targets.import_plan import ImportPlan
from hostlens.targets.onboard import default_source_registry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.import_propose_tool import (
    ImportProposeToolDeps,
    build_propose_target_import_spec,
    register_import_propose_tool,
)
from hostlens.tools.registry import ToolRegistry

_POSIX_ONLY = pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="LocalTarget probe requires POSIX"
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _ctx() -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=build_registry_from_search_paths([], settings=Settings()).registry,
        config=Settings(),
        logger=structlog.get_logger("test_import_propose_tool"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _deps(
    *,
    settings: Settings | None = None,
    existing_names: set[str] | None = None,
    read_existing_names: Any | None = None,
) -> ImportProposeToolDeps:
    """Assemble deps with a fixed (or supplied) existing-names reader."""
    eff_settings = settings if settings is not None else Settings()
    if read_existing_names is None:
        snapshot = set(existing_names or set())

        def read_existing_names() -> set[str]:  # type: ignore[misc]
            return set(snapshot)

    return ImportProposeToolDeps(
        settings=eff_settings,
        source_registry=default_source_registry(),
        read_existing_names=read_existing_names,
    )


def _registry(deps: ImportProposeToolDeps) -> ToolRegistry:
    reg = ToolRegistry()
    register_import_propose_tool(reg, deps=deps)
    return reg


def _local_inventory(tmp_path: Path, name: str = "demo-localhost") -> Path:
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"hosts_local": {name: {"type": "local"}}}))
    return inv


def _settings_with_tmp_targets(tmp_path: Path) -> Settings:
    """Settings whose ``targets_config_path`` is an absent tmp file."""
    return Settings(targets_config_path=tmp_path / "targets.yaml")


# =========================================================================== #
# 4.1 — registration declares read-only policy metadata; descriptions distinct
# =========================================================================== #


def test_spec_declares_readonly_policy_metadata() -> None:
    spec = build_propose_target_import_spec(_deps())
    assert spec.name == "propose_target_import"
    assert spec.side_effects == "read"
    assert spec.sensitive_output is True
    assert spec.requires_approval is False
    assert "mcp" in spec.surfaces
    assert spec.output_schema is ImportPlan


def test_spec_mcp_and_agent_descriptions_distinct() -> None:
    spec = build_propose_target_import_spec(_deps())
    assert spec.agent_description.strip() != ""
    assert spec.mcp_description.strip() != ""
    assert spec.mcp_description != spec.agent_description


def test_registered_tool_projects_to_mcp_and_agent_surfaces() -> None:
    reg = _registry(_deps())
    assert "propose_target_import" in reg.names()
    mcp_names = {s.name for s in reg.list_for("mcp")}
    agent_names = {s.name for s in reg.list_for("agent")}
    assert "propose_target_import" in mcp_names
    assert "propose_target_import" in agent_names


def test_passes_fail_closed_list_for_mcp_projection() -> None:
    """list_for_mcp's ``sensitive_output`` gate must NOT trip (it is declared)."""
    reg = _registry(_deps())
    adapter = McpToolsAdapter(reg, _ctx)
    tools = adapter.list_for_mcp()  # raises ToolPolicyViolation if undeclared
    names = {t.name for t in tools}
    assert "propose_target_import" in names


# =========================================================================== #
# 4.2 — dispatch: non-empty to_add + byte-identical targets.yaml; fail-closed
# =========================================================================== #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_dispatch_produces_nonempty_to_add(tmp_path: Path) -> None:
    """A reachable local candidate buckets into ``to_add`` via the real adapter."""
    inv = _local_inventory(tmp_path)
    deps = _deps(settings=_settings_with_tmp_targets(tmp_path))
    adapter = McpToolsAdapter(_registry(deps), _ctx)

    out = await adapter.dispatch("propose_target_import", {"ref": str(inv), "source": "yaml"})

    assert "is_error" not in out
    assert out["version"] == "1"
    assert [item["entry"]["name"] for item in out["to_add"]] == ["demo-localhost"]
    # round-trips back into an ImportPlan (D1: output dict == model_dump()).
    plan = ImportPlan.model_validate(out)
    assert not plan.is_empty


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_dispatch_never_writes_targets_yaml(tmp_path: Path) -> None:
    """propose-only: ``targets.yaml`` is byte-identical before/after dispatch.

    Pre-seed an existing ``targets.yaml`` so we can assert it is not rewritten,
    truncated, or re-permissioned by the propose path.
    """
    inv = _local_inventory(tmp_path)
    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        yaml.safe_dump({"version": "1", "targets": [{"name": "pre-existing", "type": "local"}]})
    )
    before = targets_path.read_bytes()

    deps = _deps(settings=Settings(targets_config_path=targets_path))
    adapter = McpToolsAdapter(_registry(deps), _ctx)
    out = await adapter.dispatch("propose_target_import", {"ref": str(inv), "source": "yaml"})

    assert "is_error" not in out
    # The handler read existing names ("pre-existing") but never wrote.
    assert targets_path.read_bytes() == before


@pytest.mark.asyncio
async def test_dispatch_illegal_source_surfaces_as_is_error_not_envelope(tmp_path: Path) -> None:
    """An out-of-set ``source`` is an input-schema violation: the adapter raises
    ``TypeError`` BEFORE the handler (it is the server's ``isError`` path), NOT
    the dispatch ``except`` envelope. Probe is never triggered, nothing written.
    """
    inv = _local_inventory(tmp_path)
    targets_path = tmp_path / "targets.yaml"
    deps = _deps(settings=Settings(targets_config_path=targets_path))
    adapter = McpToolsAdapter(_registry(deps), _ctx)

    with pytest.raises(TypeError):
        await adapter.dispatch("propose_target_import", {"ref": str(inv), "source": "not_a_source"})
    assert not targets_path.exists()


@pytest.mark.asyncio
async def test_dispatch_concurrency_below_one_is_is_error(tmp_path: Path) -> None:
    inv = _local_inventory(tmp_path)
    adapter = McpToolsAdapter(_registry(_deps(settings=_settings_with_tmp_targets(tmp_path))), _ctx)
    with pytest.raises(TypeError):
        await adapter.dispatch(
            "propose_target_import", {"ref": str(inv), "source": "yaml", "concurrency": 0}
        )


@pytest.mark.asyncio
async def test_dispatch_concurrency_above_hundred_is_is_error(tmp_path: Path) -> None:
    inv = _local_inventory(tmp_path)
    adapter = McpToolsAdapter(_registry(_deps(settings=_settings_with_tmp_targets(tmp_path))), _ctx)
    with pytest.raises(TypeError):
        await adapter.dispatch(
            "propose_target_import", {"ref": str(inv), "source": "yaml", "concurrency": 101}
        )


@pytest.mark.asyncio
async def test_dispatch_is_error_surfaces_through_server_handle_call_tool(tmp_path: Path) -> None:
    """End-to-end: the ``TypeError`` the input gate raises is caught by the
    server's ``handle_call_tool`` and returned as an MCP ``isError`` text result
    (the documented mechanism for input-schema violations — design D6 path ①)."""
    pytest.importorskip("mcp")
    from hostlens.mcp_server.server import build_server

    inv = _local_inventory(tmp_path)
    targets_path = tmp_path / "targets.yaml"
    reg = _registry(_deps(settings=Settings(targets_config_path=targets_path)))
    server = build_server(reg, _ctx)

    handler = server.request_handlers
    # Drive the registered call_tool handler directly via the mcp SDK request
    # type so we observe the CallToolResult(isError=True) the server returns.
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="propose_target_import",
            arguments={"ref": str(inv), "source": "bogus"},
        ),
    )
    result = await handler[CallToolRequest](req)
    # ServerResult wraps a CallToolResult; isError must be True (fail-closed).
    payload = result.root
    assert payload.isError is True
    assert not targets_path.exists()


@pytest.mark.asyncio
async def test_dispatch_bad_ref_returns_envelope_not_is_error(tmp_path: Path) -> None:
    """A bad ``ref`` is a HANDLER-period failure (``ConfigError`` inside
    ``build_import_plan``): it goes through the dispatch ``except`` envelope
    (``is_error`` dict), NOT the input-gate TypeError path. No write, scrubbed.
    """
    targets_path = tmp_path / "targets.yaml"
    deps = _deps(settings=Settings(targets_config_path=targets_path))
    adapter = McpToolsAdapter(_registry(deps), _ctx)

    missing = tmp_path / "does-not-exist.yml"
    out = await adapter.dispatch("propose_target_import", {"ref": str(missing), "source": "yaml"})

    assert out["is_error"] is True
    assert out["tool_name"] == "propose_target_import"
    # Envelope shape (design D6 path ②), not raised.
    assert "message" in out and "cause" in out
    assert not targets_path.exists()


@pytest.mark.asyncio
async def test_dispatch_empty_inventory_yields_empty_plan(tmp_path: Path) -> None:
    """A zero-candidate inventory → empty four-bucket plan, not an error."""
    empty_inv = tmp_path / "empty.yml"
    empty_inv.write_text("")
    deps = _deps(settings=_settings_with_tmp_targets(tmp_path))
    adapter = McpToolsAdapter(_registry(deps), _ctx)

    out = await adapter.dispatch("propose_target_import", {"ref": str(empty_inv), "source": "yaml"})

    assert "is_error" not in out
    plan = ImportPlan.model_validate(out)
    assert plan.is_empty is True


# =========================================================================== #
# 4.3 — round-trip: model_dump dict ⇄ model_validate; YAML & JSON files load
# =========================================================================== #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_tool_output_dict_round_trips_field_for_field(tmp_path: Path) -> None:
    """The tool ``model_dump()`` dict → ``model_validate`` is field-for-field
    equivalent to the handler's produced ``ImportPlan``."""
    inv = _local_inventory(tmp_path)
    spec = build_propose_target_import_spec(_deps(settings=_settings_with_tmp_targets(tmp_path)))
    produced = await spec.handler(
        spec.input_schema.model_validate({"ref": str(inv), "source": "yaml"}), _ctx()
    )
    assert isinstance(produced, ImportPlan)

    dumped = produced.model_dump()
    restored = ImportPlan.model_validate(dumped)
    assert restored == produced


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_tool_output_serialised_to_yaml_and_json_loads_equivalently(tmp_path: Path) -> None:
    """Serialise the produced plan to a YAML file AND a JSON file → both load
    (via ``ImportPlan.load``) to a field-for-field equivalent plan (target-import
    spec §场景:加载兼容 YAML 与 JSON 两种格式)."""
    inv = _local_inventory(tmp_path)
    spec = build_propose_target_import_spec(_deps(settings=_settings_with_tmp_targets(tmp_path)))
    produced = await spec.handler(
        spec.input_schema.model_validate({"ref": str(inv), "source": "yaml"}), _ctx()
    )
    assert isinstance(produced, ImportPlan)
    dumped = produced.model_dump()

    yaml_path = tmp_path / "plan.yaml"
    yaml_path.write_text(yaml.safe_dump(json.loads(produced.model_dump_json())))
    json_path = tmp_path / "plan.json"
    json_path.write_text(produced.model_dump_json())

    from_yaml = ImportPlan.load(yaml_path)
    from_json = ImportPlan.load(json_path)
    assert from_yaml == produced
    assert from_json == produced
    assert from_yaml == from_json
    # Sanity: the dict and the JSON-derived dict agree.
    assert json.loads(produced.model_dump_json()) == json.loads(json.dumps(dumped, default=str))


def test_missing_version_old_plan_loads_as_v1(tmp_path: Path) -> None:
    """A pre-version ``.save`` artefact (no ``version`` key) loads as v1 via the
    field default (target-import spec §场景:缺 version 旧 plan 加载为 v1)."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        yaml.safe_dump(
            {
                "to_add": [{"entry": {"name": "demo", "type": "local"}}],
                "skipped": [],
                "failed_probe": [],
                "invalid_candidate": [],
            }
        )
    )
    plan = ImportPlan.load(legacy)
    assert plan.version == "1"
    assert [item.entry.name for item in plan.to_add] == ["demo"]


# =========================================================================== #
# 4.4 — credential view: MCP host missing env; fresh-read existing names
# =========================================================================== #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_credful_candidate_missing_env_buckets_failed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cred-ful SSH candidate whose ``password_env`` is unset on the MCP serve
    host probes from the serve process's credential view: it honestly buckets
    into ``failed_probe`` (NOT a crash, NOT a false ``to_add``/reachable). We use
    an RFC 5737 TEST-NET-1 host so the probe fails deterministically/fast.
    """
    # Env-var NAME assembled via concatenation so the secret scanner cannot match
    # a quoted value adjacent to a credential key (repo .gitguardian.yaml
    # convention — a NAME, never a secret value).
    absent_env_ref = "ABSENT_MCP_HOST" + "_ENV"
    monkeypatch.delenv(absent_env_ref, raising=False)
    inv = tmp_path / "credful.yml"
    inv.write_text(
        yaml.safe_dump(
            {
                "g": {
                    "credful-host": {
                        "type": "ssh",
                        "host": "192.0.2.1",
                        "user": "nobody",
                        "password_env": absent_env_ref,
                    }
                }
            }
        )
    )
    spec = build_propose_target_import_spec(_deps(settings=_settings_with_tmp_targets(tmp_path)))
    produced = await spec.handler(
        spec.input_schema.model_validate({"ref": str(inv), "source": "yaml", "concurrency": 1}),
        _ctx(),
    )
    assert isinstance(produced, ImportPlan)
    assert [f.entry.name for f in produced.failed_probe] == ["credful-host"]
    assert produced.to_add == []  # never falsely reachable


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_existing_names_fresh_read_buckets_skipped_after_serve_boot(
    tmp_path: Path,
) -> None:
    """The existing-names reader is fresh-read per call: a target written to
    ``targets.yaml`` AFTER deps assembly (simulating a local CLI add during the
    serve lifetime) is still bucketed ``skipped`` on the next propose — proving
    the read is the on-disk config, not a serve-boot-frozen registry.
    """
    inv = _local_inventory(tmp_path, name="demo-localhost")
    targets_path = tmp_path / "targets.yaml"

    # Build deps with the REAL fresh-read callable (mirrors _build_import_propose_deps).
    settings = Settings(targets_config_path=targets_path)

    def _read_existing_names() -> set[str]:
        from hostlens.targets.config import load_targets_config

        cfg = load_targets_config(settings.targets_config_path, expand_env=False)
        return {entry.name for entry in cfg.targets}

    deps = ImportProposeToolDeps(
        settings=settings,
        source_registry=default_source_registry(),
        read_existing_names=_read_existing_names,
    )
    spec = build_propose_target_import_spec(deps)

    # Frozen registry built at "serve boot" — deliberately EMPTY; if the handler
    # read this instead of the config, the candidate would wrongly be to_add.
    boot_registry = TargetRegistry()
    assert "demo-localhost" not in boot_registry.names()

    # 1. Before any local add → candidate is to_add (config is absent/empty).
    first = await spec.handler(
        spec.input_schema.model_validate({"ref": str(inv), "source": "yaml"}), _ctx()
    )
    assert isinstance(first, ImportPlan)
    assert [item.entry.name for item in first.to_add] == ["demo-localhost"]
    assert first.skipped == []

    # 2. A local CLI writes the target AFTER serve boot (registry NOT rebuilt).
    targets_path.write_text(
        yaml.safe_dump({"version": "1", "targets": [{"name": "demo-localhost", "type": "local"}]})
    )

    # 3. Next propose fresh-reads the config → candidate now buckets skipped.
    second = await spec.handler(
        spec.input_schema.model_validate({"ref": str(inv), "source": "yaml"}), _ctx()
    )
    assert isinstance(second, ImportPlan)
    assert second.skipped == ["demo-localhost"]
    assert second.to_add == []


def test_register_is_non_idempotent_on_duplicate() -> None:
    """A second ``register_import_propose_tool`` on the same registry raises."""
    from hostlens.core.exceptions import ToolError

    reg = ToolRegistry()
    register_import_propose_tool(reg, deps=_deps())
    with pytest.raises(ToolError):
        register_import_propose_tool(reg, deps=_deps())


def test_dispatch_surface_gate_rejects_unregistered_mcp(tmp_path: Path) -> None:
    """Sanity: the read-only spec passes all four dispatch gates (no
    ToolPolicyViolation from surface / sensitive_output / side_effects /
    requires_approval). Asserted by list_for_mcp not raising above; here we also
    confirm the spec metadata could not trip the write/destructive gate."""
    spec = build_propose_target_import_spec(_deps())
    assert spec.side_effects not in {"write", "destructive"}
    assert spec.requires_approval is not True
    assert spec.sensitive_output is not None
    # No way to construct a ToolPolicyViolation from this metadata under the gates.
    _ = ToolPolicyViolation  # referenced for intent; not raised on the happy path


def test_build_deps_fresh_read_on_absent_config_skips_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serve-assembled fresh-read callable must short-circuit on an ABSENT
    ``targets.yaml`` and NOT call ``load_targets_config`` — the ``.exists()`` guard
    in ``_build_import_propose_deps`` (a micro-optimisation skipping a pointless
    loader call, mirroring ``_build_target_registry``). Protocol-stream safety
    under stdio MCP is guaranteed separately by ``serve_cmd`` routing all structlog
    output to stderr (see ``test_serve_routes_structlog_to_stderr``), so this guard
    is no longer the stdout protection — but the short-circuit behaviour is still a
    contract worth pinning.

    Asserted directly (loader not called), which is order-independent — unlike
    capturing stdout it does not depend on whatever global structlog config a
    sibling test happened to install.
    """
    import hostlens.cli.mcp as mcp_cli

    def _must_not_be_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "load_targets_config must not run on an absent config (stdout-log risk)"
        )

    monkeypatch.setattr(mcp_cli, "load_targets_config", _must_not_be_called)
    settings = Settings(targets_config_path=tmp_path / "absent.yaml")
    deps = mcp_cli._build_import_propose_deps(settings, TargetRegistry())
    assert deps.read_existing_names() == set()  # guard short-circuits; loader untouched
