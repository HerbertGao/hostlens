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

__all__ = ["request_key_for_payload"]


def request_key_for_payload(
    model: str,
    messages: list[dict[str, Any]],
    tools_count: int,
) -> str:
    """Compute the SHA256 request key for cassette lookup.

    ``sort_keys=True`` makes the hash order-independent; ``ensure_ascii=False``
    keeps non-ASCII characters (e.g. Chinese user prompts) byte-stable across
    platforms. The output is byte-for-byte identical to the algorithm that was
    previously inlined in ``PlaybackBackend._request_key`` — golden tests pin
    this equivalence.
    """

    payload = {
        "model": model,
        "messages": messages,
        "tools_count": tools_count,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
