"""Tests for `@tool` decorator per spec §需求:@tool 装饰器必须是纯 spec factory.

Three scenarios:
1. Decorator returns a `ToolSpec` instance (not a callable).
2. Importing a module that uses `@tool` does NOT mutate any module-level /
   global registry-shaped variable.
3. Trying to `await decorated_name(args, ctx)` raises `TypeError`.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types

import pytest
from pydantic import BaseModel

from hostlens.tools.base import ToolSpec
from hostlens.tools.decorators import tool


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def test_decorator_returns_tool_spec_instance() -> None:
    @tool(
        name="x",
        version="1.0.0",
        input_schema=_In,
        output_schema=_Out,
        agent_description="ad",
        mcp_description="md",
        cli_help=None,
        surfaces={"agent"},
        side_effects="read",
    )
    async def handler(args: BaseModel, ctx: object) -> BaseModel:
        return _Out()

    assert isinstance(handler, ToolSpec)
    # Original handler is retained in the .handler attribute (escape hatch
    # for unit tests). It must be the very same function object.
    assert callable(handler.handler)
    assert inspect.iscoroutinefunction(handler.handler)


def test_calling_decorated_name_raises_type_error_with_guidance() -> None:
    """Per spec §需求:@tool §场景:试图直接调用装饰后的名字 raise.

    The decorated name binds to a ToolSpec instance; direct invocation must
    raise TypeError with a message that explicitly points to the supported
    entry points (`registry.dispatch()` / `spec.handler()`). Asserting a bare
    TypeError without checking the message would pass vacuously against
    Pydantic's default "object is not callable" behavior.
    """

    @tool(
        name="x",
        version="1.0.0",
        input_schema=_In,
        output_schema=_Out,
        agent_description="ad",
        mcp_description="md",
        cli_help=None,
        surfaces={"agent"},
        side_effects="read",
    )
    async def handler(args: BaseModel, ctx: object) -> BaseModel:
        return _Out()

    with pytest.raises(TypeError) as exc_info:
        handler(_In(), None)  # type: ignore[operator]

    message = str(exc_info.value)
    # Spec-required guidance — must steer the caller to the supported entry
    # points, not a generic Pydantic message.
    assert "registry.dispatch" in message
    assert "spec.handler" in message
    assert "not callable" in message


def test_import_does_not_mutate_module_level_registry_state() -> None:
    """spec §需求:@tool 装饰不触发任何 import side effect.

    Use `importlib.reload` on a tiny synthetic module that uses `@tool` and
    snapshot the modules' dict before/after to assert no `_default_registry`
    / `_global_registry` / `_tools_list` style variables exist anywhere
    inside `hostlens.tools.*`.
    """

    # Build a synthetic module exercising @tool.
    source = """
from pydantic import BaseModel
from hostlens.tools.decorators import tool


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


@tool(
    name="probe_tool",
    version="0.0.1",
    input_schema=_In,
    output_schema=_Out,
    agent_description="ad",
    mcp_description="md",
    cli_help=None,
    surfaces={"agent"},
    side_effects="none",
)
async def probe_tool(args, ctx):
    return _Out()
"""
    mod = types.ModuleType("_hostlens_tools_test_probe_module")
    sys.modules["_hostlens_tools_test_probe_module"] = mod
    try:
        exec(compile(source, "<probe>", "exec"), mod.__dict__)

        # Reload all hostlens.tools.* submodules and snapshot their globals
        # — the decorator must not have written any registry-style attribute
        # into them.
        for modname in [
            "hostlens.tools",
            "hostlens.tools.base",
            "hostlens.tools.decorators",
        ]:
            if modname in sys.modules:
                m = importlib.reload(sys.modules[modname])
                forbidden_names = {
                    "_default_registry",
                    "_global_registry",
                    "_tools_list",
                    "_REGISTRY",
                    "DEFAULT_REGISTRY",
                }
                attrs = set(dir(m))
                assert attrs.isdisjoint(forbidden_names), (
                    f"{modname} unexpectedly defines {attrs & forbidden_names}"
                )

        # The decorated probe_tool is a ToolSpec, proving @tool ran.
        assert isinstance(mod.probe_tool, ToolSpec)
    finally:
        sys.modules.pop("_hostlens_tools_test_probe_module", None)
