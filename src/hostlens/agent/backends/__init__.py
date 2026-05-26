"""LLM backend implementations (M2 add-llm-backend-protocol).

Three concrete backends ship in M2:

- ``AnthropicAPIBackend`` — production default; thin adapter over the
  Anthropic SDK with ``max_retries=0`` and full exception wrapping.
- ``FakeBackend`` — unit test stub returning canned ``MessageResponse``
  objects in sequence.
- ``PlaybackBackend`` — integration-test cassette replay; raises
  ``CassetteMiss`` (subclass of ``BackendError``) on lookup miss.

The single-source factory ``hostlens.agent.backend.create_backend`` (added
in group 4) is the only sanctioned entry point — direct instantiation is
allowed in tests but discouraged elsewhere.
"""

from __future__ import annotations

from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.backends.playback import CassetteMiss, PlaybackBackend

__all__ = [
    "AnthropicAPIBackend",
    "CassetteMiss",
    "FakeBackend",
    "PlaybackBackend",
]
