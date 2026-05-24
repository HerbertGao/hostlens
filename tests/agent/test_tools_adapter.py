"""Tests for `ToolsAdapter.list_for_agent` per spec §需求:ToolsAdapter 必须把
ToolSpec 投成 Anthropic `tool_use` schema.

Four scenarios:
1. Projection output is a list of dicts with exactly `{name, description,
   input_schema}` keys.
2. List is sorted by ToolSpec `name` (independent of registration order).
3. `input_schema` is a JSON Schema Draft 2020-12-compatible dict with the
   expected top-level fields.
4. Specs whose `surfaces ∌ "agent"` are filtered out.
"""

from __future__ import annotations

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools.registry import ToolRegistry

from ._helpers import (
    TypedInput,
    TypedOutput,
    ctx_factory,
    make_spec,
    typed_ok_handler,
)


def test_list_for_agent_returns_three_key_dicts() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="tool_a", input_schema=TypedInput, output_schema=TypedOutput))
    reg.register(make_spec(name="tool_b", input_schema=TypedInput, output_schema=TypedOutput))
    reg.register(make_spec(name="tool_c", input_schema=TypedInput, output_schema=TypedOutput))
    adapter = ToolsAdapter(reg, ctx_factory())

    schemas = adapter.list_for_agent()

    assert len(schemas) == 3
    for entry in schemas:
        assert set(entry.keys()) == {"name", "description", "input_schema"}


def test_list_for_agent_is_sorted_by_name_ignoring_registration_order() -> None:
    reg = ToolRegistry()
    # Register out of alphabetical order to prove sorting is name-driven.
    reg.register(make_spec(name="run_inspector"))
    reg.register(make_spec(name="list_inspectors"))
    reg.register(make_spec(name="list_targets"))
    adapter = ToolsAdapter(reg, ctx_factory())

    schemas = adapter.list_for_agent()

    assert [entry["name"] for entry in schemas] == [
        "list_inspectors",
        "list_targets",
        "run_inspector",
    ]


def test_input_schema_is_json_schema_draft_2020_12_compatible() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=typed_ok_handler,
        )
    )
    adapter = ToolsAdapter(reg, ctx_factory())

    schemas = adapter.list_for_agent()

    input_schema = schemas[0]["input_schema"]
    assert isinstance(input_schema, dict)
    assert input_schema.get("type") == "object"
    assert "properties" in input_schema
    assert set(input_schema["properties"].keys()) == {"name", "version"}
    # required is present because both TypedInput fields lack defaults
    assert set(input_schema.get("required", [])) == {"name", "version"}


def test_list_for_agent_filters_out_non_agent_surfaces() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="agent_only", surfaces={"agent"}))
    reg.register(make_spec(name="mcp_only", surfaces={"mcp"}))
    reg.register(make_spec(name="cli_only", surfaces={"cli"}))
    reg.register(make_spec(name="agent_plus_mcp", surfaces={"agent", "mcp"}))
    adapter = ToolsAdapter(reg, ctx_factory())

    schemas = adapter.list_for_agent()

    names = [entry["name"] for entry in schemas]
    assert names == ["agent_only", "agent_plus_mcp"]
    assert "mcp_only" not in names
    assert "cli_only" not in names
