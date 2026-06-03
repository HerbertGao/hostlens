"""Unit tests for the thinking-normalizing cassette request-key projection.

Covers spec §需求:request-key 算法必须单一来源 scenarios introduced by
``tolerate-inbound-thinking``:

- 场景:含 thinking 块的多轮 messages keying 稳定 — two records of the same
  logical multi-turn request whose ``thinking`` / ``signature`` differ
  (provider non-determinism) hash to the same key.
- 场景:keying 投影丢整块而非丢字段 — a thinking block carrying ``extra="allow"``
  private fields is dropped whole, so no field of it affects the key.
- 场景:三处 keying 同源 — the helper, ``PlaybackBackend._record_request_key``,
  and ``cassette_lint.request_key_for_record`` agree on the hex.
- 场景:key 匹配不依赖落盘 request 是否 strip thinking — a persisted record whose
  ``request.messages`` STILL contains thinking blocks, re-keyed via
  ``_record_request_key``, matches the live key from ``request_key_for_payload``
  for the equivalent thinking-bearing request.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.agent.cassette_key import (
    project_messages_drop_thinking,
    request_key_for_payload,
)

# ``scripts/`` is not an importable package; load the duplicate-key helper the
# lint uses so the three-source equality covers the real lint code path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from cassette_lint import request_key_for_record

_MODEL = "deepseek-v4-pro"
_TOOLS_COUNT = 2


def _multiturn(thinking_text: str, signature: str) -> list[dict[str, Any]]:
    """A multi-turn payload whose only inter-record difference is thinking."""

    return [
        {"role": "user", "content": "检查磁盘"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": thinking_text, "signature": signature},
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "disk_inspector",
                    "input": {"path": "/"},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"}],
        },
    ]


def test_thinking_nondeterminism_does_not_change_key() -> None:
    key_a = request_key_for_payload(_MODEL, _multiturn("reasoning A", "sig-1"), _TOOLS_COUNT)
    key_b = request_key_for_payload(_MODEL, _multiturn("reasoning B", "sig-2"), _TOOLS_COUNT)
    assert key_a == key_b


def test_projection_drops_whole_block_including_extra_fields() -> None:
    """``extra="allow"`` private fields on a thinking block do not affect the key."""

    base = _multiturn("reasoning A", "sig-1")
    with_extra = _multiturn("reasoning A", "sig-1")
    # Inject a provider-private field onto the thinking block of the second one.
    with_extra[1]["content"][0]["provider_private"] = {"nested": [1, 2, 3]}

    assert request_key_for_payload(_MODEL, base, _TOOLS_COUNT) == request_key_for_payload(
        _MODEL, with_extra, _TOOLS_COUNT
    )


def test_redacted_thinking_block_dropped() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "检查磁盘"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "opaque-blob-A"},
                {"type": "text", "text": "结果"},
            ],
        },
    ]
    stripped: list[dict[str, Any]] = [
        {"role": "user", "content": "检查磁盘"},
        {"role": "assistant", "content": [{"type": "text", "text": "结果"}]},
    ]
    assert request_key_for_payload(_MODEL, messages, _TOOLS_COUNT) == request_key_for_payload(
        _MODEL, stripped, _TOOLS_COUNT
    )


def test_projection_is_side_effect_free() -> None:
    messages = _multiturn("reasoning A", "sig-1")
    before = [dict(m) for m in messages]
    projected = project_messages_drop_thinking(messages)
    # Input untouched.
    assert messages == before
    assert messages[1]["content"][0]["type"] == "thinking"
    # Output has the thinking block removed.
    assert all(
        block.get("type") != "thinking"
        for m in projected
        for block in (m["content"] if isinstance(m["content"], list) else [])
    )


def test_three_sources_agree_on_key() -> None:
    """Helper, PlaybackBackend record-key, and lint record-key all match.

    All three delegate to the one ``request_key_for_payload`` source, so this
    is an anti-drift guard (it fails if any source re-inlines a divergent key
    algorithm), not an independent re-derivation of the normalization.
    """

    messages = _multiturn("reasoning A", "sig-1")
    helper_key = request_key_for_payload(_MODEL, messages, _TOOLS_COUNT)

    # The cassette ``request`` body STILL contains thinking (落盘未 strip case):
    # each source must re-normalize on read and land on the same hex.
    record = {
        "request": {
            "model": _MODEL,
            "messages": messages,
            "tools_count": _TOOLS_COUNT,
        }
    }
    playback_key = PlaybackBackend._record_request_key(record["request"])
    lint_key = request_key_for_record(record)

    assert helper_key == playback_key == lint_key


def test_record_key_independent_of_persisted_strip() -> None:
    """Matching does not depend on whether the persisted body was stripped.

    A live request keyed via ``request_key_for_payload`` (thinking-bearing) must
    equal ``_record_request_key`` of a persisted record whose ``messages`` STILL
    carry thinking (i.e. persistence-time strip skipped/rolled back). This pins
    that record→replay matching is guaranteed by the shared helper, not by the
    persisted body being stripped.
    """

    live_messages = _multiturn("reasoning A", "sig-1")
    live_key = request_key_for_payload(_MODEL, live_messages, _TOOLS_COUNT)

    persisted_request_unstripped = {
        "model": _MODEL,
        # Differs from the live thinking text/signature AND is unstripped — the
        # re-normalization on read drops both, so the keys still coincide.
        "messages": _multiturn("a totally different trace", "sig-99"),
        "tools_count": _TOOLS_COUNT,
    }
    replay_key = PlaybackBackend._record_request_key(persisted_request_unstripped)

    assert live_key == replay_key
