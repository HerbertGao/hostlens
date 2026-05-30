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

__all__ = ["CASSETTE_SENSITIVE_PATTERNS", "detect_sensitive_text", "redact_text"]


# Compiled once at import time; OPERABILITY.md §7.2 default rule set.
_KEYWORD_ASSIGN = re.compile(r"(?i)\b(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*(\S+)")
"""Matches `key:value` / `key=value` form, e.g. `password=<the-value>`
or `api_key: sk-<the-value>`. The regex requires a `:` or `=` separator
between keyword and value; the bare HTTP-header form `Bearer <token>`
(space-separated) is handled by `_BEARER_HEADER` below.

Group 1 = keyword (preserved verbatim).
Group 2 = the secret value to redact.
"""

_BEARER_HEADER = re.compile(r"(?i)\bBearer\s+(\S+)")
"""Matches the bare HTTP `Authorization: Bearer <token>` form where
keyword and token are separated by whitespace rather than `:` / `=`.
This is the shape that flows into ``BackendError.__str__`` when an
SDK exception message embeds an upstream HTTP header verbatim — the
``_KEYWORD_ASSIGN`` regex's required ``[:=]`` separator does not cover
it. The token is masked while the literal word ``Bearer`` is preserved
to keep the redacted output recognizable as an auth header.

Group 1 = the token to redact (any run of non-whitespace).
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
    out = _BEARER_HEADER.sub(lambda m: f"Bearer {_mask(m.group(1))}", out)
    out = _JWT.sub(lambda m: _mask(m.group(0)), out)
    out = _SK_KEY.sub(lambda m: _mask(m.group(0)), out)
    return out


# Cassette commit gate uses a broader standard than runtime log redaction:
# a cassette is committed to git and reviewed by a human, so it is held to a
# higher bar than runtime log output (where `redact_text` only scrubs the
# most obvious leaks while deliberately keeping HOME / paths that aid
# debugging). Both `cassette_lint.py` and `RecordingBackend` import this
# single source so "recorded then linted" stays consistent. Each tuple is
# ``(name, compiled_regex)``; ``name`` is reported on a hit so a reviewer can
# identify the firing rule without re-scanning.
CASSETTE_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # API-key prefixes (Anthropic / OpenAI ``sk-...``).
    ("anthropic_or_openai_sk_key", re.compile(r"sk-[A-Za-z0-9_-]{6,}")),
    # ``Authorization: Bearer <token>``.
    ("bearer_token", re.compile(r"(?i)\bBearer\s+\S+")),
    # Three-segment JWT (header.payload.signature).
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")),
    # ``password=...`` / ``api_key=...`` / ``token=...`` / ``secret=...``
    # assignment forms. The trailing value must be at least two chars to
    # avoid matching empty-value JSON-encoded ``"password":""`` keys that
    # cassettes occasionally need to express literal empty-string fields.
    (
        "credential_assignment",
        re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*\S{2,}"),
    ),
    # User home directories — macOS ``/Users/<name>`` and Linux ``/home/<name>``.
    ("user_home_path", re.compile(r"/(Users|home)/[A-Za-z0-9._-]+")),
    # ``.ssh`` directories, anywhere in the path.
    ("ssh_path", re.compile(r"\.ssh(/|\\)")),
    # IPv4 literals (not 0.0.0.0 / 127.0.0.1 ish — block both private and
    # public; cassettes have no business holding any specific IP).
    ("ipv4_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Email addresses.
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Hostnames / FQDNs with at least one label + a common TLD or environment
    # suffix. Catches strings like ``prod-db.internal.example.com`` or
    # ``auth.corp.local`` that may leak via inspector output into cassettes.
    # The suffix set is narrow on purpose: a generic ``\.[a-z]{2,}`` would
    # collide with model IDs ("claude-opus-4-7"), tool names, and other
    # legitimate dotted tokens we expect inside cassette bodies.
    (
        "hostname_or_fqdn",
        re.compile(
            r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"(?:internal|intranet|local|lan|corp|company|enterprise|prod|production"
            r"|staging|dev|test|example|home|office|com|net|org|io|app|cloud|tech)\b",
            re.IGNORECASE,
        ),
    ),
)


def detect_sensitive_text(text: str) -> str | None:
    """Return the name of the first `CASSETTE_SENSITIVE_PATTERNS` rule that
    matches `text`, or `None` if none match.

    This is the cassette commit gate's detector. It differs from
    `redact_text` in two ways:

    - **Detection vs masking**: this returns a rule name (or None) so a caller
      can fail-and-reject; `redact_text` rewrites the string in place to mask
      secrets and is used at runtime rendering boundaries.
    - **Wider standard**: cassettes are committed to git, so this gate flags
      categories runtime redaction deliberately keeps (HOME / `.ssh` paths,
      IPv4, email, hostname-FQDN) to aid debugging. `redact_text`'s runtime
      masking semantics are intentionally narrower and are not changed here.
    """
    for name, pattern in CASSETTE_SENSITIVE_PATTERNS:
        if pattern.search(text):
            return name
    return None
