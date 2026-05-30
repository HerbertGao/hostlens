"""Golden + same-source tests for the cassette request-key helper.

Covers spec §需求:request-key 算法必须单一来源:
- 场景:三处同源 — ``request_key_for_payload`` and ``PlaybackBackend`` lookup
  path produce the identical hex for the same ``(model, messages, tools_count)``
  payload.
- 场景:重构不改 playback hash (golden) — the helper hash for a fixed payload
  equals a hard-coded golden value computed from the M2.1 ``PlaybackBackend``
  algorithm, pinning the refactor as behaviour-equivalent.
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


def test_request_key_matches_golden_hash() -> None:
    assert (
        request_key_for_payload(
            _GOLDEN_MODEL,
            _GOLDEN_MESSAGES,
            _GOLDEN_TOOLS_COUNT,
        )
        == _GOLDEN_HEX
    )


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
