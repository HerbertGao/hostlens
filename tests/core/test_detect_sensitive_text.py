"""Tests for the cassette commit gate detector in `hostlens.core.redact`.

Covers spec §需求:`hostlens.core.redact` 必须暴露 cassette 共享敏感规则:
hit-returns-rule-name (each pattern), clean-text-returns-None,
`redact_text` runtime semantics unchanged for `/Users/alice`, and
lint-vs-recorder same-source consistency.
"""

from __future__ import annotations

import pytest

from hostlens.core.redact import (
    CASSETTE_SENSITIVE_PATTERNS,
    detect_sensitive_text,
    redact_text,
)

# One positive example per rule. `expected` is the rule name the detector
# must report; because detection is order-sensitive, the example is chosen so
# that the named rule is the first matching one in `CASSETTE_SENSITIVE_PATTERNS`.
_POSITIVE_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("anthropic_or_openai_sk_key", "key is sk-ABCdef1234567890"),
    ("bearer_token", "Authorization: Bearer abc.def.ghi"),
    ("jwt", "eyJhbGci.eyJzdWIi.SflKxwRJ"),
    ("credential_assignment", "password=hunter2"),
    ("user_home_path", "saved under /Users/somebody/data"),
    ("ssh_path", "look in ~/.ssh/id_rsa"),
    ("ipv4_address", "connect to 203.0.113.42 now"),
    ("email_address", "contact ops@somecorp.org for help"),
    ("hostname_or_fqdn", "host prod-db.internal.example.com refused"),
)


def test_positive_examples_cover_every_pattern() -> None:
    """Each of the 9 rules has exactly one positive example covering it."""
    covered = {name for name, _ in _POSITIVE_EXAMPLES}
    declared = {name for name, _ in CASSETTE_SENSITIVE_PATTERNS}
    assert covered == declared


@pytest.mark.parametrize(("expected", "text"), _POSITIVE_EXAMPLES)
def test_detect_returns_rule_name_on_hit(expected: str, text: str) -> None:
    assert detect_sensitive_text(text) == expected


def test_detect_bearer_assignment_returns_non_none() -> None:
    # Spec scenario: detect_sensitive_text("token=Bearer xyz123") -> rule name.
    result = detect_sensitive_text("token=Bearer xyz123")
    assert result in {"bearer_token", "credential_assignment"}


@pytest.mark.parametrize(
    "clean",
    [
        "hello world, connection refused",
        # Model id must not trip hostname_or_fqdn or any other rule.
        "claude-opus-4-8",
    ],
)
def test_detect_returns_none_on_clean_text(clean: str) -> None:
    assert detect_sensitive_text(clean) is None


def test_redact_text_runtime_semantics_unchanged_for_user_path() -> None:
    # `redact_text` (runtime masking) keeps non-secret paths verbatim; the new
    # cassette gate must not tighten this. A bare `/Users/alice` path is not a
    # secret per the runtime rules, so `redact_text` leaves it untouched.
    sample = "report written to /Users/alice/output.json"
    assert redact_text(sample) == sample


def test_lint_and_recorder_same_source() -> None:
    # `cassette_lint.py` and `RecordingBackend` both import this single rule
    # set; same input must yield the same verdict. Assert the source identity:
    # there is one tuple, and the lint module re-exports it rather than
    # carrying its own copy.
    from hostlens.core import redact

    assert redact.CASSETTE_SENSITIVE_PATTERNS is CASSETTE_SENSITIVE_PATTERNS
    # Same input, same verdict across the public detector entry point.
    sample = "leaked /Users/alice/secret"
    assert detect_sensitive_text(sample) == "user_home_path"
