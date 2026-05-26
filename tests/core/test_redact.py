"""Unit tests for `hostlens.core.redact.redact_text`.

Covers the five rule classes defined in OPERABILITY.md §7.2:
1. `password=...` keyword assignment
2. `secret=...` keyword assignment
3. `token=...` / `api_key=...` / `bearer ...` keyword assignment
4. JWT three-segment tokens (`eyJ...`)
5. Anthropic / OpenAI `sk-...` keys
"""

from __future__ import annotations

import pytest

from hostlens.core.redact import redact_text


class TestKeywordAssignment:
    def test_password_equals_is_masked(self) -> None:
        out = redact_text("password=p@ssw0rd!supersecret")
        assert "p@ssw0rd!supersecret" not in out
        assert "password=" in out
        assert "..." in out

    def test_password_colon_with_spaces_is_masked(self) -> None:
        out = redact_text("password : verylongpassword123")
        assert "verylongpassword123" not in out

    def test_secret_assignment_is_masked(self) -> None:
        out = redact_text("secret=topsecretvalue")
        assert "topsecretvalue" not in out

    def test_secret_colon_is_masked(self) -> None:
        out = redact_text("secret: anotherlongsecret")
        assert "anotherlongsecret" not in out

    def test_token_is_masked(self) -> None:
        out = redact_text("token=ghp_1234567890abcdefghij")
        assert "ghp_1234567890abcdefghij" not in out

    def test_api_key_underscore_is_masked(self) -> None:
        out = redact_text("api_key=longvaluexyz123")
        assert "longvaluexyz123" not in out

    def test_api_key_hyphen_is_masked(self) -> None:
        out = redact_text("api-key=somelongvalue999")
        assert "somelongvalue999" not in out

    def test_bearer_is_masked(self) -> None:
        out = redact_text("bearer=mytokenvalue123456")
        assert "mytokenvalue123456" not in out

    def test_case_insensitive_keyword(self) -> None:
        out = redact_text("PASSWORD=mysecretvalue999")
        assert "mysecretvalue999" not in out

    def test_short_value_fully_masked(self) -> None:
        # value <=8 chars masked as ****
        out = redact_text("password=short")
        assert "short" not in out
        assert "****" in out


class TestJWT:
    def test_simple_jwt_is_masked(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = redact_text(f"Authorization: Bearer {jwt}")
        assert jwt not in out
        assert "eyJh" in out  # first 4 retained
        assert "..." in out

    def test_jwt_in_log_line(self) -> None:
        jwt = "eyJ0eXAiOiJKV1Q.eyJleHAiOjE2MDB9.abcDEF1234"
        out = redact_text(f"got token={jwt} from upstream")
        assert jwt not in out


class TestSkKey:
    def test_anthropic_sk_key_masked(self) -> None:
        key = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
        out = redact_text(f"key is {key}")
        assert key not in out
        assert "sk-a" in out  # prefix retained

    def test_openai_sk_with_hyphen_masked(self) -> None:
        key = "sk-proj-1234567890abcdefghijklmn"
        out = redact_text(key)
        assert key not in out


class TestNoSecret:
    def test_plain_text_unchanged(self) -> None:
        assert redact_text("hello world") == "hello world"

    def test_short_sk_prefix_not_matched(self) -> None:
        # Less than 20 chars after `sk-` → not matched
        assert redact_text("sk-short") == "sk-short"

    def test_empty_string(self) -> None:
        assert redact_text("") == ""


class TestPrefixSuffixPreservation:
    def test_mask_preserves_4_4(self) -> None:
        key = "sk-abcdefghijklmnopqrst7890"
        out = redact_text(key)
        # Format: <first4>...<last4>
        assert out.startswith("sk-a")
        assert out.endswith("7890")
        assert "..." in out


@pytest.mark.parametrize(
    "line",
    [
        "password=verylongpasswordhere",
        "secret=anothersecretvalue",
        "token=ghp_xxxxxxxxxxxxxxxxxx",
        "api_key=somekeyvalue1234567",
        "bearer=jwtlikevalueabcdef",
    ],
)
def test_assignment_keeps_keyword_visible(line: str) -> None:
    """Keyword stays in output (only the value is masked)."""
    out = redact_text(line)
    keyword = line.split("=", 1)[0]
    assert keyword in out
