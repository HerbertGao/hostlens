"""Unit tests for ``PlaybackBackend`` and its ``CassetteMiss`` sentinel.

Pins per spec §需求:`PlaybackBackend`:

- Normal replay path returns a parsed ``MessageResponse`` matching the
  cassette record.
- A miss raises ``CassetteMiss`` (not a fallback to real API) with a
  basename-only path rendering.
- Invalid JSON in the cassette is rejected at construction time with a
  line-numbered ``ValueError``.
- The miss path does not invoke the Anthropic SDK at all — even if a
  real ``ANTHROPIC_API_KEY`` happens to be in the environment.
- ``CassetteMiss`` is in the ``BackendError`` / ``HostlensError`` chain
  and carries ``backend_name="playback"`` from the base class.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hostlens.agent.backends.playback import CassetteMiss, PlaybackBackend
from hostlens.core.exceptions import BackendError, HostlensError


def _write_record(
    path: Path,
    *,
    request: dict[str, object],
    response: dict[str, object],
) -> None:
    """Write a single JSONL record (request + response) to ``path``."""

    record = {"request": request, "response": response}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _valid_response(*, msg_id: str = "msg_replay") -> dict[str, object]:
    return {
        "id": msg_id,
        "model": "claude-opus-4-7",
        "role": "assistant",
        "content": [{"type": "text", "text": "replayed"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.mark.asyncio
async def test_replay_returns_parsed_message_response(tmp_path: Path) -> None:
    cassette = tmp_path / "demo.jsonl"
    _write_record(
        cassette,
        request={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
            "tools_count": 0,
        },
        response=_valid_response(),
    )

    backend = PlaybackBackend(cassette_path=cassette)
    result = await backend.messages_create(
        model="claude-opus-4-7",
        system="any",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        max_tokens=100,
        timeout=10.0,
    )
    assert result.id == "msg_replay"
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_miss_raises_cassette_miss(tmp_path: Path) -> None:
    cassette = tmp_path / "demo.jsonl"
    _write_record(
        cassette,
        request={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
            "tools_count": 0,
        },
        response=_valid_response(),
    )

    backend = PlaybackBackend(cassette_path=cassette)
    with pytest.raises(CassetteMiss) as exc_info:
        await backend.messages_create(
            model="claude-opus-4-7",
            system="any",
            messages=[{"role": "user", "content": "different prompt"}],
            tools=[],
            max_tokens=100,
            timeout=10.0,
        )
    # The full SHA256 hex lives on the attribute; the string repr only
    # exposes a truncated prefix and the cassette basename.
    assert len(exc_info.value.request_key) == 64
    assert str(cassette.name) in str(exc_info.value)


@pytest.mark.asyncio
async def test_cassette_miss_str_hides_absolute_path(tmp_path: Path) -> None:
    """Spec §需求:Backend 实现必须脱敏所有敏感字段 §场景:`CassetteMiss`
    不含绝对路径 — ``__str__`` must show only the basename, never
    ``/Users/<name>`` style absolute path fragments.
    """

    cassette = tmp_path / "user_alice_secret_path.jsonl"
    _write_record(
        cassette,
        request={"model": "m", "messages": [], "tools_count": 0},
        response=_valid_response(),
    )
    backend = PlaybackBackend(cassette_path=cassette)
    with pytest.raises(CassetteMiss) as exc_info:
        await backend.messages_create(
            model="other-model",
            system="any",
            messages=[],
            tools=[],
            max_tokens=10,
            timeout=10.0,
        )
    rendered = str(exc_info.value)
    # tmp_path is platform-dependent; assert no absolute-path leading slash
    # or path prefix from the parent directories leaks through.
    assert str(tmp_path) not in rendered
    assert tmp_path.name not in rendered


def test_invalid_json_at_specific_line_raises_value_error(tmp_path: Path) -> None:
    cassette = tmp_path / "broken.jsonl"
    cassette.write_text(
        '{"request": {}, "response": {}}\n'  # line 1 valid
        "this is not json\n",  # line 2 invalid
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        PlaybackBackend(cassette_path=cassette)
    # Lock the exact spec-mandated phrasing so an accidental message
    # rewording (e.g. adding the JSON decode detail back into the
    # public message) is caught by the unit test, not by reviewers.
    assert str(exc_info.value) == "invalid cassette format at line 2"
    # Chained ``__cause__`` must be the real JSON decode error so
    # debugging keeps the underlying parser context.
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


@pytest.mark.asyncio
async def test_miss_does_not_invoke_anthropic_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with ``ANTHROPIC_API_KEY`` set, a miss must NEVER fall back to
    the real API (spec §需求:`PlaybackBackend` §场景:miss 时禁止回落真实
    API). Patches the SDK client constructor to a sentinel that fails the
    test if ever called.
    """

    import anthropic

    fake_key = (
        "sk-" + "ant-" + "realfallbacktest"
    )  # pragma: allowlist secret — fake fixture, not a real key
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key)

    sdk_called = False

    def _fail_on_construct(*args: object, **kwargs: object) -> None:
        nonlocal sdk_called
        sdk_called = True
        raise AssertionError("Anthropic SDK must NOT be constructed on cassette miss")

    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _fail_on_construct)

    cassette = tmp_path / "demo.jsonl"
    _write_record(
        cassette,
        request={"model": "m", "messages": [], "tools_count": 0},
        response=_valid_response(),
    )
    backend = PlaybackBackend(cassette_path=cassette)
    with pytest.raises(CassetteMiss):
        await backend.messages_create(
            model="different-model",
            system="any",
            messages=[],
            tools=[],
            max_tokens=10,
            timeout=10.0,
        )
    assert sdk_called is False


def test_cassette_miss_is_in_exception_hierarchy() -> None:
    """``CassetteMiss`` must be a ``BackendError`` (and transitively a
    ``HostlensError``) so callers can ``except BackendError`` cleanly,
    and the base ``backend_name`` field must be populated to
    ``"playback"`` by the constructor."""

    exc = CassetteMiss(request_key="x" * 64, cassette_path="cassettes/x.jsonl")
    assert isinstance(exc, BackendError)
    assert isinstance(exc, HostlensError)
    assert exc.backend_name == "playback"
    assert exc.kind == "cassette_miss"
