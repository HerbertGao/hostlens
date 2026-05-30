"""Tests for the M1-integrated `list_inspectors` ToolSpec handler.

Covers the inspector-plugin-system spec MODIFIED block in
`tool-registry-capability-layer/spec.md` §需求:M2 首批 ToolSpec... §场景:
list_inspectors handler:

(a) Real registry containing the M1 builtins (`hello.echo` +
    `system.uptime`) plus the M2.8 incident-pack →
    `ListInspectorsOutput.inspectors` includes the M1 entries, sorted by
    name in dictionary order; `tags` / `compatible_target_kinds` are
    themselves sorted dictionary order (prompt-cache prefix stability).
(b) `tag="linux"` filter includes `system.uptime` (which carries the
    `linux` tag) and excludes `hello.echo` (which carries `demo` /
    `hello`).
(c) `target_kind="ssh"` filter includes both M1 builtins (all builtins
    declare `targets: [local, ssh]`).
(d) No filter returns the full set, sorted ascending by name.
"""

from __future__ import annotations

import asyncio

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_inspectors_handler
from hostlens.tools.schemas.list_inspectors import (
    ListInspectorsInput,
    ListInspectorsOutput,
)


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _ctx(inspector_registry: InspectorRegistry) -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_list_inspectors"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# (a) No filter — both builtins, sorted ascending; nested lists sorted too
# ---------------------------------------------------------------------------


def test_list_inspectors_no_filter_returns_both_builtins_sorted() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(), ctx)

    output = asyncio.run(go())
    names = [summary.name for summary in output.inspectors]
    # The two M1 builtins are always present; the M2.8 incident-pack adds
    # more. Pin presence + ascending sort rather than the exact set so the
    # test does not drift every time a builtin is added.
    assert {"hello.echo", "system.uptime"} <= set(names)
    assert names == sorted(names)
    for summary in output.inspectors:
        assert summary.tags == sorted(summary.tags)
        assert summary.compatible_target_kinds == sorted(summary.compatible_target_kinds)


# ---------------------------------------------------------------------------
# (b) tag="linux" → includes system.uptime, excludes hello.echo
# ---------------------------------------------------------------------------


def test_list_inspectors_tag_linux_filters_to_linux_tagged_only() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(tag="linux"), ctx)

    output = asyncio.run(go())
    names = [s.name for s in output.inspectors]
    # `system.uptime` carries the `linux` tag; `hello.echo` (demo/hello)
    # does not. The incident-pack adds more `linux`-tagged inspectors, so
    # pin the filter behaviour (include uptime, exclude echo) instead of
    # the exact set.
    assert "system.uptime" in names
    assert "hello.echo" not in names
    for summary in output.inspectors:
        assert "linux" in summary.tags


# ---------------------------------------------------------------------------
# (c) target_kind="ssh" → both builtins
# ---------------------------------------------------------------------------


def test_list_inspectors_target_kind_ssh_returns_both() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(target_kind="ssh"), ctx)

    output = asyncio.run(go())
    names = [s.name for s in output.inspectors]
    # Both M1 builtins declare `targets: [local, ssh]`; so do all
    # incident-pack inspectors. Pin presence + ascending sort.
    assert {"hello.echo", "system.uptime"} <= set(names)
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# (d) Unknown tag → empty
# ---------------------------------------------------------------------------


def test_list_inspectors_unknown_tag_returns_empty() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(tag="nonexistent"), ctx)

    output = asyncio.run(go())
    assert output.inspectors == []
