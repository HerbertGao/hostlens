"""Tests for the M2 first-batch ToolSpec policy metadata per spec
§需求:M2 首批 ToolSpec 必须含 ....

Three scenarios — one per ToolSpec — locking down the exact policy
metadata table that surface adapters (agent / mcp / cli) will consume.
"""

from __future__ import annotations

from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


def test_run_inspector_metadata_matches_spec_table() -> None:
    spec = _registry().get("run_inspector")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "read"
    assert spec.sensitive_output is True
    assert spec.requires_approval is False
    assert spec.timeout == 30.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None


def test_list_inspectors_metadata_matches_spec_table() -> None:
    spec = _registry().get("list_inspectors")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "none"
    assert spec.sensitive_output is False
    assert spec.requires_approval is False
    assert spec.timeout == 5.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None


def test_list_targets_metadata_matches_spec_table() -> None:
    spec = _registry().get("list_targets")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "none"
    assert spec.sensitive_output is True
    assert spec.requires_approval is False
    assert spec.timeout == 5.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None
