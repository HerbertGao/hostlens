"""Tests for `hostlens.agent.tools_adapter.scrub_exception_message`.

Covers spec §需求:handler 异常必须包装 §场景:tool_error 不泄露敏感数据 by
exercising each of the 5 regex pattern classes with at least 2 positive
examples plus negative controls (normal strings must pass through). The
last test is the documented e2e scenario combining all 5 pattern classes
in one input string.
"""

from __future__ import annotations

from hostlens.agent.tools_adapter import scrub_exception_message

# ---------------------------------------------------------------------------
# Pattern class 1: path substrings
# ---------------------------------------------------------------------------


def test_scrub_redacts_users_home_path() -> None:
    out = scrub_exception_message("file at /Users/alice/secrets.txt is missing")
    assert "/Users/alice" not in out
    assert "alice" not in out
    assert "***" in out


def test_scrub_redacts_linux_home_path() -> None:
    out = scrub_exception_message("config at /home/bob/.profile broken")
    assert "/home/bob" not in out
    assert "***" in out


def test_scrub_redacts_ssh_path() -> None:
    out = scrub_exception_message("cannot read .ssh/id_rsa")
    assert ".ssh/id_rsa" not in out
    assert "***" in out


def test_scrub_redacts_aws_and_kube_paths() -> None:
    out = scrub_exception_message("loaded .aws/credentials and .kube/config")
    assert ".aws/credentials" not in out
    assert ".kube/config" not in out


# ---------------------------------------------------------------------------
# Pattern class 2: IPv4 / IPv6 literals
# ---------------------------------------------------------------------------


def test_scrub_redacts_ipv4_literal() -> None:
    out = scrub_exception_message("host 10.0.0.5 unreachable")
    assert "10.0.0.5" not in out
    assert "***" in out


def test_scrub_redacts_ipv6_literal() -> None:
    out = scrub_exception_message("connect to 2001:db8::1234 failed")
    assert "2001:db8::1234" not in out
    assert "***" in out


def test_scrub_redacts_ipv6_loopback_abbreviated() -> None:
    """`::1` is the IPv6 loopback shortened form — the earlier single-pattern
    regex missed it because it required a hex char before the first colon.
    """

    out = scrub_exception_message("bind to ::1 refused")
    assert "::1" not in out
    assert "***" in out


def test_scrub_redacts_ipv4_mapped_ipv6() -> None:
    """`::ffff:10.0.0.5` is the IPv4-mapped IPv6 form, another `::`-prefix
    shortened form the earlier regex missed.
    """

    out = scrub_exception_message("origin ::ffff:10.0.0.5 blocked")
    assert "::ffff" not in out
    assert "10.0.0.5" not in out
    assert "***" in out


def test_scrub_redacts_full_ipv6() -> None:
    out = scrub_exception_message("peer 2001:db8::1234 timed out")
    assert "2001:db8::1234" not in out
    assert "***" in out


# ---------------------------------------------------------------------------
# Pattern class 3: credential signatures
# ---------------------------------------------------------------------------


def test_scrub_redacts_api_key_assignment() -> None:
    # Construct the dummy secret at runtime so the raw `sk_live_*` literal
    # never appears in the diff — otherwise high-entropy-secret scanners
    # (GitGuardian etc.) false-positive on this fixture.
    dummy_secret = "sk_" + "live_" + "abc123xyz"  # pragma: allowlist secret
    out = scrub_exception_message(f"env says API_KEY={dummy_secret}")
    assert dummy_secret not in out
    assert "API_KEY=" not in out
    assert "***" in out


def test_scrub_redacts_bearer_token() -> None:
    out = scrub_exception_message("auth header Bearer xyz123.deadbeef")
    assert "Bearer xyz123.deadbeef" not in out
    assert "***" in out


def test_scrub_redacts_sk_prefixed_secret() -> None:
    # Use an obviously-dummy repeated-char pattern so GitGuardian / similar
    # secret scanners don't false-positive on what is in fact a test fixture
    # for the scrubber itself.
    dummy_key = "sk-" + "z" * 25  # pragma: allowlist secret — dummy fixture
    out = scrub_exception_message(f"secret leak {dummy_key}")
    assert dummy_key not in out
    assert "***" in out


# ---------------------------------------------------------------------------
# Pattern class 4: identity key-value pairs
# ---------------------------------------------------------------------------


def test_scrub_redacts_user_assignment() -> None:
    out = scrub_exception_message("connect failed: user=admin")
    assert "user=admin" not in out
    assert "admin" not in out


def test_scrub_redacts_username_assignment() -> None:
    out = scrub_exception_message("login attempt username=alice ignored")
    assert "username=alice" not in out
    assert "alice" not in out


# ---------------------------------------------------------------------------
# Pattern class 5: email / user@host patterns
# ---------------------------------------------------------------------------


def test_scrub_redacts_email_pattern() -> None:
    out = scrub_exception_message("notify alice@example.com on failure")
    assert "alice@example.com" not in out
    assert "***" in out


def test_scrub_redacts_user_at_ipv4_pattern() -> None:
    out = scrub_exception_message("ssh admin@10.0.0.5 refused")
    # IPv4 pattern also fires; in either case "10.0.0.5" must vanish.
    assert "admin@10.0.0.5" not in out
    assert "10.0.0.5" not in out


# ---------------------------------------------------------------------------
# Negative controls: benign strings must NOT be mangled into useless soup
# ---------------------------------------------------------------------------


def test_scrub_does_not_touch_plain_strings() -> None:
    msg = "hello world"
    assert scrub_exception_message(msg) == msg


def test_scrub_does_not_touch_generic_error_text() -> None:
    msg = "connection refused"
    assert scrub_exception_message(msg) == msg


# ---------------------------------------------------------------------------
# E2E scenario: combined attack surface from spec §9.4c
# ---------------------------------------------------------------------------


def test_scrub_e2e_combined_input_redacts_every_pattern_class() -> None:
    raw = (
        "connect to /Users/alice/.ssh/id_rsa failed via user=admin "
        "host=10.0.0.5 token=Bearer xyz123 contact=alice@10.0.0.5"
    )
    out = scrub_exception_message(raw)
    forbidden_substrings = [
        "/Users/alice",
        "admin",
        "10.0.0.5",
        "Bearer xyz123",
        "alice@10.0.0.5",
    ]
    for needle in forbidden_substrings:
        assert needle not in out, f"scrub leaked {needle!r}: {out!r}"
