"""Golden + same-source tests for the cassette request-key helper.

Covers spec §需求:request-key 算法必须单一来源:
- 场景:三处同源 — ``request_key_for_payload`` and ``PlaybackBackend`` lookup
  path produce the identical hex for the same ``(model, messages, tools_count)``
  payload.
- 场景:thinking-free payload 重构不改 playback hash (golden) — the helper hash
  for a fixed thinking-free payload equals a hard-coded golden value computed
  from the M2.1 ``PlaybackBackend`` algorithm. Because the thinking-drop
  projection is the identity on thinking-free messages, this golden hex MUST
  stay unchanged across the tolerate-inbound-thinking refactor — it is the
  anchor proving existing keying was not silently altered. Do NOT reset it.
- 场景:含 thinking 块的 hash == thinking-stripped 等价物 — a payload carrying
  thinking blocks hashes to exactly the same hex as the equivalent payload with
  those blocks removed by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hostlens.agent.backend import MessageResponse
from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.agent.cassette_key import request_key_for_payload

# Fixed payload including non-ASCII content so the golden also pins the
# ``ensure_ascii=False`` serialization parameter. The golden hex was computed
# from the pre-refactor ``PlaybackBackend._request_key`` algorithm
# (json.dumps(payload, sort_keys=True, ensure_ascii=False) -> sha256 hexdigest).
_GOLDEN_MODEL = "claude-opus-4-8"
_GOLDEN_MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": "诊断这台服务器的磁盘"},
    {"role": "assistant", "content": "好的 开始巡检"},
]
_GOLDEN_TOOLS_COUNT = 3
_GOLDEN_HEX = "99695ce2071aaa5a4dbf6b3ac1bf2ddfb663036a527a3a544ef07088e6a64e43"

# Golden for the thinking-bearing payload below, AFTER the thinking-drop
# projection. It is NOT equal to ``_GOLDEN_HEX``: that golden uses a bare-string
# assistant ``content`` (``"好的 开始巡检"``), whereas a message carrying a
# thinking block must use list-form ``content`` (``[{"type":"text",...}]``) —
# a different wire shape, hence a different hash. This constant pins the
# projection's byte-stable output on the thinking/list-form path so a future
# change to how kept blocks are serialized would fail here.
_THINKING_STRIPPED_GOLDEN_HEX = "28a02aeabf1dfaeb5969451dec934ebf1b71ea8ac83f77c06bc2a790f475707f"


def test_request_key_matches_golden_hash() -> None:
    assert (
        request_key_for_payload(
            _GOLDEN_MODEL,
            _GOLDEN_MESSAGES,
            _GOLDEN_TOOLS_COUNT,
        )
        == _GOLDEN_HEX
    )


def test_thinking_bearing_payload_hashes_to_stripped_equivalent() -> None:
    """A thinking-bearing payload hashes to its thinking-stripped equivalent.

    Two assertions, both load-bearing:
    1. ``thinking_hash == stripped_hash`` — the projection drops the thinking
       block while preserving the text block, so a payload that carries a
       thinking block keys identically to the hand-stripped version.
    2. both equal ``_THINKING_STRIPPED_GOLDEN_HEX`` — a hard-coded anchor for
       the projection's output on the list-form path, so this is NOT a
       tautology (a change to how kept blocks serialize would break the pin).

    Note: this does NOT re-pin ``_GOLDEN_HEX``. That golden uses bare-string
    assistant ``content``; a thinking block requires list-form ``content``, so
    the two are different wire shapes with different hashes by construction.
    """

    thinking_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "诊断这台服务器的磁盘"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "non-deterministic reasoning trace one",
                    "signature": "sig-abc",
                },
                {"type": "text", "text": "好的 开始巡检"},
            ],
        },
    ]
    stripped_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "诊断这台服务器的磁盘"},
        {"role": "assistant", "content": [{"type": "text", "text": "好的 开始巡检"}]},
    ]

    thinking_hash = request_key_for_payload(_GOLDEN_MODEL, thinking_messages, _GOLDEN_TOOLS_COUNT)
    stripped_hash = request_key_for_payload(_GOLDEN_MODEL, stripped_messages, _GOLDEN_TOOLS_COUNT)
    assert thinking_hash == stripped_hash == _THINKING_STRIPPED_GOLDEN_HEX
    assert thinking_hash != _GOLDEN_HEX  # list-form != bare-string golden


def test_helper_and_playback_lookup_are_same_source(tmp_path: Path) -> None:
    """The helper key equals the key ``PlaybackBackend`` computes on lookup.

    Builds a cassette whose single record's canonical ``request`` matches the
    fixed payload, then drives ``messages_create`` so the backend's internal
    lookup key (via the shared helper) resolves the record — proving the live
    lookup path and the standalone helper share one source.
    """

    expected_response = {
        "id": "msg_golden",
        "model": _GOLDEN_MODEL,
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    record = {
        "request": {
            "model": _GOLDEN_MODEL,
            "messages": _GOLDEN_MESSAGES,
            "tools_count": _GOLDEN_TOOLS_COUNT,
        },
        "response": expected_response,
    }
    cassette = tmp_path / "golden.jsonl"
    cassette.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    backend = PlaybackBackend(cassette_path=cassette)

    # The standalone helper and the backend's lookup must agree on the key; if
    # they did not, the record below would not resolve and CassetteMiss would
    # raise instead of returning the recorded response.
    helper_key = request_key_for_payload(
        _GOLDEN_MODEL,
        _GOLDEN_MESSAGES,
        _GOLDEN_TOOLS_COUNT,
    )
    assert helper_key == _GOLDEN_HEX

    response = _run(
        backend.messages_create(
            model=_GOLDEN_MODEL,
            system="sys",
            messages=_GOLDEN_MESSAGES,
            tools=[{"name": f"t{i}"} for i in range(_GOLDEN_TOOLS_COUNT)],
            max_tokens=16,
            timeout=1.0,
        )
    )
    assert isinstance(response, MessageResponse)
    assert response.id == "msg_golden"


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
