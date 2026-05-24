"""`ToolRegistry` — name-indexed `ToolSpec` registry with async dispatch.

Layer 1 of the double-layer capability model (CLAUDE.md §4.10). Each
process / test usually owns its own `ToolRegistry` instance; there is no
global default registry (see `tools/decorators.py` for rationale).

Boundary contract:
- `dispatch(name, args, ctx)` accepts a `BaseModel` instance for `args`
  (trusted-caller path). The dict/JSON boundary lives one layer up in
  `hostlens.agent.tools_adapter.ToolsAdapter.dispatch`, which validates
  untrusted input via `spec.input_schema.model_validate(...)` first.
- Timeouts propagate as `asyncio.TimeoutError` from `asyncio.wait_for`;
  the registry layer does NOT wrap them into tool_error envelopes — that
  is the adapter's job.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from hostlens.core.exceptions import ToolError
from hostlens.tools.base import ToolContext, ToolSpec


class ToolRegistry:
    """In-memory name-indexed registry for `ToolSpec` instances."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a `ToolSpec`. Duplicate names raise `ToolError`.

        Error message includes both the original and incoming spec's
        `handler.__module__` so reviewers can locate the conflict.
        """
        existing = self._specs.get(spec.name)
        if existing is not None:
            raise ToolError(
                "duplicate tool name "
                f"{spec.name!r}: already registered from "
                f"{getattr(existing.handler, '__module__', '<unknown>')!r}, "
                f"attempted re-registration from "
                f"{getattr(spec.handler, '__module__', '<unknown>')!r}"
            )
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        """Return the spec for `name` or raise `KeyError`."""
        return self._specs[name]

    def names(self) -> set[str]:
        return set(self._specs.keys())

    def list_for(self, surface: Literal["agent", "mcp", "cli"]) -> list[ToolSpec]:
        """Return specs exposed to `surface`, sorted by name ascending."""
        matched = [spec for spec in self._specs.values() if surface in spec.surfaces]
        matched.sort(key=lambda s: s.name)
        return matched

    async def dispatch(self, name: str, args: BaseModel, ctx: ToolContext) -> BaseModel:
        """Look up `name`, validate `args` type, invoke handler with optional timeout.

        Steps:
        1. `self.get(name)` — `KeyError` if missing.
        2. `isinstance(args, spec.input_schema)` — `TypeError` if not.
        3. `await spec.handler(args, ctx)` (wrapped in `asyncio.wait_for`
           when `spec.timeout is not None`).
        4. `isinstance(result, spec.output_schema)` — `TypeError` if not.
        5. Return the model instance.

        `asyncio.TimeoutError` from `asyncio.wait_for` propagates unwrapped.
        """
        spec = self.get(name)
        if not isinstance(args, spec.input_schema):
            raise TypeError(
                f"ToolRegistry.dispatch({name!r}) expected args of type "
                f"{spec.input_schema.__name__}, got {type(args).__name__}"
            )

        if spec.timeout is not None:
            result = await asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout)
        else:
            result = await spec.handler(args, ctx)

        if not isinstance(result, spec.output_schema):
            raise TypeError(
                f"ToolRegistry.dispatch({name!r}) expected handler to return "
                f"{spec.output_schema.__name__}, got {type(result).__name__}"
            )
        return result
