"""In-memory ``FakeBackend`` for unit tests.

Returns pre-canned ``MessageResponse`` objects in order, raising ``IndexError``
when exhausted. Capability declaration is constructor-configurable so tests
can exercise the ``BackendCapabilityViolation`` gate (e.g. by setting
``prompt_caching=False`` and then sending ``cache_control`` blocks).

The default capability set is intentionally aligned with
``AnthropicAPIBackend`` so the "normal path" test of any consumer can stay
realistic without explicit capability wiring.
"""

from __future__ import annotations

from typing import Any, ClassVar

from hostlens.agent.backend import (
    BackendCapabilities,
    MessageResponse,
    check_capability_consistency,
)

__all__ = ["FakeBackend"]


# Default capability declaration; mirrors ``AnthropicAPIBackend.capabilities``
# so tests using ``FakeBackend()`` without overrides do not accidentally
# diverge from production-path behavior.
_DEFAULT_CAPABILITIES = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


class FakeBackend:
    """Sequential canned-response backend used by unit tests.

    Implements the ``LLMBackend`` Protocol (structurally) but deliberately
    NOT ``BackendDiagnostics`` — cassette / canned backends have no
    meaningful health concept, and the duck-type check in ``hostlens
    doctor`` skips them cleanly.
    """

    name: ClassVar[str] = "fake"

    def __init__(
        self,
        *,
        responses: list[MessageResponse],
        capabilities: BackendCapabilities | None = None,
    ) -> None:
        # Copy the list defensively so callers cannot mutate ``responses``
        # under us between calls.
        self._responses: list[MessageResponse] = list(responses)
        self._response_idx: int = 0
        self.capabilities: BackendCapabilities = (
            capabilities if capabilities is not None else _DEFAULT_CAPABILITIES
        )

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
        # Capability gate runs BEFORE the response queue is touched so a
        # gate violation cannot consume a queued response slot.
        check_capability_consistency(
            backend_name=self.name,
            capabilities=self.capabilities,
            system=system,
            messages=messages,
            tools=tools,
        )

        if self._response_idx >= len(self._responses):
            raise IndexError(
                f"FakeBackend exhausted: {self._response_idx + 1} calls made, "
                f"{len(self._responses)} responses configured"
            )
        response = self._responses[self._response_idx]
        self._response_idx += 1
        return response
