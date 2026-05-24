"""Shared pytest fixtures for the Hostlens test suite.

`tool_registry` and `tool_context_factory` are the M2 fixtures used by
multiple test modules — each test that depends on them receives an
independent instance (function scope, no module-level state).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


class _StubTargetSummary:
    """Minimal stub matching the structural shape `list_targets_handler`
    expects (name / kind / display_name / description / capabilities /
    tags / enabled). Tests can override via fixture parameterization.
    """

    def __init__(
        self,
        *,
        name: str = "stub-target",
        kind: str = "local",
        display_name: str | None = None,
        description: str | None = None,
        capabilities: list[str] | None = None,
        tags: list[str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.kind = kind
        self.display_name = display_name
        self.description = description
        self.capabilities = capabilities or []
        self.tags = tags or []
        self.enabled = enabled


class _StubInspectorSummary:
    """Minimal stub matching `list_inspectors_handler`'s expected
    attribute shape.
    """

    def __init__(
        self,
        *,
        name: str = "stub-inspector",
        version: str = "1.0.0",
        description: str = "stub inspector for tests",
        tags: list[str] | None = None,
        compatible_target_kinds: list[str] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.tags = tags or []
        self.compatible_target_kinds = compatible_target_kinds or []


class _StubTargetRegistry:
    """Default stub: one safe target so `list_targets_handler` returns
    a non-empty list under the fixture.
    """

    def __init__(self, targets: list[Any] | None = None) -> None:
        self._targets = targets if targets is not None else [_StubTargetSummary()]

    def list_summaries(self) -> list[Any]:
        return list(self._targets)


class _StubInspectorRegistry:
    """Default stub: one safe inspector so `list_inspectors_handler`
    returns a non-empty list under the fixture.
    """

    def __init__(self, inspectors: list[Any] | None = None) -> None:
        self._inspectors = inspectors if inspectors is not None else [_StubInspectorSummary()]

    def list_summaries(self) -> list[Any]:
        return list(self._inspectors)


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """A fresh `ToolRegistry` with the M2 default ToolSpec batch
    pre-registered. Each test receives its own instance — mutating the
    fixture cannot leak to other tests.
    """
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


@pytest.fixture
def tool_context_factory() -> Callable[..., ToolContext]:
    """Return a callable that produces a fresh `ToolContext` per call.

    Each invocation allocates new stub registries, a new
    `asyncio.Event`, and a new `NoopApprovalService`. Callers can pass
    `target_registry=` / `inspector_registry=` to override the defaults
    while keeping the other dependencies stub-provided.
    """

    def _make(
        *,
        target_registry: Any | None = None,
        inspector_registry: Any | None = None,
    ) -> ToolContext:
        return ToolContext(
            target_registry=target_registry or _StubTargetRegistry(),
            inspector_registry=inspector_registry or _StubInspectorRegistry(),
            config=Settings(),
            logger=structlog.get_logger("tool_context_factory"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make
