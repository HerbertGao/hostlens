"""The `@tool` decorator: pure ToolSpec factory (M2).

The decorator returned by `tool(**metadata)` accepts an async handler
function and returns a `ToolSpec` instance combining handler + metadata.

IMPORTANT semantics:

- The decorator **MUST NOT** mutate any module-level / global / class-level
  state at decoration time. There is no implicit registry write here —
  assembly happens explicitly in `register_default_tools(registry)`.
- The decorated name becomes a `ToolSpec` instance, **not a callable**.
  Trying to `await run_inspector(args, ctx)` after decoration raises
  `TypeError` because `ToolSpec` is not callable. Production code MUST use
  `await registry.dispatch(name, args, ctx)` (or the agent adapter); the
  `ToolSpec.handler` attribute is an escape hatch reserved for unit tests
  that need to bypass the registry policy gate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from hostlens.tools.base import ToolSpec

# Type alias for the input async handler accepted by `@tool`. Kept narrow so
# mypy can still validate handler shape at decorator call sites.
_AsyncToolHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


def tool(**metadata: Any) -> Callable[[_AsyncToolHandler], ToolSpec]:
    """Return a decorator that wraps an async handler into a `ToolSpec`.

    Usage:

        @tool(
            name="run_inspector",
            version="1.0.0",
            input_schema=RunInspectorInput,
            output_schema=RunInspectorOutput,
            agent_description="...",
            mcp_description="...",
            cli_help=None,
            surfaces={"agent"},
            side_effects="read",
            sensitive_output=True,
            timeout=30.0,
        )
        async def run_inspector(args, ctx):
            ...

    After decoration the name `run_inspector` refers to a `ToolSpec`
    instance and is no longer directly callable.
    """

    def decorator(handler: _AsyncToolHandler) -> ToolSpec:
        return ToolSpec(handler=handler, **metadata)

    return decorator
