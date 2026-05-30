"""Cassette-replay ``PlaybackBackend`` for integration tests.

Loads a JSON Lines cassette (one ``{"request": {...}, "response": {...},
"tools_schema_hash": "..."?}`` record per line) at construction time and
returns the recorded response when the incoming request key matches.

Per design.md D-6 and spec §需求:`PlaybackBackend`, a miss MUST raise
``CassetteMiss`` rather than fall back to a real API call — falling back
would silently consume Anthropic quota in CI and make tests non-reproducible.
The request-key algorithm intentionally hashes only ``model`` / ``messages``
/ ``len(tools)`` (system / max_tokens / tools content excluded) so a system-
prompt iteration does not invalidate the whole cassette set; tools schema
drift is detected separately by ``scripts/cassette_lint.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from hostlens.agent.backend import (
    BackendCapabilities,
    MessageResponse,
    check_capability_consistency,
)
from hostlens.agent.cassette_key import request_key_for_payload
from hostlens.core.exceptions import BackendError

__all__ = ["CassetteMiss", "PlaybackBackend"]


# Length of the request-key prefix surfaced in ``CassetteMiss.__str__``.
# The full SHA256 hex is kept on the attribute for programmatic access;
# only a short prefix is rendered into log lines for readability.
_REQUEST_KEY_PREVIEW_LEN = 16


# Default capability declaration; same shape as ``FakeBackend`` /
# ``AnthropicAPIBackend`` so any tooling that snapshots capability sets
# does not see drift across the three M2 backends.
_DEFAULT_CAPABILITIES = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


class CassetteMiss(BackendError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when ``PlaybackBackend`` cannot find a matching cassette record.

    Carries ``request_key`` (full SHA256 hex) and ``cassette_path`` (string)
    as instance attributes. The ``__str__`` rendering deliberately uses only
    the file basename and a truncated key prefix so absolute paths and full
    hashes do not leak through to logs / CI surfaces.
    """

    def __init__(
        self,
        *,
        request_key: str,
        cassette_path: str,
    ) -> None:
        # ``BackendError.__init__`` requires the ``backend_name`` kwarg; the
        # explicit ``kind="cassette_miss"`` makes the error code visible in
        # structured logs / doctor JSON output without callers needing to
        # branch on ``isinstance(exc, CassetteMiss)``.
        super().__init__(
            backend_name="playback",
            kind="cassette_miss",
            cause=None,
        )
        self.request_key: str = request_key
        self.cassette_path: str = cassette_path

    def __str__(self) -> str:
        # Render only the basename + truncated key prefix; the full path /
        # full hash live on the attributes for programmatic access.
        return (
            f"CassetteMiss(request_key={self.request_key[:_REQUEST_KEY_PREVIEW_LEN]}..., "
            f"cassette={Path(self.cassette_path).name})"
        )


class PlaybackBackend:
    """Cassette-replay backend.

    Implements the ``LLMBackend`` Protocol (structurally). Does NOT implement
    ``BackendDiagnostics`` — a cassette has no health / quota concept and
    ``ensure_safe_for_daemon`` would be misleading.
    """

    name: ClassVar[str] = "playback"
    capabilities: ClassVar[BackendCapabilities] = _DEFAULT_CAPABILITIES

    def __init__(self, *, cassette_path: Path) -> None:
        self._cassette_path: Path = cassette_path
        self._records: list[dict[str, Any]] = self._load_cassette(cassette_path)

    @staticmethod
    def _load_cassette(path: Path) -> list[dict[str, Any]]:
        """Parse the JSON Lines cassette at ``path``.

        Each non-empty line MUST be a JSON object; a parse failure raises
        ``ValueError("invalid cassette format at line N")`` (1-indexed line
        number for human-readable diagnostics). Empty / whitespace-only
        lines are skipped so cassettes can be hand-edited without strict
        trailing-newline rules.
        """

        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fp:
            for line_no, raw in enumerate(fp, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    # Spec mandates the exact phrasing
                    # ``invalid cassette format at line N``; the original
                    # JSON decode detail stays accessible via the chained
                    # ``__cause__`` for debugging.
                    raise ValueError(f"invalid cassette format at line {line_no}") from exc
                if not isinstance(record, dict):
                    # Single canonical phrasing across all per-line validation
                    # failures (JSON decode / non-object / field type) so spec
                    # §场景:cassette 文件 JSON 格式校验 has one error contract.
                    # The structural detail (``got <type>``) survives via a
                    # synthetic chained ``__cause__`` for debugging.
                    raise ValueError(f"invalid cassette format at line {line_no}") from TypeError(
                        f"expected object, got {type(record).__name__}"
                    )
                # Per-field type validation at load time so ``_record_request_key``
                # can trust the cassette shape at runtime (spec §需求:`PlaybackBackend`
                # —— cassette loading validates, runtime treats as already-valid).
                request = record.get("request")
                if isinstance(request, dict):
                    messages = request.get("messages")
                    if messages is not None and not isinstance(messages, list):
                        raise ValueError(
                            f"invalid cassette format at line {line_no}"
                        ) from TypeError(
                            f"request.messages must be a list, got {type(messages).__name__}"
                        )
                    tools_count = request.get("tools_count")
                    if tools_count is not None and not isinstance(tools_count, int):
                        raise ValueError(
                            f"invalid cassette format at line {line_no}"
                        ) from TypeError(
                            f"request.tools_count must be an int, got {type(tools_count).__name__}"
                        )
                records.append(record)
        return records

    def _request_key(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> str:
        """Compute the SHA256 request key for cassette lookup.

        Excludes ``system`` (so prompt iteration doesn't break cassettes),
        ``max_tokens`` (response-independent), full ``tools`` content
        (only ``len(tools)`` is used; schema drift is caught by the
        separate ``cassette_lint`` tool), and ``timeout`` (client-side
        only). Trade-offs documented in spec §需求:`PlaybackBackend`.
        """

        return request_key_for_payload(
            model=model,
            messages=messages,
            tools_count=len(tools),
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
        # Capability gate before lookup so a violation surfaces immediately
        # rather than hiding behind a CassetteMiss.
        check_capability_consistency(
            backend_name=self.name,
            capabilities=self.capabilities,
            system=system,
            messages=messages,
            tools=tools,
        )

        key = self._request_key(model=model, messages=messages, tools=tools)
        for record in self._records:
            request = record.get("request")
            if not isinstance(request, dict):
                continue
            record_key = self._record_request_key(request)
            if record_key == key:
                response = record.get("response")
                if not isinstance(response, dict):
                    raise ValueError(
                        "cassette record missing 'response' object for key "
                        f"{key[:_REQUEST_KEY_PREVIEW_LEN]}..."
                    )
                return MessageResponse.model_validate(response)

        raise CassetteMiss(
            request_key=key,
            cassette_path=str(self._cassette_path),
        )

    @staticmethod
    def _record_request_key(request: dict[str, Any]) -> str:
        """Compute the cassette-side request key.

        The cassette ``request`` payload is already the canonical key shape
        ``{"model": ..., "messages": ..., "tools_count": N}`` (per spec
        §需求:`PlaybackBackend` §场景:正常回放 and §9.6 fixture format).
        We hash it the same way ``_request_key`` does for the live call so
        identical canonical payloads always produce identical hex.
        """

        return request_key_for_payload(
            model=str(request.get("model", "")),
            messages=list(request.get("messages", [])),
            tools_count=int(request.get("tools_count", 0)),
        )
