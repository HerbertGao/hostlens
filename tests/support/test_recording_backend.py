"""Unit tests for ``RecordingBackend`` (tests/support, group D).

Zero-API: the inner ``AnthropicAPIBackend`` is replaced by a ``FakeBackend``
(or a tiny scripted stand-in) so nothing here touches the network.
``RecordingBackend.__init__`` is typed ``inner: AnthropicAPIBackend`` for
production clarity but consumes it structurally, so the fake is ``cast`` in.

The module-level ``_ACTIVE_CASSETTE_PATHS`` registry is shared across
instances; the ``_clean_active_paths`` autouse fixture clears it before and
after each test so a leaked path never poisons a sibling test.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from hostlens.agent.backend import (
    MessageResponse,
    TextBlock,
    Usage,
)
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.cassette_key import request_key_for_payload

from .cassette_recording import (
    _ACTIVE_CASSETTE_PATHS,
    RecordingBackend,
    SensitiveCassetteContentError,
)


@pytest.fixture(autouse=True)
def _clean_active_paths() -> Any:
    _ACTIVE_CASSETTE_PATHS.clear()
    yield
    _ACTIVE_CASSETTE_PATHS.clear()


def _response(text: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _fake_inner(responses: list[MessageResponse]) -> AnthropicAPIBackend:
    return cast(AnthropicAPIBackend, FakeBackend(responses=responses))


def _recorder(cassette_path: Path, responses: list[MessageResponse]) -> RecordingBackend:
    return RecordingBackend(cassette_path=cassette_path, inner=_fake_inner(responses))


async def _call(
    backend: RecordingBackend,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> MessageResponse:
    return await backend.messages_create(
        model="claude-test",
        system="you are a test",
        messages=messages,
        tools=tools if tools is not None else [],
        max_tokens=100,
        timeout=30.0,
    )


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ---------------------------------------------------------------------------
# §需求:RecordingBackend 必须内存收集整个 scenario 并原子 overwrite 写盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_writes_distinct_records(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("r1"), _response("r2"), _response("r3")])

    # messages grow each turn (multi-turn Agent loop), so request keys differ.
    await _call(backend, messages=[{"role": "user", "content": "a"}])
    await _call(
        backend,
        messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
    )
    await _call(
        backend,
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
    )
    backend.flush()

    records = _read_records(cassette)
    assert len(records) == 3
    keys = [
        request_key_for_payload(
            model=r["request"]["model"],
            messages=r["request"]["messages"],
            tools_count=r["request"]["tools_count"],
        )
        for r in records
    ]
    assert len(set(keys)) == 3
    for r in records:
        assert set(r["request"].keys()) == {"model", "messages", "tools_count"}


@pytest.mark.asyncio
async def test_record_carries_tools_schema_hash(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("ok")])
    tools = [{"name": "t", "input_schema": {"type": "object"}}]

    await _call(backend, messages=[{"role": "user", "content": "x"}], tools=tools)
    backend.flush()

    records = _read_records(cassette)
    assert len(records) == 1
    expected = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
    assert records[0]["tools_schema_hash"] == expected
    assert records[0]["tools_schema_hash"]


@pytest.mark.asyncio
async def test_rerecord_overwrites_whole_file(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    cassette.write_text('{"stale": "old"}\n{"stale": "old2"}\n', encoding="utf-8")

    backend = _recorder(cassette, [_response("new")])
    await _call(backend, messages=[{"role": "user", "content": "x"}])
    backend.flush()

    records = _read_records(cassette)
    assert len(records) == 1
    assert "stale" not in records[0]


@pytest.mark.asyncio
async def test_interrupted_write_leaves_no_half_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = tmp_path / "scenario.jsonl"
    cassette.write_text('{"original": true}\n', encoding="utf-8")

    backend = _recorder(cassette, [_response("new")])
    await _call(backend, messages=[{"role": "user", "content": "x"}])

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        backend.flush()

    # The original file is untouched (atomic rename never landed) and the
    # active path was still released (try/finally).
    assert _read_records(cassette) == [{"original": True}]
    assert cassette not in _ACTIVE_CASSETTE_PATHS


# ---------------------------------------------------------------------------
# §需求:写盘前必须对 request 与 response 都跑敏感检测门禁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_sensitive_rejected(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("see /Users/alice/secret")])

    with pytest.raises(SensitiveCassetteContentError) as exc_info:
        await _call(backend, messages=[{"role": "user", "content": "clean"}])

    assert exc_info.value.rule_name == "user_home_path"
    assert "alice" not in str(exc_info.value)
    backend.flush()
    assert not cassette.exists()


@pytest.mark.asyncio
async def test_request_tool_result_sensitive_rejected(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("clean response")])

    # Previous-turn tool_result smuggled a real home path into the request.
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "/Users/alice/data"}
            ],
        },
    ]
    with pytest.raises(SensitiveCassetteContentError) as exc_info:
        await _call(backend, messages=messages)

    assert exc_info.value.rule_name == "user_home_path"
    assert exc_info.value.side == "request"
    assert "alice" not in str(exc_info.value)
    backend.flush()
    assert not cassette.exists()


@pytest.mark.asyncio
async def test_clean_record_persisted(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("connection refused, retrying")])

    await _call(backend, messages=[{"role": "user", "content": "hello world"}])
    backend.flush()

    records = _read_records(cassette)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# §需求:任一检测门禁命中或调用异常后 recorder 必须进入 poisoned 状态
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poisoned_after_gate_hit_flush_is_noop(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("ok turn 1"), _response("leak /home/bob/x")])

    # Turn 1 clean (accumulated), turn 2 hits the gate.
    await _call(backend, messages=[{"role": "user", "content": "1"}])
    with pytest.raises(SensitiveCassetteContentError):
        await _call(backend, messages=[{"role": "user", "content": "2"}])

    backend.flush()
    # Poisoned → nothing written despite one clean record accumulated.
    assert not cassette.exists()
    assert cassette not in _ACTIVE_CASSETTE_PATHS


@pytest.mark.asyncio
async def test_poisoned_after_inner_exception(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    # FakeBackend exhausts → IndexError on first call.
    backend = _recorder(cassette, [])

    with pytest.raises(IndexError):
        await _call(backend, messages=[{"role": "user", "content": "x"}])

    backend.flush()
    assert not cassette.exists()
    assert cassette not in _ACTIVE_CASSETTE_PATHS


@pytest.mark.asyncio
async def test_flush_idempotent(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("ok")])
    await _call(backend, messages=[{"role": "user", "content": "x"}])

    backend.flush()
    first = _read_records(cassette)
    # Second flush is a no-op: no raise, no double write, registry untouched.
    backend.flush()
    assert _read_records(cassette) == first
    assert cassette not in _ACTIVE_CASSETTE_PATHS


# ---------------------------------------------------------------------------
# §需求:record 模式必须防止同一 cassette 路径被并发/多实例覆盖
# ---------------------------------------------------------------------------


def test_duplicate_path_fails_fast(tmp_path: Path) -> None:
    cassette = tmp_path / "scenario.jsonl"
    _recorder(cassette, [_response("a")])
    with pytest.raises(RuntimeError, match="already being recorded"):
        _recorder(cassette, [_response("b")])


def test_xdist_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    with pytest.raises(RuntimeError, match="xdist"):
        _recorder(tmp_path / "scenario.jsonl", [_response("a")])


def test_construction_failure_leaves_no_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = tmp_path / "scenario.jsonl"

    # xdist check runs BEFORE registration, so a failed construction must not
    # leave the path registered: a later construction succeeds.
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    with pytest.raises(RuntimeError, match="xdist"):
        _recorder(cassette, [_response("a")])
    monkeypatch.delenv("PYTEST_XDIST_WORKER")

    assert cassette not in _ACTIVE_CASSETTE_PATHS
    backend = _recorder(cassette, [_response("a")])
    assert cassette in _ACTIVE_CASSETTE_PATHS
    backend.flush()


# ---------------------------------------------------------------------------
# Review regressions (Bugbot/Copilot): teardown must never clobber a committed
# cassette with an empty / partial / failed-run recording.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_flush_does_not_clobber_committed_cassette(tmp_path: Path) -> None:
    """#1: flush() with zero recorded calls must NOT overwrite an existing
    cassette with an empty file (a record run that exits before any LLM call)."""
    cassette = tmp_path / "committed.jsonl"
    committed = {"request": {"model": "m", "messages": [], "tools_count": 0}, "response": {}}
    cassette.write_text(json.dumps(committed) + "\n", encoding="utf-8")

    backend = _recorder(cassette, [_response("unused")])
    # No messages_create call → _records empty.
    backend.flush()

    assert _read_records(cassette) == [committed]  # untouched, not emptied
    assert cassette not in _ACTIVE_CASSETTE_PATHS


@pytest.mark.asyncio
async def test_flush_persist_false_does_not_write(tmp_path: Path) -> None:
    """#2: a FAILED test teardown passes persist=False; the recorded (clean but
    possibly partial) scenario must NOT overwrite the committed cassette."""
    cassette = tmp_path / "committed.jsonl"
    cassette.write_text('{"keep": true}\n', encoding="utf-8")

    backend = _recorder(cassette, [_response("recorded")])
    await _call(backend, messages=[{"role": "user", "content": "x"}])
    backend.flush(persist=False)

    assert _read_records(cassette) == [{"keep": True}]  # not overwritten
    assert cassette not in _ACTIVE_CASSETTE_PATHS


@pytest.mark.asyncio
async def test_write_failure_keeps_records_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3: a failed _atomic_write must NOT mark the recorder flushed — the
    in-memory records survive so a retry can still persist them."""
    cassette = tmp_path / "scenario.jsonl"
    backend = _recorder(cassette, [_response("ok")])
    await _call(backend, messages=[{"role": "user", "content": "x"}])

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", _flaky)
    with pytest.raises(OSError, match="disk full"):
        backend.flush()

    # Retry now succeeds because the first failure did not mark _flushed.
    backend.flush()
    assert len(_read_records(cassette)) == 1
