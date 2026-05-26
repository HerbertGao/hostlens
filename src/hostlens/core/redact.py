"""Text-level secret redaction utility.

`redact_text(s)` applies the default regex rules from
`docs/OPERABILITY.md` §7.2 to a single string and returns a redacted copy.
Each matched secret is replaced with its first 4 and last 4 characters
joined by `...` (`sk-abcd...7890`); strings shorter than 9 characters are
fully masked as `****`.

The function is purely functional and stateless. It is invoked at any
rendering boundary that writes user-visible output (markdown / json
report, log lines, notifier payloads).
"""

from __future__ import annotations

import re

__all__ = ["redact_text"]


# Compiled once at import time; OPERABILITY.md §7.2 default rule set.
_KEYWORD_ASSIGN = re.compile(r"(?i)\b(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*(\S+)")
"""Matches `key:value` / `key=value` form, e.g. `password=<the-value>`
or `api_key: sk-<the-value>`. The regex requires a `:` or `=` separator
between keyword and value; the bare HTTP-header form `Bearer <token>`
(space-separated) is **not** matched here — that case needs an HTTP
header parser and is out of scope for the M1 default ruleset (the
standalone `sk-...` / JWT regexes still catch the token itself when it
appears verbatim in the text).

Group 1 = keyword (preserved verbatim).
Group 2 = the secret value to redact.
"""

_SENSITIVE_KEY_NAMES = re.compile(r"(?i)(password|secret|token|api[_-]?key|bearer)")
"""Matches a dict key name that, by itself, signals the associated
value is sensitive. Used by structured-data walkers to mask values
whose adjacent key is one of these keywords (e.g. JSON-like
``{"password": "..."}`` where the value alone does not match
`_KEYWORD_ASSIGN`)."""


def is_sensitive_key(key: str) -> bool:
    """Return True if `key` looks like a secret-bearing field name.

    Helpers that walk dict-like structures use this to decide whether to
    mask the whole adjacent value regardless of its content.
    """
    return _SENSITIVE_KEY_NAMES.search(key) is not None


_JWT = re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")
"""Three-segment base64url JWT (header.payload.signature)."""

_SK_KEY = re.compile(r"sk-[a-zA-Z0-9-]{20,}")
"""Anthropic / OpenAI `sk-...` API key prefix."""


def _mask(value: str) -> str:
    """Replace `value` with `<first4>...<last4>` (or `****` if too short)."""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def redact_text(s: str) -> str:
    """Return a redacted copy of `s` with default secret patterns masked.

    The function is order-sensitive: keyword-assignment matches are
    handled first so that values containing `sk-...` or JWT fragments
    inside an assignment are masked once (avoiding double-replacement
    that would corrupt the kept-prefix marker).
    """

    def _sub_assign(match: re.Match[str]) -> str:
        keyword = match.group(1)
        value = match.group(2)
        return f"{keyword}={_mask(value)}"

    out = _KEYWORD_ASSIGN.sub(_sub_assign, s)
    out = _JWT.sub(lambda m: _mask(m.group(0)), out)
    out = _SK_KEY.sub(lambda m: _mask(m.group(0)), out)
    return out
