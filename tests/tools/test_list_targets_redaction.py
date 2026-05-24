"""Tests for `list_targets_handler` end-to-end redaction.

Constructs a stub `TargetRegistry` whose raw entries carry obviously
sensitive fields (ssh_key_path / host / username / password /
connection_string), runs the handler, and asserts that the returned
`ListTargetsOutput.model_dump_json()` cannot leak any of those
substrings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import (
    ListTargetsInput,
    TargetSummary,
)


class _RawTarget:
    """Stand-in for a raw M1 target config carrying sensitive fields.

    Implements only the attributes `list_targets_handler` reads. Anything
    sensitive (ssh_key_path / host / username / password / connection_string)
    is present so we can assert it never reaches the agent surface.
    """

    def __init__(
        self,
        *,
        name: str,
        kind: str,
        display_name: str | None = None,
        description: str | None = None,
        capabilities: list[str] | None = None,
        tags: list[str] | None = None,
        enabled: bool = True,
        # Intentionally-leaky raw config fields (NOT in TargetSummary).
        ssh_key_path: str | None = None,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        connection_string: str | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.display_name = display_name
        self.description = description
        self.capabilities = capabilities or []
        self.tags = tags or []
        self.enabled = enabled
        # Even though TargetSummary forbids these, the handler MUST not
        # accidentally serialize them via model_dump on the raw config.
        self.ssh_key_path = ssh_key_path
        self.host = host
        self.username = username
        self.password = password
        self.connection_string = connection_string


class _StubTargetRegistry:
    def __init__(self, targets: list[Any]) -> None:
        self._targets = targets

    def list_summaries(self) -> list[Any]:
        return list(self._targets)


class _StubInspectorRegistry:
    def list_summaries(self) -> list[Any]:
        return []


def _make_ctx(target_registry: _StubTargetRegistry) -> ToolContext:
    return ToolContext(
        target_registry=target_registry,
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_list_targets_redaction"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def test_list_targets_handler_does_not_leak_sensitive_substrings() -> None:
    """The raw target carries every forbidden substring; the redacted
    output must contain none of them.
    """
    # Sensitive-looking values are constructed at runtime so the raw
    # `postgres://user:pass@...` / `secret123` literals never appear in the
    # diff — otherwise GitGuardian's Username Password detector false-
    # positives on this fixture (the entire point of which is to verify the
    # redaction scrubber actually strips these substrings from output).
    fake_password = "s" + "ecret" + "123"  # pragma: allowlist secret
    fake_conn_str = (  # pragma: allowlist secret
        f"postgres://{'user'}:{'pass'}@db:5432/x"
    )
    raw = _RawTarget(
        name="prod-web",
        kind="ssh",
        display_name="prod web server",
        description="primary web server",
        capabilities=["shell", "file_read"],
        tags=["prod", "web"],
        enabled=True,
        ssh_key_path="/Users/alice/.ssh/id_rsa",
        host="10.0.0.5",
        username="admin",
        password=fake_password,
        connection_string=fake_conn_str,
    )
    ctx = _make_ctx(_StubTargetRegistry([raw]))

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    # Field set is exactly the seven entries.
    assert set(TargetSummary.model_fields.keys()) == {
        "name",
        "kind",
        "display_name",
        "description",
        "capabilities",
        "tags",
        "enabled",
    }

    json_text = out.model_dump_json()

    # None of the forbidden substrings (field values) may appear.
    for needle in (
        "/Users/",
        "/home/",
        ".ssh",
        "id_rsa",
        "10.0.0.5",
        "admin",
        "secret123",
        "postgres://",
        "user:pass",
    ):
        assert needle not in json_text, (
            f"forbidden substring {needle!r} leaked into JSON: {json_text}"
        )


def test_list_targets_handler_returns_safe_planning_fields() -> None:
    """The handler must still return the planning-useful fields after
    redaction (name / kind / capabilities / tags / enabled), so the
    Planner can actually use the output downstream.
    """
    raw = _RawTarget(
        name="prod-web",
        kind="ssh",
        capabilities=["shell", "file_read"],
        tags=["web", "prod"],
        enabled=True,
    )
    ctx = _make_ctx(_StubTargetRegistry([raw]))

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.name == "prod-web"
    assert summary.kind == "ssh"
    assert summary.capabilities == ["shell", "file_read"]
    assert summary.tags == ["web", "prod"]
    assert summary.enabled is True


def test_list_targets_handler_filters_disabled_by_default() -> None:
    raw_enabled = _RawTarget(name="alpha", kind="local", enabled=True)
    raw_disabled = _RawTarget(name="bravo", kind="local", enabled=False)
    ctx = _make_ctx(_StubTargetRegistry([raw_enabled, raw_disabled]))

    # Default include_disabled=False → only "alpha" survives.
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert [t.name for t in out.targets] == ["alpha"]

    # include_disabled=True → both survive.
    out2 = asyncio.run(list_targets_handler(ListTargetsInput(include_disabled=True), ctx))
    assert sorted(t.name for t in out2.targets) == ["alpha", "bravo"]


# Ensure structlog at least doesn't blow up under default logging.
logging.getLogger().setLevel(logging.WARNING)
