"""Hostlens logging configuration.

M0 scope per openspec/changes/bootstrap-project-skeleton/specs/core-services/spec.md:
`configure_logging(mode)` initialises structlog with a shared processor chain.
A `redact_sensitive` processor sits at the top of the chain and recursively
masks the values of any mapping key whose name matches
`_SENSITIVE_FIELD_PATTERN`. Traversal covers `collections.abc.Mapping`
(so `os._Environ` is handled, not just `dict`), `list`, `tuple`, and `set`,
with a maximum recursion depth of 8 to guard against pathological inputs.

The redactor is purely functional: it never mutates the caller's data, so
`logger.info("x", data=d)` leaves `d` untouched while the log render shows
masked values.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from typing import Any, Literal, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

__all__ = [
    "configure_logging",
    "redact_sensitive",
]


_SENSITIVE_FIELD_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(key|token|secret|password|credential)"
)
"""Field-name regex shared with `core.config`; matches anywhere in the key."""

_REDACTED: str = "***"

_MAX_REDACT_DEPTH: int = 8
"""Maximum recursion depth for `_redact_value`.

Beyond this depth values are returned as-is. This is a defence-in-depth
guard against hostile / cyclic inputs; legitimate log payloads should not
nest this deeply.
"""


def _is_sensitive(field_name: str) -> bool:
    return _SENSITIVE_FIELD_PATTERN.search(field_name) is not None


def _redact_value(value: Any, depth: int = 0) -> Any:
    """Return a redacted copy of `value` without mutating the input.

    - For any `Mapping` (covers `dict`, `os._Environ`, etc.) emit a new
      `dict` whose values for sensitive keys are replaced with `"***"`
      and whose other values are recursively cleaned.
    - For `list` / `tuple` / `set` recurse element-wise, preserving the
      container type.
    - For scalars return as-is.
    - If `depth >= _MAX_REDACT_DEPTH` short-circuit and return as-is to
      avoid unbounded recursion (RecursionError is not acceptable).
    """

    if depth >= _MAX_REDACT_DEPTH:
        return value

    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, sub in value.items():
            if isinstance(key, str) and _is_sensitive(key):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = _redact_value(sub, depth + 1)
        return cleaned

    # Order matters: str is iterable but must not be treated as a sequence.
    if isinstance(value, str | bytes | bytearray):
        return value

    if isinstance(value, list):
        return [_redact_value(item, depth + 1) for item in value]

    if isinstance(value, tuple):
        return tuple(_redact_value(item, depth + 1) for item in value)

    if isinstance(value, set):
        return {_redact_value(item, depth + 1) for item in value}

    return value


def redact_sensitive(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: mask sensitive fields anywhere in `event_dict`.

    Builds a fresh `EventDict` so the caller's original mapping is never
    mutated. Top-level keys are evaluated with the same regex as nested
    keys; matching keys get `"***"` regardless of the value type.
    """

    del logger, method_name  # processor signature; unused.

    cleaned: EventDict = {}
    for key, value in event_dict.items():
        if isinstance(key, str) and _is_sensitive(key):
            cleaned[key] = _REDACTED
        else:
            cleaned[key] = _redact_value(value, depth=1)
    return cleaned


def _shared_processors() -> list[structlog.typing.Processor]:
    """Processor chain shared by dev + prod modes.

    `redact_sensitive` MUST stay at the head so downstream renderers only
    ever see masked payloads.
    """

    return [
        redact_sensitive,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(mode: Literal["dev", "prod"], *, stream: TextIO | None = None) -> None:
    """Configure structlog for the chosen render mode.

    - `dev`:  human-readable `ConsoleRenderer` (colours enabled; structlog
      auto-falls-back to plain text when the destination is not a TTY).
    - `prod`: single-line JSON via `JSONRenderer` for log aggregators.

    Both modes share the same head-of-chain redactor so secrets never
    reach the renderer regardless of mode.

    `stream` selects the log sink. The default (`None`) keeps structlog's
    historical `sys.stdout` target. A **stdio MCP server must pass
    `sys.stderr`**: under `hostlens mcp serve`, stdout IS the JSON-RPC protocol
    stream, so any dispatch-time log on stdout corrupts the client frame (e.g.
    the ssh_config parser's debug lines while `propose_target_import` parses an
    inventory). Routing the sink to stderr is the single correct fix — it covers
    every log call-site, not just the ones a guard happens to wrap.
    """

    renderer: structlog.typing.Processor
    if mode == "dev":
        # Decide colours from the ACTUAL sink: a non-TTY ``stream`` (file / StringIO)
        # must not get ANSI codes, and a TTY sink should. Defaults to ``sys.stderr``
        # when no stream is given (the historical behaviour).
        renderer = structlog.dev.ConsoleRenderer(colors=(stream or sys.stderr).isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    processors: list[structlog.typing.Processor] = [*_shared_processors(), renderer]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )
