"""Single-source request-key algorithm for cassette testing.

The cassette request key hashes only ``model`` / ``messages`` / ``tools_count``
(``system`` / ``max_tokens`` / full ``tools`` content / ``timeout`` excluded) so
a system-prompt iteration does not invalidate the whole cassette set; tools
schema drift is detected separately by ``scripts/cassette_lint.py``.

``PlaybackBackend`` (replay lookup), ``RecordingBackend`` (write canonical
request), and ``cassette_lint.py`` (duplicate-key detection) all share this
helper so the keying algorithm has a single source — any drift in serialization
parameters / projection detail would otherwise cause "lint says no duplicate but
playback actually collides" or "recorder-written key != playback-read key".

This module is intentionally side-effect free and does NOT import any backend.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["project_messages_drop_thinking", "request_key_for_payload"]

# Inbound ``thinking`` / ``redacted_thinking`` blocks carry provider-generated,
# non-deterministic ``thinking`` text / ``signature`` (and ``extra="allow"``
# private fields) that get relayed back into ``messages`` across turns. Hashing
# them would make every record of the same logical request produce a different
# key, so record→replay would never hit. Both block ``type``s are dropped
# whole — not field-by-field — because ``extra="allow"`` means any residual
# field would still destabilize the hash.
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def project_messages_drop_thinking(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with every thinking/redacted block dropped.

    Side-effect free: the input list and its messages are never mutated. Only
    ``content`` entries that are dicts whose ``type`` is ``"thinking"`` or
    ``"redacted_thinking"`` are removed (the whole block, including any
    ``extra="allow"`` provider fields); string ``content`` and all other block
    types pass through unchanged. For thinking-free ``messages`` this is the
    identity projection.

    This is the **single source** of the thinking-drop rule. Both
    ``request_key_for_payload`` (keying) and ``RecordingBackend`` (canonical
    request persistence) call it — neither may inline a second drop
    implementation, or the two projections could drift.
    """

    projected: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            projected.append(message)
            continue
        kept = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES)
        ]
        if len(kept) == len(content):
            projected.append(message)
            continue
        projected.append({**message, "content": kept})
    return projected


def request_key_for_payload(
    model: str,
    messages: list[dict[str, Any]],
    tools_count: int,
) -> str:
    """Compute the SHA256 request key for cassette lookup.

    ``messages`` is projected through ``project_messages_drop_thinking`` before
    hashing so non-deterministic inbound thinking blocks (relayed back across
    turns) do not destabilize the key — record→replay matching depends solely
    on this projection, not on whether the persisted cassette body was stripped.

    ``sort_keys=True`` makes the hash order-independent; ``ensure_ascii=False``
    keeps non-ASCII characters (e.g. Chinese user prompts) byte-stable across
    platforms. For thinking-free ``messages`` the projection is the identity, so
    the output stays byte-for-byte identical to the algorithm that was
    previously inlined in ``PlaybackBackend._request_key`` — golden tests pin
    this equivalence.
    """

    payload = {
        "model": model,
        "messages": project_messages_drop_thinking(messages),
        "tools_count": tools_count,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
