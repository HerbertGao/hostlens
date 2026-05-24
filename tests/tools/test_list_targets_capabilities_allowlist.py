"""Tests for `list_targets_handler` capability allowlist enforcement.

A target carrying a non-allowlisted capability (e.g.
`"internal_admin_root"`) must NOT have that capability surface to the
agent. The handler silently drops tokens outside
`CAPABILITY_ALLOWLIST` (defined in
`hostlens.tools.schemas.list_targets`).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import (
    CAPABILITY_ALLOWLIST,
    ListTargetsInput,
)


class _RawTarget:
    def __init__(
        self,
        *,
        name: str = "prod-web",
        kind: str = "ssh",
        capabilities: list[str],
    ) -> None:
        self.name = name
        self.kind = kind
        self.display_name: str | None = None
        self.description: str | None = None
        self.capabilities = capabilities
        self.tags: list[str] = []
        self.enabled = True


class _StubTargetRegistry:
    def __init__(self, targets: list[Any]) -> None:
        self._targets = targets

    def list_summaries(self) -> list[Any]:
        return list(self._targets)


class _StubInspectorRegistry:
    def list_summaries(self) -> list[Any]:
        return []


def _ctx_with(targets: list[Any]) -> ToolContext:
    return ToolContext(
        target_registry=_StubTargetRegistry(targets),
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_capabilities_allowlist"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def test_non_allowlisted_capability_is_dropped() -> None:
    raw = _RawTarget(
        capabilities=["shell", "file_read", "internal_admin_root"],
    )
    ctx = _ctx_with([raw])
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.capabilities == ["shell", "file_read"]
    assert "internal_admin_root" not in summary.capabilities


def test_allowlist_only_capabilities_survive() -> None:
    """Every capability we feed in is allowlisted; all should survive."""
    allowed = list(CAPABILITY_ALLOWLIST)
    raw = _RawTarget(capabilities=allowed)
    ctx = _ctx_with([raw])
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    # Order is preserved, every input survives.
    assert set(summary.capabilities) == CAPABILITY_ALLOWLIST


def test_all_non_allowlisted_capabilities_yield_empty_capabilities() -> None:
    raw = _RawTarget(capabilities=["internal_admin_root", "secret_capability"])
    ctx = _ctx_with([raw])
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.capabilities == []
