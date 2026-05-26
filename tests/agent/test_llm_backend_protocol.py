"""Protocol-shape contract for ``LLMBackend``.

Three things the Agent loop relies on:

1. The Protocol exposes ``name`` / ``capabilities`` / ``messages_create`` as
   its declared members.
2. ``@runtime_checkable`` semantics let a non-subclass-but-structurally-
   conformant stub pass ``isinstance(stub, LLMBackend)`` — needed so
   ``hostlens doctor`` and ``create_backend`` can introspect arbitrary
   backend objects.
3. ``messages_create`` is an ``async def`` so the Agent loop always awaits
   it.
"""

from __future__ import annotations

import inspect
from typing import Any

from hostlens.agent.backend import (
    BackendCapabilities,
    LLMBackend,
    MessageResponse,
    TextBlock,
    Usage,
)


def _caps() -> BackendCapabilities:
    return BackendCapabilities(
        prompt_caching=True,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )


class _StubBackend:
    """Structural-only implementation of ``LLMBackend``.

    Intentionally **does not** inherit from ``LLMBackend`` — the test is
    that ``@runtime_checkable`` detects structural conformance.
    """

    name = "stub"

    def __init__(self) -> None:
        self.capabilities = _caps()

    async def messages_create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> MessageResponse:
        return MessageResponse(
            id="msg_stub",
            model=model,
            role="assistant",
            content=[TextBlock(type="text", text="stub")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )


def test_protocol_declares_expected_members() -> None:
    """``LLMBackend`` MUST expose ``name`` / ``capabilities`` /
    ``messages_create`` as Protocol members.

    The dunder attribute name shifted across CPython releases
    (``__protocol_attrs__`` on 3.12+, fallback ``_get_protocol_attrs`` on
    3.11) — we check both so the assertion stays stable across the
    supported interpreter range.
    """

    attrs: set[str]
    raw = getattr(LLMBackend, "__protocol_attrs__", None)
    if raw is not None:
        attrs = set(raw)
    else:
        # Pre-3.12 fallback.
        from typing import _get_protocol_attrs  # type: ignore[attr-defined]

        attrs = set(_get_protocol_attrs(LLMBackend))

    assert {"name", "capabilities", "messages_create"} <= attrs


def test_structural_implementation_is_recognized_by_isinstance() -> None:
    """A class that implements all members without inheriting the Protocol
    MUST pass ``isinstance`` thanks to ``@runtime_checkable``."""

    stub = _StubBackend()
    assert isinstance(stub, LLMBackend)


def test_messages_create_is_a_coroutine_function() -> None:
    """``messages_create`` must be ``async def`` so the Agent loop always
    awaits it (sync would silently work via Pyright but break at runtime
    once we try ``await`` on a non-awaitable)."""

    assert inspect.iscoroutinefunction(_StubBackend.messages_create)
