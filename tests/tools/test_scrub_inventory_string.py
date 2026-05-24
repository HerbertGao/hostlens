"""Tests for `scrub_inventory_string` — the per-string-field redactor
applied to every `TargetSummary` string value before it leaves
`list_targets_handler`.

Five scenarios per spec §需求:TargetSummary 输出 schema 必须脱敏:
1. Path substrings trigger skip (return None).
2. IPv4 / IPv6 literals trigger skip.
3. Credential signatures (KEY=value / Bearer .../ sk-...) trigger skip.
4. The `user|username|usr` keyword followed by a token replaces only
   the trailing identifier with `"***"`, preserving prefix + suffix.
5. Compound words like `"user-service"` are NOT skipped (word-boundary
   rule).
"""

from __future__ import annotations

import pytest

from hostlens.tools.schemas.list_targets import scrub_inventory_string

# ---------------------------------------------------------------------------
# (a) Path substrings — return None (skip whole target).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/Users/alice/secrets",
        "/home/bob/.ssh/id_rsa",
        "config at .aws/credentials backup",
        "kube state in .kube/config",
        "owned-by /Users/charlie",
    ],
)
def test_path_substrings_return_none(value: str) -> None:
    assert scrub_inventory_string(value, field_kind="description") is None


# ---------------------------------------------------------------------------
# (b) IPv4 / IPv6 literals — return None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "prod-10.0.0.5",
        "prod10.0.0.5",  # no word boundary — glued to alphanumeric
        "192.168.1.42",
        "login as admin@10.0.0.5",
        "fe80::1",
        "2001:db8:85a3::8a2e:370:7334",
    ],
)
def test_ip_literals_return_none(value: str) -> None:
    assert scrub_inventory_string(value, field_kind="display_name") is None


# ---------------------------------------------------------------------------
# (c) Credential signatures — return None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "API_KEY=sk-abc123",
        "DB_PASSWORD=hunter2",
        "MY_SECRET=xyz",
        "AUTH_TOKEN=eyJhbGciOi",
        "Bearer xyz123",
        "bearer abc.def.ghi",
        "sk-" + "a" * 30,  # pragma: allowlist secret — dummy fixture, not a real key
    ],
)
def test_credential_signatures_return_none(value: str) -> None:
    assert scrub_inventory_string(value, field_kind="description") is None


# ---------------------------------------------------------------------------
# (d) "user" keyword followed by token — replace token with "***" only.
# ---------------------------------------------------------------------------


def test_user_keyword_replaces_identifier_token_only() -> None:
    out = scrub_inventory_string(
        "Owned by user alice, contact via slack",
        field_kind="description",
    )
    assert out == "Owned by user ***, contact via slack"
    assert out is not None
    assert "alice" not in out


def test_username_keyword_replaces_identifier_token_only() -> None:
    out = scrub_inventory_string(
        "set username bob",
        field_kind="description",
    )
    assert out == "set username ***"


def test_usr_keyword_replaces_identifier_token_only() -> None:
    out = scrub_inventory_string(
        "found usr carol on host",
        field_kind="description",
    )
    assert out == "found usr *** on host"


# ---------------------------------------------------------------------------
# (e) Compound words must NOT trigger the user-keyword rule.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "user-service",
        "auth-microservice",
        "user-facing-api",
        "username-validator",
    ],
)
def test_compound_words_are_not_redacted(value: str) -> None:
    assert scrub_inventory_string(value, field_kind="tags") == value


# ---------------------------------------------------------------------------
# Sanity: completely benign strings flow through unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "prod-web",
        "primary database",
        "Owned by ops team",
        "v1.2.3",
    ],
)
def test_benign_strings_are_unchanged(value: str) -> None:
    assert scrub_inventory_string(value, field_kind="description") == value
