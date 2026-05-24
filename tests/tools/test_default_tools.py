"""Tests for `register_default_tools` per spec §需求:register_default_tools
显式装配函数必须存在且非幂等.

Three scenarios:
1. Successful assembly: registry contains exactly the three first-batch
   ToolSpec names.
2. Duplicate assembly raises `ToolError`.
3. Two registries are isolated — assembling on one does not affect the
   other.
"""

from __future__ import annotations

import pytest

from hostlens.core.exceptions import ToolError
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


def test_register_default_tools_registers_exactly_three_names() -> None:
    reg = ToolRegistry()
    register_default_tools(reg)
    assert sorted(reg.names()) == ["list_inspectors", "list_targets", "run_inspector"]


def test_register_default_tools_is_non_idempotent() -> None:
    reg = ToolRegistry()
    register_default_tools(reg)
    with pytest.raises(ToolError) as ei:
        register_default_tools(reg)
    # Error message must name the duplicated ToolSpec (run_inspector is
    # the first one registered, so re-registration trips on it first).
    assert "run_inspector" in str(ei.value)


def test_register_default_tools_multiple_registries_are_isolated() -> None:
    r1 = ToolRegistry()
    r2 = ToolRegistry()
    register_default_tools(r1)
    assert sorted(r1.names()) == ["list_inspectors", "list_targets", "run_inspector"]
    # r2 has not been touched.
    assert r2.names() == set()
