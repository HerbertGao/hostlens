"""Tests that the `tool_registry` fixture truly gives each test its own
instance (spec §需求:register_default_tools §场景:多 registry 实例隔离 in
programmatic form).

Two tests share the fixture name but mutate their own registries; if
isolation failed, the second test would observe the first test's
mutation.
"""

from __future__ import annotations

from hostlens.tools.registry import ToolRegistry


def test_first_test_can_clear_then_re_register(tool_registry: ToolRegistry) -> None:
    # Mutate this registry's internal state (allowed for tests).
    tool_registry._specs.clear()  # type: ignore[attr-defined]
    assert tool_registry.names() == set()


def test_second_test_observes_fresh_registry(tool_registry: ToolRegistry) -> None:
    # If fixture leaked across tests, this would still be empty after
    # the previous test cleared it. Since the fixture is function-scoped
    # and re-allocates per test, it must come back fully populated.
    assert sorted(tool_registry.names()) == [
        "list_inspectors",
        "list_targets",
        "run_inspector",
    ]
