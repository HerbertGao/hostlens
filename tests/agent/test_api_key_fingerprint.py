"""Unit tests for ``api_key_fingerprint`` helper.

The fingerprint function is the only sanctioned helper that may render an
API-key-shaped string in user-visible output (logs / doctor JSON /
``__repr__``). Its contract — particularly the ``len < 12`` cutoff to
``"<redacted>"`` — exists so that short test keys cannot be reconstructed
from their fingerprint by slicing.
"""

from __future__ import annotations

from hostlens.agent.backend import api_key_fingerprint


def test_none_returns_unset_placeholder() -> None:
    assert api_key_fingerprint(None) == "<unset>"


def test_empty_string_returns_unset_placeholder() -> None:
    assert api_key_fingerprint("") == "<unset>"


def test_short_key_returns_redacted_constant() -> None:
    """A 5-char key is too short to slice safely — must collapse to a
    constant placeholder so the input cannot be reconstructed."""

    assert api_key_fingerprint("short") == "<redacted>"


def test_boundary_length_12_uses_slicing() -> None:
    """Exactly at the threshold: first 4 + ``...`` + last 4 (no overlap)."""

    assert api_key_fingerprint("123456789012") == "1234...9012"


def test_long_key_uses_slicing() -> None:
    """Realistic-length key: only 4+4 chars are exposed."""

    fake_key = (
        "sk-" + "ant-" + "abcdefghijklmnop"
    )  # pragma: allowlist secret — fake fixture, not a real key
    assert api_key_fingerprint(fake_key) == "sk-a...mnop"
