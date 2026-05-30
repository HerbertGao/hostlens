"""``RecordingBackend`` + ``guard_record_targets`` — cassette recording layer.

Per design.md D-1 / D-2 / D-7 this is the **record half** of the cassette
loop, and it is test-support only (one-way import tests → src). The
``RecordingBackend`` wraps a real ``AnthropicAPIBackend``, collects an entire
scenario's ``(request, response)`` pairs in memory, runs a detect-and-reject
sensitive gate over both the canonical request and the response before
persisting, and atomically overwrites the whole cassette file at scenario
end (temp file + ``os.replace``; never append).

The real-target guard lives here (assembly layer) rather than inside
``messages_create`` because ``LLMBackend.messages_create`` has no
``target`` / ``ToolContext`` in its signature — the backend cannot see the
target it would leak (D-1).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hostlens.agent.backend import BackendCapabilities, MessageResponse
from hostlens.core.redact import detect_sensitive_text

if TYPE_CHECKING:
    from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
    from hostlens.targets.registry import TargetRegistry

__all__ = ["RecordingBackend", "guard_record_targets"]


# Process-wide registry of cassette paths an active ``RecordingBackend`` is
# writing. Shared across instances so a second recorder pointed at the same
# ``cassette_path`` in the same pytest run fails fast instead of silently
# clobbering the first recorder's data on teardown (spec §需求:record 模式必须
# 防止同一 cassette 路径被并发/多实例覆盖). Cross-process collisions cannot be
# detected through an in-process set, which is why xdist is rejected outright.
_ACTIVE_CASSETTE_PATHS: set[Path] = set()


# Fixed marker an assembly-layer fixture stamps onto ``TargetEntry.tags`` to
# declare a ``local`` target is a byte-stable synthetic stand-in (not the real
# local machine). Pinned to a literal string so the guard rule cannot drift.
_SYNTHETIC_TARGET_TAG = "cassette-synthetic"


class RecordingBackend:
    """Cassette-recording backend wrapping a live ``AnthropicAPIBackend``.

    Implements the ``LLMBackend`` Protocol structurally (``name`` /
    ``capabilities`` / ``messages_create``). Does NOT implement
    ``BackendDiagnostics`` — it is a pytest-only recorder, never reached by
    ``hostlens doctor`` / daemon paths.

    Lifecycle:

    1. ``__init__`` — reject xdist, reject duplicate active path, then
       register the path as the **last** init step (nothing can fail after
       registration, so no rollback is needed).
    2. ``messages_create`` — call the inner backend, run the detect-reject
       gate over both canonical request and response, accumulate the record.
       Any gate hit or any other exception poisons the recorder.
    3. ``flush`` — atomically overwrite the cassette when clean; no-op (only
       deregister) when poisoned or already flushed. Idempotent.
    """

    name: str = "recording"

    def __init__(
        self,
        *,
        cassette_path: Path,
        inner: AnthropicAPIBackend,
    ) -> None:
        # Order is contractual (spec §需求:record 模式必须防止...覆盖 +
        # §需求:poisoned 状态 (d)): xdist check → collision check →
        # registration as the final statement. Because registration is last
        # and nothing after it can raise, a construction failure never leaves
        # a stale path in the registry.
        if "PYTEST_XDIST_WORKER" in os.environ:
            raise RuntimeError(
                "RecordingBackend does not support pytest-xdist concurrency: "
                "the in-process active-cassette registry cannot be shared "
                "across worker processes, so it cannot prevent two workers "
                "from overwriting the same cassette. Run record mode without "
                "xdist (e.g. drop '-n auto')."
            )
        if cassette_path in _ACTIVE_CASSETTE_PATHS:
            raise RuntimeError(
                f"cassette {cassette_path.name!r} is already being recorded by "
                "another active RecordingBackend in this run; two recorders "
                "writing the same cassette would silently clobber each other. "
                "Give each parametrized case a distinct cassette name."
            )

        self._cassette_path: Path = cassette_path
        self._inner: AnthropicAPIBackend = inner
        self.capabilities: BackendCapabilities = inner.capabilities
        self._records: list[dict[str, Any]] = []
        self._poisoned: bool = False
        self._flushed: bool = False

        # LAST statement — registration must be unconditionally reachable and
        # have no failure point after it.
        _ACTIVE_CASSETTE_PATHS.add(cassette_path)

    async def messages_create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> MessageResponse:
        try:
            response = await self._inner.messages_create(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except BaseException:
            # Any inner failure (network, capability violation, cancellation)
            # poisons the recorder so teardown ``flush`` will not persist a
            # partial scenario.
            self._poisoned = True
            raise

        # Project the request to the same canonical subset the keying helper
        # hashes; this is exactly what gets written to the cassette and what
        # ``PlaybackBackend`` reads back, so it is the surface to gate.
        #
        # ``messages`` is snapshot via a JSON round-trip: the Agent loop reuses
        # and mutates a single ``messages`` list in place across turns (and
        # injects rolling ``cache_control`` into per-turn copies), so storing
        # the live reference would leave every record pointing at the final
        # mutated list — replay would then ``CassetteMiss`` even though each
        # turn was recorded. The deep copy also makes the stored shape identical
        # to what ``PlaybackBackend`` re-deserializes from the file.
        messages_snapshot: list[dict[str, Any]] = json.loads(
            json.dumps(messages, ensure_ascii=False)
        )
        canonical_request: dict[str, Any] = {
            "model": model,
            "messages": messages_snapshot,
            "tools_count": len(tools),
        }

        try:
            request_text = json.dumps(canonical_request, sort_keys=True, ensure_ascii=False)
            response_text = response.model_dump_json()
            # Detect-and-reject gate over BOTH sides: a synthetic ``tool_result``
            # may smuggle a tmp path / username / hostname into the request
            # ``messages`` as well as the response (spec §需求:写盘前必须对
            # request 与 response 都跑敏感检测门禁). This is detection, NOT
            # scrubbing — a hit fails the call without rewriting either side.
            request_hit = detect_sensitive_text(request_text)
            if request_hit is not None:
                raise SensitiveCassetteContentError(rule_name=request_hit, side="request")
            response_hit = detect_sensitive_text(response_text)
            if response_hit is not None:
                raise SensitiveCassetteContentError(rule_name=response_hit, side="response")
        except BaseException:
            # A gate hit (or any serialization failure) poisons the recorder
            # and the offending record is NOT accumulated and NOT written.
            self._poisoned = True
            raise

        # ``tools_schema_hash`` uses default ``ensure_ascii`` to match the CI
        # ``--current-tools-hash`` computation in the llm-backend-protocol
        # spec (NOT the request-key's ``ensure_ascii=False``); a mismatch
        # would make non-ASCII tool schemas falsely report drift.
        tools_schema_hash = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()

        self._records.append(
            {
                "request": canonical_request,
                "response": json.loads(response_text),
                "tools_schema_hash": tools_schema_hash,
            }
        )
        return response

    def flush(self, *, persist: bool = True) -> None:
        """Persist the scenario, or no-op (deregister only) when it must not write.

        Idempotent (spec §需求:poisoned (c)): teardown may call this more than
        once. The write is **skipped** (only the active path is deregistered) in
        any of these cases, so a committed cassette is never clobbered by a
        broken / partial / empty recording:

        - ``self._poisoned`` — a ``messages_create`` gate hit or error occurred
          (spec §需求:poisoned 状态...teardown 不得写出部分 cassette).
        - ``self._flushed`` — already flushed.
        - ``persist is False`` — the caller (fixture teardown) detected the test
          FAILED; persisting a recording from a failing run would overwrite a
          good committed cassette with a truncated/wrong scenario.
        - ``not self._records`` — nothing was recorded; writing would replace an
          existing cassette with an empty file.
        """

        if self._poisoned or self._flushed or not persist or not self._records:
            self._flushed = True
            self._deregister()
            return

        try:
            self._atomic_write()
            # Mark flushed ONLY after a successful write, so a failed
            # ``_atomic_write`` (e.g. ``os.replace`` error) leaves the recorder
            # retry-able rather than silently dropping the in-memory records.
            self._flushed = True
        finally:
            # Deregister even if the write raised, so a write failure does not
            # leave the path occupied for the rest of the run.
            self._deregister()

    def _atomic_write(self) -> None:
        """Overwrite the whole cassette atomically (temp file + rename).

        Never appends. The temp file is written fully then ``os.replace``d
        over the target, so an interruption mid-write leaves the original
        cassette intact (or absent) rather than a half-written JSONL (spec
        §场景:录制中断不留半写文件).
        """

        tmp_path = self._cassette_path.with_suffix(".tmp")
        lines = [json.dumps(record, ensure_ascii=False) for record in self._records]
        body = "\n".join(lines)
        if body:
            body += "\n"
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, self._cassette_path)

    def _deregister(self) -> None:
        _ACTIVE_CASSETTE_PATHS.discard(self._cassette_path)


class SensitiveCassetteContentError(RuntimeError):
    """Raised when request/response text hits a ``CASSETTE_SENSITIVE_PATTERNS`` rule.

    The message names the firing rule and which side (request/response) hit,
    but deliberately does NOT echo the matched secret value (spec §需求:写盘前
    必须对 request 与 response 都跑敏感检测门禁 — 禁止回显命中的敏感原值).
    """

    def __init__(self, *, rule_name: str, side: str) -> None:
        super().__init__(
            f"sensitive content detected in {side} cassette payload "
            f"(rule={rule_name}); refusing to record. The matched value is "
            "not echoed. Use byte-stable synthetic fixtures only."
        )
        self.rule_name: str = rule_name
        self.side: str = side


def guard_record_targets(
    target_registry: TargetRegistry,
    *,
    allow_real: bool,
) -> None:
    """Reject recording against real targets unless explicitly allowed.

    Per spec §需求:record 模式必须由 fixture 强制在装配层拒绝真实 target this
    runs at the **assembly layer** (it can see ``target_registry``), not in
    ``RecordingBackend.messages_create`` (which cannot). Classification:

    - ``type ∈ {ssh, docker, k8s}`` → always real.
    - ``type == local`` → synthetic only when its ``TargetEntry.tags`` contains
      the fixed ``"cassette-synthetic"`` marker; a bare local target points at
      the real machine and is treated as real.

    When any real target is present and ``allow_real is False`` (i.e.
    ``HOSTLENS_ALLOW_REAL_TARGET_RECORD`` is not set to ``1``) this raises.
    The error names neither host nor credentials (spec §场景:默认拒绝真实
    target — 不回显 host / 凭据).
    """

    if allow_real:
        return

    for target in target_registry.list():
        entry = target_registry.get_entry(target.name)
        is_synthetic = target.type == "local" and _SYNTHETIC_TARGET_TAG in entry.tags
        if not is_synthetic:
            raise RuntimeError(
                "record mode refuses to record against a real target "
                f"(type={target.type!r}); recording would write a real "
                "hostname / IP / path into the committed cassette. Set "
                "HOSTLENS_ALLOW_REAL_TARGET_RECORD=1 to override (risky), or "
                "use a synthetic local target tagged "
                f"{_SYNTHETIC_TARGET_TAG!r}."
            )
