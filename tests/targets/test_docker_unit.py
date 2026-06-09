"""Unit tests for ``DockerTarget`` — entry guards + client reuse (mock allowed).

Spec: ``openspec/changes/add-docker-target/specs/docker-execution-target/spec.md``
§需求:`DockerTarget` ... (scenarios: 复用单个 client / disabled docker target
exec 不触发 daemon / standalone 构造 raise docker_no_entry).

Unlike ``test_docker_integration.py`` (which the spec forbids from mocking
docker-py), this module is **explicitly permitted** to mock-wrap
``docker.from_env`` because the assertions here are *call counts* — the
client-reuse invariant and the "disabled gate must not dial the daemon"
invariant can only be proven by counting constructions, which a real
container cannot witness (docker-py holds no persistent TCP socket to the
daemon — see spec §需求:DockerTarget 集成测试 排除项 ①).
"""

from __future__ import annotations

from typing import Any

import pytest

import hostlens.targets.docker as docker_mod
from hostlens.core.exceptions import TargetError
from hostlens.targets.docker import DockerTarget


class _FakeEntry:
    """Structural stand-in for ``DockerEntry`` (the shape ``register`` injects)."""

    def __init__(
        self,
        *,
        container: str = "my-app",
        docker_host: str | None = None,
        enabled: bool = True,
        name: str = "docker-unit",
    ) -> None:
        self.name = name
        self.container = container
        self.docker_host = docker_host
        self.enabled = enabled


class _FakeContainer:
    """Minimal container exposing only ``status`` + ``exec_run``."""

    def __init__(self) -> None:
        self.status = "running"

    def exec_run(self, cmd: Any, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        # ``demux=True`` returns ``(stdout_bytes, stderr_bytes)``.
        return 0, (b"ok\n", b"")


class _FakeContainers:
    def get(self, ref: str) -> _FakeContainer:
        return _FakeContainer()


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def _build_target(*, entry: _FakeEntry | None) -> DockerTarget:
    target = DockerTarget("docker-unit")
    if entry is not None:
        target._entry = entry  # type: ignore[assignment]
    return target


# ---------------------------------------------------------------------------
# client reuse: 3 execs → from_env called exactly once
# ---------------------------------------------------------------------------


async def test_client_built_once_across_three_execs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:DockerTarget 复用单个 client (单元测试, 允许 mock 计数).

    Three consecutive ``exec`` calls on the same instance must construct
    the docker client exactly once (``docker.from_env`` called once); all
    three return a normal ``ExecResult``.
    """

    counter = [0]

    def fake_from_env() -> _FakeClient:
        counter[0] += 1
        return _FakeClient()

    monkeypatch.setattr(docker_mod.docker, "from_env", fake_from_env)

    target = _build_target(entry=_FakeEntry())
    for _ in range(3):
        result = await target.exec("echo hi", timeout=5)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "ok" in result.stdout

    assert counter[0] == 1


# ---------------------------------------------------------------------------
# disabled gate: target_disabled, daemon NOT dialled (from_env called 0 times)
# ---------------------------------------------------------------------------


async def test_disabled_target_raises_without_dialling_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:disabled docker target exec 不触发 daemon.

    ``_entry.enabled is False`` must raise ``target_disabled`` *before*
    any docker call — ``docker.from_env`` must be called 0 times.
    """

    counter = [0]

    def fake_from_env() -> _FakeClient:
        counter[0] += 1
        return _FakeClient()

    monkeypatch.setattr(docker_mod.docker, "from_env", fake_from_env)

    target = _build_target(entry=_FakeEntry(enabled=False))
    with pytest.raises(TargetError) as exc_info:
        await target.exec("echo hi", timeout=5)

    assert exc_info.value.kind == "target_disabled"
    assert exc_info.value.target == "docker-unit"
    assert counter[0] == 0


async def test_disabled_target_read_file_raises_without_dialling_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_file`` must apply the same disabled gate before any docker call."""

    counter = [0]

    def fake_from_env() -> _FakeClient:
        counter[0] += 1
        return _FakeClient()

    monkeypatch.setattr(docker_mod.docker, "from_env", fake_from_env)

    target = _build_target(entry=_FakeEntry(enabled=False))
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("/tmp/x")

    assert exc_info.value.kind == "target_disabled"
    assert counter[0] == 0


# ---------------------------------------------------------------------------
# standalone construction (no _entry) → docker_no_entry (not bare TypeError)
# ---------------------------------------------------------------------------


async def test_standalone_exec_raises_docker_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:standalone 构造 (无 _entry) exec raise docker_no_entry.

    A ``DockerTarget`` never run through ``TargetRegistry.register``
    leaves ``_entry`` as ``None``; ``exec`` must raise ``docker_no_entry``
    rather than crash with a bare ``TypeError`` / ``AttributeError`` and
    must NOT dial the daemon.
    """

    counter = [0]

    def fake_from_env() -> _FakeClient:
        counter[0] += 1
        return _FakeClient()

    monkeypatch.setattr(docker_mod.docker, "from_env", fake_from_env)

    target = _build_target(entry=None)
    with pytest.raises(TargetError) as exc_info:
        await target.exec("echo hi", timeout=5)

    assert exc_info.value.kind == "docker_no_entry"
    assert exc_info.value.target == "docker-unit"
    assert counter[0] == 0


async def test_standalone_read_file_raises_docker_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_file`` mirrors the ``_entry is None`` guard ordering."""

    counter = [0]

    def fake_from_env() -> _FakeClient:
        counter[0] += 1
        return _FakeClient()

    monkeypatch.setattr(docker_mod.docker, "from_env", fake_from_env)

    target = _build_target(entry=None)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("/tmp/x")

    assert exc_info.value.kind == "docker_no_entry"
    assert counter[0] == 0
