"""Tests for the ``hostlens inspect --intent`` Planner Agent CLI path (Group 3b).

Spec: ``openspec/changes/add-intent-cli/specs/inspect-cli-command/spec.md``.

These tests drive ``hostlens.cli.main`` (the project entrypoint) so the
``click.UsageError`` → exit 3 wrapper runs, mirroring ``tests/cli/test_inspect.py``.
``_run_main`` patches ``sys.argv`` and captures the ``SystemExit`` the wrapper
raises (``CliRunner.invoke`` is intentionally NOT used — it calls the raw Typer
``app`` and would observe Click's default exit 2 for usage errors).

The backend factory is the only seam these tests replace: several cases
monkeypatch ``hostlens.cli._intent.create_backend`` so the CLI runs its full
``_run_intent`` + ``RichLiveObserver`` + ``render_planner_result`` path while a
deterministic backend (scripted ``FakeBackend`` / record-then-replay
``PlaybackBackend`` / a persistently-failing fake) stands in for a paid API.
This is the orchestrator-endorsed low-friction approach: the unit under test is
the CLI path, not the backend factory. ``asyncio_mode = "auto"`` (pyproject) —
no ``@pytest.mark.asyncio``; no ``@pytest.mark.live`` (every backend is fake).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from hostlens.agent.backend import (
    BackendCapabilities,
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.cli import main

# --------------------------------------------------------------------------- #
# Backend / settings injection fixtures
# --------------------------------------------------------------------------- #
#
# The ``--intent`` path calls ``load_settings()`` then ``create_backend(settings)``.
# ``create_backend`` raises ``ConfigError`` when ``settings.backend is None`` (no
# LLM block configured). We provide a configured backend block by env so the
# AgentLoop's ``settings.agent`` is non-None; the actual backend object is
# swapped at ``hostlens.cli._intent.create_backend`` so no real API is hit.


_DEFAULT_CAPS = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)

# run_inspector tool_use input must satisfy RunInspectorInput (extra="forbid").
# Points at the local-host target + hello.echo inspector wired by the fixtures.
_RUN_INSPECTOR_INPUT = {"target_name": "local-host", "inspector_name": "hello.echo"}


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a tmp file with one local target."""

    path = tmp_path / "targets.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local-host", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point inspectors search paths at an empty user dir (builtins stay visible)."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


@pytest.fixture
def agent_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a ``backend`` + ``agent`` namespace via env.

    ``backend.type=anthropic_api`` + a dummy ``api_key`` makes ``load_settings``
    build a non-None ``settings.backend`` / ``settings.agent`` (the AgentLoop
    requires ``settings.agent``). The backend object itself is replaced by
    monkeypatching ``create_backend`` in the tests, so the dummy key is never
    used to reach the network.
    """

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("HOSTLENS_AGENT__PRIMARY_MODEL", "claude-test")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the loop retry backoff so the degraded-path test does not sleep."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("hostlens.agent.loop.asyncio.sleep", _instant)


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` with patched argv; return (code, stdout, stderr)."""

    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- #
# MessageResponse builders (shared with the planner test shapes)
# --------------------------------------------------------------------------- #


def _msg(
    *,
    content: list[Any],
    stop_reason: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _tool_use_turn(*, block_id: str) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="run_inspector",
                input=_RUN_INSPECTOR_INPUT,
            )
        ],
        stop_reason="tool_use",
    )


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: LLMBackend) -> None:
    """Replace ``create_backend`` in the CLI intent module with a constant factory.

    This is the single seam: the CLI still goes through ``build_planner`` →
    ``PlannerAgent`` → ``AgentLoop`` → ``_run_intent`` → observer → render, but
    talks to ``backend`` instead of a real API.
    """

    monkeypatch.setattr("hostlens.cli._intent.create_backend", lambda _settings: backend)


# --------------------------------------------------------------------------- #
# Record-then-replay cassette (5.3⑤/⑥): drive the loop once with a scripted
# FakeBackend to capture exact multi-turn request keys, then replay via a real
# PlaybackBackend. Hand-writing the messages would be miss-prone.
# --------------------------------------------------------------------------- #


class _RecordingBackend:
    """Wrap a ``FakeBackend`` and record each request/response as a cassette line.

    Mirrors ``tests/agent/test_planner.py::_RecordingBackend`` so the recorded
    request keys match what the loop sends on replay.
    """

    name = "recording"

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner
        self.capabilities = inner.capabilities
        self.records: list[dict[str, Any]] = []

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
        resp = await self._inner.messages_create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        # Deep-copy messages: the loop mutates one list in place across turns,
        # so a by-reference capture would leave records pointing at the final
        # mutated list (key mismatch on replay).
        self.records.append(
            {
                "request": {
                    "model": model,
                    "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
                    "tools_count": len(tools),
                },
                "response": resp.model_dump(mode="json"),
            }
        )
        return resp


def _record_cassette(
    tmp_path: Path,
    responses: list[MessageResponse],
) -> Path:
    """Record a cassette by running the full Planner over a scripted FakeBackend.

    Uses the SAME default tool registry + a fresh local TargetRegistry /
    InspectorRegistry that the CLI will assemble, so ``tools_count`` (part of
    the cassette request key) matches on replay. We re-import the CLI helper
    ``build_planner`` so the recorded loop is byte-for-byte the loop the CLI
    runs — only the backend differs (recording vs playback).
    """

    import asyncio
    import logging

    import structlog

    from hostlens.cli._intent import build_planner
    from hostlens.cli.inspect import _load_inspector_registry, _load_target_registry
    from hostlens.core.config import load_settings

    settings = load_settings()
    target_registry = _load_target_registry()
    inspector_registry = _load_inspector_registry(settings)
    logger = cast(structlog.stdlib.BoundLogger, structlog.get_logger("record"))

    recorder = _RecordingBackend(FakeBackend(responses=responses))

    # Build a planner exactly as the CLI would, but with the recording backend.
    # Reuse build_planner via a temporary create_backend swap so the wiring
    # (tool registry / context factory) is identical to production — only the
    # backend differs (recording vs playback).
    import hostlens.cli._intent as intent_mod

    original = intent_mod.create_backend
    intent_mod.create_backend = lambda _s: cast(LLMBackend, recorder)  # type: ignore[assignment]
    try:
        planner = build_planner(settings, target_registry, inspector_registry, logger)
    finally:
        intent_mod.create_backend = original

    # The recording dispatch runs the real InspectorRunner, which emits
    # ``inspector_started`` at info to structlog's default (stdout) factory.
    # Without this guard those lines would land on the capsys-captured stdout
    # and corrupt the later json.loads assertion. Restore the snapshot after.
    saved = structlog.get_config()
    current = structlog.get_config()
    structlog.configure(
        processors=current["processors"],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        context_class=current["context_class"],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    try:
        asyncio.run(planner.run("检查这台机器的健康状况"))
    finally:
        structlog.configure(**saved)

    cassette_path = tmp_path / "intent_health_check.jsonl"
    with cassette_path.open("w", encoding="utf-8") as fp:
        for record in recorder.records:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cassette_path


# --------------------------------------------------------------------------- #
# 5.3① / ② — mutual exclusion usage errors
# --------------------------------------------------------------------------- #


def test_intent_both_missing_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither --inspector nor --intent -> exit 3 + "must provide exactly one".

    Spec §场景:缺 --inspector 且缺 --intent 报错.
    """

    exit_code, _stdout, stderr = _run_main(["inspect", "local-host"], capsys, monkeypatch)
    assert exit_code == 3
    assert "must provide exactly one of --inspector or --intent" in stderr
    assert "Traceback" not in stderr


def test_intent_both_set_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both --inspector and --intent -> exit 3 + "mutually exclusive".

    Spec §场景:--inspector 与 --intent 同时提供报错.
    """

    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "mutually exclusive" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# 5.3③ — inspector-only path not regressed (smoke)
# --------------------------------------------------------------------------- #


def test_inspector_only_path_still_works_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--inspector hello.echo`` (no --intent) still runs the M1 pipeline.

    Confirms making --inspector optional + adding the 0a mutual-exclusion gate
    did not break the existing single-inspector path (spec §场景:仅 --inspector
    走 M1 单 Inspector 管线 行为不变).
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert "Hostlens Inspection Report" in stdout


# --------------------------------------------------------------------------- #
# 5.3④ — backend not configured -> exit 3 pointing at doctor
# --------------------------------------------------------------------------- #


def test_intent_backend_not_configured_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--intent`` with no backend block -> exit 3 + doctor hint, no traceback.

    Spec §场景:backend 未配置报配置错误 — ``create_backend`` raises ConfigError
    (settings.backend is None) and the CLI maps it to exit 3.
    """

    # Deliberately do NOT use the agent_backend_env fixture so backend is None.
    # Clear any inherited backend env from the operator's shell / .env.
    for var in ("HOSTLENS_BACKEND__TYPE", "HOSTLENS_BACKEND__API_KEY"):
        monkeypatch.delenv(var, raising=False)

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "backend not configured" in stderr
    assert "hostlens doctor" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# 5.3⑤ — playback end-to-end: narrative + findings, progress on stderr, exit 0
# --------------------------------------------------------------------------- #


def test_intent_playback_end_to_end_md_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
    tmp_path: Path,
) -> None:
    """End-to-end ``--intent`` over a record-then-replay PlaybackBackend.

    Spec §场景:实时进度与报告分流 + §场景:md 模式输出综述与 findings 摘要 +
    §场景:健康巡检退出 0. The agent calls run_inspector (hello.echo on
    local-host → one info finding), then narrates; terminal_status=ok → exit 0.
    stdout carries the narrative + findings summary + telemetry; stderr carries
    the live progress tree (and nothing on stdout from progress).
    """

    cassette = _record_cassette(
        tmp_path,
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("机器健康，未发现严重问题。"),  # noqa: RUF001
        ],
    )
    _patch_backend(monkeypatch, PlaybackBackend(cassette_path=cassette))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查这台机器的健康状况"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    # Narrative + findings summary land on stdout.
    assert "机器健康，未发现严重问题。" in stdout  # noqa: RUF001
    assert "## Findings" in stdout
    assert "hello received" in stdout  # hello.echo emits an info finding
    # Telemetry line on stdout.
    assert "status=ok" in stdout
    # Live progress lands on stderr, not stdout.
    assert "run_inspector" in stderr
    assert "run_inspector" not in stdout


# --------------------------------------------------------------------------- #
# 5.3⑥ — json mode emits a parseable PlannerResult
# --------------------------------------------------------------------------- #


def test_intent_playback_json_mode_parseable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
    tmp_path: Path,
) -> None:
    """``--intent --format json`` -> stdout is a valid PlannerResult JSON.

    Spec §场景:json 模式输出可解析的 PlannerResult — contains narrative /
    findings / loop_result / intent.
    """

    cassette = _record_cassette(
        tmp_path,
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("综述完成"),
        ],
    )
    _patch_backend(monkeypatch, PlaybackBackend(cassette_path=cassette))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查这台机器的健康状况", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    payload = json.loads(stdout)
    assert payload["intent"] == "检查这台机器的健康状况"
    assert payload["narrative"] == "综述完成"
    assert "loop_result" in payload
    assert payload["loop_result"]["terminal_status"] == "ok"
    assert isinstance(payload["findings"], list)
    assert payload["findings"]  # hello.echo produced one finding


# --------------------------------------------------------------------------- #
# 5.3⑦ — degradation: exit 2 + partial output retained
# --------------------------------------------------------------------------- #


def test_intent_degraded_max_tokens_exit_2_partial_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """``max_tokens`` degradation -> exit 2, partial narrative still on stdout.

    Spec §场景:降级退出 2 且仍输出部分结果 — stop_reason=max_tokens with a text
    block makes the loop finalize degraded_token_budget while keeping the
    partial text as the narrative. The CLI emits a degraded note to stderr and
    still writes the partial result to stdout; it must NOT retry.
    """

    backend = FakeBackend(
        responses=[
            _msg(content=[TextBlock(type="text", text="部分输出")], stop_reason="max_tokens")
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # Partial narrative still emitted to stdout (CLI did not blank it).
    assert "部分输出" in stdout
    # Degraded note on stderr names the terminal_status.
    assert "degraded run" in stderr
    assert "degraded_token_budget" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# 5.4 — degradation (BackendUnavailable) vs fixture-failure (CassetteMiss)
# --------------------------------------------------------------------------- #


class _PersistentUnavailableBackend:
    """Structural ``LLMBackend`` that raises ``BackendUnavailable`` on every call.

    Drives the loop's retry budget to exhaustion → finalize
    ``failed_api_unavailable`` (no tool result was ever produced). The CLI must
    map that terminal_status to exit 2 (degraded/failed) and NOT retry on top of
    the loop (ADR-005).
    """

    name = "persistent-unavailable"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS
        self.calls = 0

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
        from hostlens.core.exceptions import BackendUnavailable

        self.calls += 1
        raise BackendUnavailable("down", backend_name="persistent-unavailable")


def test_intent_persistent_unavailable_degrades_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """Persistent BackendUnavailable -> loop finalizes failed_api_unavailable.

    Spec §场景:降级退出 2 且仍输出部分结果 (the degradation/failure branch). This
    is the **degraded** path (terminal_status), NOT the internal-wrap path: the
    loop owns the retry and finalizes a LoopResult, so the CLI sees a
    PlannerResult and maps it to exit 2 with a degraded note (not an
    ``internal:`` line).
    """

    backend = _PersistentUnavailableBackend()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # Degraded terminal_status note, not an internal-wrap line.
    assert "degraded run" in stderr
    assert "failed_api_unavailable" in stderr
    assert "internal:" not in stderr
    assert "Traceback" not in stderr
    # The loop owns retry (initial + 3 = 4); the CLI must not multiply it.
    assert backend.calls == 4
    # Narrative is empty (no model output captured) but stdout still rendered.
    assert "status=failed_api_unavailable" in stdout


def test_intent_cassette_miss_wrapped_internal_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
    tmp_path: Path,
) -> None:
    """``CassetteMiss`` (non-retriable) -> loop re-raises -> CLI wraps internal.

    Spec proposal FM5 + §需求 CLI 边界: a ``CassetteMiss`` is NOT a degradation
    (the loop does not finalize it) — it propagates out of ``run`` and the CLI's
    ``except Exception`` wraps it into one ``internal: CassetteMiss: ...`` line →
    exit 2. This asserts the wrap (NOT a terminal_status degradation) and that no
    traceback leaks.
    """

    # An empty cassette always misses on the first request.
    empty_cassette = tmp_path / "empty.jsonl"
    empty_cassette.write_text("")
    _patch_backend(monkeypatch, PlaybackBackend(cassette_path=empty_cassette))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert "internal: CassetteMiss:" in stderr
    # NOT a degradation note — the loop never finalized this error.
    assert "degraded run" not in stderr
    assert "Traceback" not in stderr
    # No PlannerResult was produced, so nothing rendered to stdout.
    assert stdout == ""


# --------------------------------------------------------------------------- #
# 5.5 — secret redaction: a sensitive string in a tool failure envelope must
# not surface un-redacted on either stream.
# --------------------------------------------------------------------------- #


def test_intent_secret_in_tool_failure_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """A secret-bearing path in a tool failure is scrubbed before either stream.

    Spec CLAUDE.md §4.4 / §7: the dispatch boundary scrubs exception messages
    (``scrub_exception_message``) before they reach the error envelope. The CLI
    renders findings (stdout) + progress (stderr) from the already-scrubbed
    invocation and must NOT re-derive / un-scrub. We register a
    ``run_inspector``-named tool whose handler raises with a sensitive
    ``/Users/<name>`` path; ``scrub_exception_message`` redacts the home-dir
    path before it reaches the error envelope, so the original username must not
    appear on either stream.

    ``ToolSpec`` is frozen, so we cannot mutate the real ``run_inspector``'s
    handler. Instead we patch the CLI's ``register_default_tools`` to register a
    leaky stub spec (same name, so the loop dispatches it). This still exercises
    the real dispatch scrub boundary + CLI rendering — only the handler body
    differs.
    """

    import hostlens.cli._intent as intent_mod
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

    secret_user = "topsecretuser"
    secret_path = f"/Users/{secret_user}/.ssh/id_rsa_supersecret"

    async def _leaky_handler(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
        raise RuntimeError(f"failed reading {secret_path}")

    def _register_leaky(registry: ToolRegistry) -> None:
        from hostlens.tools.base import ToolSpec

        registry.register(
            ToolSpec(
                name="run_inspector",
                version="1.0.0",
                input_schema=RunInspectorInput,
                output_schema=RunInspectorOutput,
                handler=cast(Any, _leaky_handler),
                agent_description="stub leaky run inspector",
                mcp_description="stub",
                cli_help=None,
                surfaces=cast(Any, {"agent"}),
                side_effects=cast(Any, "read"),
                requires_approval=False,
                sensitive_output=True,
                timeout=30.0,
            )
        )

    monkeypatch.setattr(intent_mod, "register_default_tools", _register_leaky)

    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("巡检遇到工具错误。"),
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    # Run completes (end_turn) — exit 0 (no critical finding, terminal ok).
    assert exit_code == 0, stderr
    # The sensitive username / full path must not appear on EITHER stream.
    assert secret_user not in stdout
    assert secret_user not in stderr
    assert secret_path not in stdout
    assert secret_path not in stderr
