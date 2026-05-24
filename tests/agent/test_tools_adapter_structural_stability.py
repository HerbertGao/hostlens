"""Tests for `ToolsAdapter.list_for_agent` structural stability per spec
§需求:Agent surface adapter 的投影结构稳定性.

Three scenarios:
1. Two consecutive calls return deep-equal lists and the per-entry key
   order is exactly `["name", "description", "input_schema"]`.
2. Two registries with different registration orders but the same three
   ToolSpec instances produce equal projections.
3. `json.dumps(result)` (without `sort_keys=True`) preserves the
   `name → description → input_schema` insertion order in the output JSON
   string.
"""

from __future__ import annotations

import json

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools.registry import ToolRegistry

from ._helpers import TypedInput, TypedOutput, ctx_factory, make_spec


def _make_three_specs() -> tuple[object, object, object]:
    x = make_spec(name="alpha", input_schema=TypedInput, output_schema=TypedOutput)
    y = make_spec(name="bravo", input_schema=TypedInput, output_schema=TypedOutput)
    z = make_spec(name="charlie", input_schema=TypedInput, output_schema=TypedOutput)
    return x, y, z


def test_two_calls_return_deep_equal_with_strict_key_order() -> None:
    reg = ToolRegistry()
    x, y, z = _make_three_specs()
    reg.register(x)  # type: ignore[arg-type]
    reg.register(y)  # type: ignore[arg-type]
    reg.register(z)  # type: ignore[arg-type]
    adapter = ToolsAdapter(reg, ctx_factory())

    r1 = adapter.list_for_agent()
    r2 = adapter.list_for_agent()

    assert r1 == r2
    for entry in r1:
        assert list(entry.keys()) == ["name", "description", "input_schema"]


def test_two_registries_with_different_register_orders_produce_equal_projection() -> None:
    x, y, z = _make_three_specs()

    reg_a = ToolRegistry()
    reg_a.register(x)  # type: ignore[arg-type]
    reg_a.register(y)  # type: ignore[arg-type]
    reg_a.register(z)  # type: ignore[arg-type]

    reg_b = ToolRegistry()
    reg_b.register(z)  # type: ignore[arg-type]
    reg_b.register(x)  # type: ignore[arg-type]
    reg_b.register(y)  # type: ignore[arg-type]

    adapter_a = ToolsAdapter(reg_a, ctx_factory())
    adapter_b = ToolsAdapter(reg_b, ctx_factory())

    assert adapter_a.list_for_agent() == adapter_b.list_for_agent()


def test_json_dumps_preserves_insertion_order_in_each_tool_object() -> None:
    reg = ToolRegistry()
    x, y, z = _make_three_specs()
    reg.register(x)  # type: ignore[arg-type]
    reg.register(y)  # type: ignore[arg-type]
    reg.register(z)  # type: ignore[arg-type]
    adapter = ToolsAdapter(reg, ctx_factory())

    rendered = json.dumps(adapter.list_for_agent())

    # For every tool object, "name" must appear before "description", and
    # "description" before "input_schema" (insertion order, not sorted).
    for tool_name in ("alpha", "bravo", "charlie"):
        # Locate this object's slice in the rendered JSON via its `name` value.
        # A correctly-ordered key block looks like:
        #   "name": "alpha", "description": "...", "input_schema": {...}
        name_pos = rendered.index(f'"name": "{tool_name}"')
        # Search after the name position for the subsequent keys.
        desc_pos = rendered.index('"description"', name_pos)
        schema_pos = rendered.index('"input_schema"', name_pos)
        assert name_pos < desc_pos < schema_pos, (
            f"key order broken near tool {tool_name!r}: "
            f"name@{name_pos} description@{desc_pos} input_schema@{schema_pos}"
        )
