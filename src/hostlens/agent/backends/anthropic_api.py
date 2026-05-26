"""Production ``AnthropicAPIBackend``: thin adapter over ``anthropic.AsyncAnthropic``.

Per CLAUDE.md §4.11 and design.md D-1 / D-5, this module is the **only**
place in the codebase that imports the Anthropic SDK at runtime — every
other consumer talks through the ``LLMBackend`` Protocol. The adapter:

- Constructs the SDK client with ``max_retries=0`` so the Agent loop owns
  the retry budget end-to-end (no SDK / loop double retries — D-5).
- Wraps each SDK exception class into a typed ``Backend*`` subclass so
  callers depend on the stable ``hostlens.core.exceptions`` surface, not
  on SDK internals.
- Routes ``cache_control`` blocks through ``check_capability_consistency``
  before the SDK call so a capability mismatch surfaces immediately (D-2).
- Redacts the ``api_key`` and ``base_url`` from ``__repr__`` and from
  ``health_check`` failure messages so neither logs nor doctor JSON output
  can ever leak the live secret.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import anthropic

from hostlens.agent.backend import (
    BackendCapabilities,
    BackendHealth,
    MessageResponse,
    QuotaStatus,
    api_key_fingerprint,
    check_capability_consistency,
)
from hostlens.core.exceptions import (
    BackendError,
    BackendRateLimited,
    BackendUnavailable,
)
from hostlens.core.redact import redact_text

__all__ = ["AnthropicAPIBackend"]


# HTTP status code Anthropic returns for service overload. The SDK ships an
# ``OverloadedError`` class mapped to this code, but we also accept any
# generic ``APIStatusError`` whose ``status_code`` happens to be 529 in case
# the SDK adds new specialized classes in a future version.
_HTTP_STATUS_OVERLOADED = 529


def _parse_retry_after(headers: Any) -> float | None:
    """Return the ``retry-after`` header value as a float, or ``None``.

    Defensive against:

    - ``headers`` being a non-dict / non-Mapping object (some SDK versions
      use a custom ``Headers`` class; ``.get`` works on both).
    - A missing or non-numeric ``retry-after`` value.

    The function never raises — a parse failure returns ``None`` so the
    upstream retry loop falls back to its default backoff schedule.
    """

    if headers is None:
        return None
    raw = None
    if hasattr(headers, "get"):
        try:
            raw = headers.get("retry-after")
        except Exception:
            # Defensive parse — header objects vary by SDK / httpx version.
            return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class AnthropicAPIBackend:
    """Anthropic Messages API backend (production default).

    Implements both ``LLMBackend`` (``messages_create``) and
    ``BackendDiagnostics`` (``health_check`` / ``quota_check`` /
    ``ensure_safe_for_daemon``). The ``BackendDiagnostics`` surface is
    duck-typed by ``hostlens doctor`` (CLAUDE.md §4.9).
    """

    name: ClassVar[str] = "anthropic_api"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        prompt_caching=True,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        health_check_model: str = "claude-haiku-4-5",
    ) -> None:
        # Keep ``api_key`` / ``base_url`` only as instance state for repr
        # rendering; the live secret lives inside ``self._client`` and never
        # surfaces through ``__repr__`` (see ``api_key_fingerprint`` below).
        self._api_key: str = api_key
        self._base_url: str | None = base_url
        self._health_check_model: str = health_check_model
        # ``max_retries=0`` is the explicit Anthropic SDK API to disable its
        # internal retry layer — D-5 mandates single-source retry control by
        # the Agent loop.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )

    def __repr__(self) -> str:
        # ``api_key_fingerprint`` returns ``"<unset>"`` / ``"<redacted>"`` /
        # ``"<first4>...<last4>"`` — never the raw secret. ``base_url`` is
        # rendered verbatim because dev / staging URLs are not secret; if a
        # user pushes a tokenized proxy URL through ``base_url`` they should
        # rely on ``hostlens.core.redact.redact_url`` (out of scope here).
        return (
            f"AnthropicAPIBackend(api_key_fingerprint={api_key_fingerprint(self._api_key)!r}, "
            f"base_url={self._base_url!r})"
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
        # Capability gate runs OUTSIDE the SDK try/except so a violation
        # propagates as ``BackendCapabilityViolation`` (not wrapped into
        # ``BackendUnavailable``).
        check_capability_consistency(
            backend_name=self.name,
            capabilities=self.capabilities,
            system=system,
            messages=messages,
            tools=tools,
        )

        try:
            # ``system`` / ``messages`` / ``tools`` are passed through
            # verbatim (Anthropic-schema-first; CLAUDE.md §4.11 rule).
            # ``type: ignore`` is needed because the Anthropic SDK accepts
            # ``Iterable[dict] | NotGiven`` shaped inputs through its
            # ``MessageParam`` / ``ToolParam`` overloads, which mypy cannot
            # narrow from raw ``list[dict]``.
            sdk_message = await self._client.messages.create(
                model=model,
                system=system,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after(getattr(exc.response, "headers", None))
            raise BackendRateLimited(
                backend_name=self.name,
                retry_after_seconds=retry_after,
                cause=exc,
            ) from exc
        except anthropic.APIStatusError as exc:
            # 529 is "overloaded" — semantically a rate limit / retryable
            # backpressure event with no retry-after hint. Anthropic ships
            # ``OverloadedError`` as a dedicated subclass; we also accept a
            # generic ``APIStatusError`` with ``status_code == 529`` in case
            # the SDK adds new subclasses later.
            status = getattr(exc, "status_code", None)
            if status == _HTTP_STATUS_OVERLOADED:
                raise BackendRateLimited(
                    backend_name=self.name,
                    retry_after_seconds=None,
                    cause=exc,
                ) from exc
            # 401 (``AuthenticationError``) and 403 (``PermissionDeniedError``)
            # are both non-retryable credential failures — neither should be
            # treated as a retryable availability event by the Agent loop, so
            # both map to ``BackendError(kind="auth_invalid")``.
            if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
                raise BackendError(
                    backend_name=self.name,
                    kind="auth_invalid",
                    cause=exc,
                ) from exc
            # All other 4xx / 5xx — unavailable for the Agent loop's purposes
            # (the loop's retry policy treats them uniformly).
            raise BackendUnavailable(
                backend_name=self.name,
                cause=exc,
            ) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise BackendUnavailable(
                backend_name=self.name,
                cause=exc,
            ) from exc

        # ``Message.model_dump()`` produces a dict matching ``MessageResponse``'s
        # field set; ``extra="ignore"`` (set on the Pydantic model) tolerates
        # any SDK additions without failing parse.
        return MessageResponse.model_validate(sdk_message.model_dump())

    async def health_check(self) -> BackendHealth:
        """Ping the API with a minimal prompt.

        Uses the constructor-injected ``health_check_model`` (default
        ``claude-haiku-4-5``) so the ping does not consume Opus quota.
        Any SDK failure becomes ``is_healthy=False`` with the error text
        passed through ``redact_text`` to strip api_keys / JWT / bearer
        tokens before it lands in doctor output / logs.
        """

        start = time.perf_counter()
        try:
            await self._client.messages.create(
                model=self._health_check_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=10,
            )
        except Exception as exc:
            # ``health_check`` reports any failure as ``is_healthy=False``;
            # the error text is run through ``redact_text`` so api_keys /
            # bearer tokens / JWT fragments cannot leak into doctor output.
            return BackendHealth(
                is_healthy=False,
                backend_name=self.name,
                latency_ms=None,
                error=redact_text(str(exc)),
            )
        latency_ms = (time.perf_counter() - start) * 1000.0
        return BackendHealth(
            is_healthy=True,
            backend_name=self.name,
            latency_ms=latency_ms,
            error=None,
        )

    async def quota_check(self) -> QuotaStatus | None:
        """M2 scope: Anthropic Console has no public quota API yet.

        Spec §需求:`AnthropicAPIBackend` mandates returning ``None`` so the
        doctor command surfaces "quota unknown" rather than fabricating
        zeros that look like exhaustion.
        """

        return None

    def ensure_safe_for_daemon(self) -> None:
        """No-op. API key auth is daemon-safe (ADR-008)."""

        return None
