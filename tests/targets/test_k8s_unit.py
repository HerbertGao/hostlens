"""Unit tests for ``KubernetesTarget`` — construction / clients / gates / resolve.

Spec: ``openspec/changes/add-kubernetes-target/specs/kubernetes-execution-target/spec.md``.

These tests mock the kubernetes-asyncio SDK (no cluster required). Unlike
``test_k8s_integration.py`` (which the spec forbids from mocking the SDK),
this module is explicitly permitted to mock the client factories +
auth-load because the assertions here are *call counts* and structural
behaviour — the two-client construction invariant and the "disabled gate
must not dial the API server" invariant can only be proven by counting
constructions, which a real cluster cannot witness (spec §排除项 ①).

The exec / read_file websocket bodies belong to group C; group B's tests
patch the internal ``_ws_exec`` seam so the proactive pod-resolution,
client-reuse, and capability-probe logic is exercised independently.
"""

from __future__ import annotations

import io as _io
import tarfile as _tarfile
from typing import Any

import pytest

import hostlens.targets.kubernetes as k8s_mod
from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability
from hostlens.targets.kubernetes import KubernetesTarget, _RawExecOutcome


class _FakeEntry:
    """Structural stand-in for ``K8sEntry`` (the shape ``register`` injects)."""

    def __init__(
        self,
        *,
        pod: str = "my-pod",
        namespace: str = "default",
        container: str | None = None,
        kubeconfig: str | None = None,
        context: str | None = None,
        enabled: bool = True,
        name: str = "k8s-unit",
    ) -> None:
        self.name = name
        self.pod = pod
        self.namespace = namespace
        self.container = container
        self.kubeconfig = kubeconfig
        self.context = context
        self.enabled = enabled


# --- fake pod object tree ---------------------------------------------------


class _FakeContainerSpec:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeState:
    def __init__(self, *, running: object) -> None:
        self.running = running


class _FakeContainerStatus:
    def __init__(self, name: str, *, running: object = object()) -> None:
        self.name = name
        self.state = _FakeState(running=running)


class _FakePodSpec:
    def __init__(self, container_names: list[str]) -> None:
        self.containers = [_FakeContainerSpec(n) for n in container_names]


class _FakePodStatus:
    def __init__(
        self,
        *,
        phase: str = "Running",
        container_statuses: list[_FakeContainerStatus] | None = None,
    ) -> None:
        self.phase = phase
        self.container_statuses = container_statuses


class _FakePod:
    def __init__(self, *, spec: _FakePodSpec, status: _FakePodStatus) -> None:
        self.spec = spec
        self.status = status


def _running_pod(container_names: list[str]) -> _FakePod:
    return _FakePod(
        spec=_FakePodSpec(container_names),
        status=_FakePodStatus(
            phase="Running",
            container_statuses=[_FakeContainerStatus(n) for n in container_names],
        ),
    )


# --- fake clients -----------------------------------------------------------


class _FakeReadApi:
    def __init__(self, pod: _FakePod) -> None:
        self._pod = pod
        self.calls = 0

    async def read_namespaced_pod(self, name: str, namespace: str) -> _FakePod:
        self.calls += 1
        return self._pod


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counters: dict[str, int],
) -> None:
    """Patch the module-level SDK handles so ``_build_clients`` constructs fakes.

    Counts each factory invocation in ``counters`` so tests can assert the
    two clients (and the auth load) are each built exactly once.
    """

    class _FakeConfiguration:
        pass

    class _FakeApiClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            counters["api_client"] = counters.get("api_client", 0) + 1

        async def close(self) -> None:
            counters["close"] = counters.get("close", 0) + 1

    class _FakeWsApiClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            counters["ws_client"] = counters.get("ws_client", 0) + 1

        async def close(self) -> None:
            counters["close"] = counters.get("close", 0) + 1

    class _FakeCoreV1Api:
        def __init__(self, *, api_client: Any) -> None:
            # Tag which client backs this api so exec/read-pod routing is
            # assertable: WsApiClient backs exec, plain ApiClient backs read.
            self.api_client = api_client
            self.is_ws = isinstance(api_client, _FakeWsApiClient)

    class _FakeClientModule:
        Configuration = _FakeConfiguration
        ApiClient = _FakeApiClient
        CoreV1Api = _FakeCoreV1Api

    class _FakeConfigModule:
        @staticmethod
        async def load_kube_config(**kwargs: Any) -> None:
            counters["load"] = counters.get("load", 0) + 1

        @staticmethod
        def load_incluster_config(**kwargs: Any) -> None:  # pragma: no cover
            counters["load"] = counters.get("load", 0) + 1

    monkeypatch.setattr(k8s_mod, "k8s_client", _FakeClientModule)
    monkeypatch.setattr(k8s_mod, "k8s_config", _FakeConfigModule)
    monkeypatch.setattr(k8s_mod, "WsApiClient", _FakeWsApiClient)
    # in-cluster branch off
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)


def _build_target(*, entry: _FakeEntry | None) -> KubernetesTarget:
    target = KubernetesTarget("k8s-unit")
    if entry is not None:
        target._entry = entry  # type: ignore[assignment]
    return target


# ---------------------------------------------------------------------------
# type / name
# ---------------------------------------------------------------------------


def test_type_is_k8s() -> None:
    """Spec §场景:KubernetesTarget type 为 k8s."""

    target = KubernetesTarget(name="x")
    assert target.type == "k8s"


@pytest.mark.parametrize("bad", ["Prod-Pod", "1pod", "_under", "has space"])
def test_invalid_name_raises(bad: str) -> None:
    """Spec §场景:非法 name 构造 raise invalid_target_name."""

    with pytest.raises(TargetError) as exc:
        KubernetesTarget(name=bad)
    assert exc.value.kind == "invalid_target_name"


def test_initial_capabilities() -> None:
    """Spec §场景:capabilities 首次 exec 后才探测 (initial set)."""

    target = KubernetesTarget(name="x")
    assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}


# ---------------------------------------------------------------------------
# entry guards: standalone (no _entry) + disabled
# ---------------------------------------------------------------------------


async def test_standalone_exec_raises_k8s_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:standalone 构造 (无 _entry) exec raise k8s_no_entry.

    Must raise before constructing any client / dialling the API server.
    """

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    target = _build_target(entry=None)
    with pytest.raises(TargetError) as exc:
        await target.exec("echo hi", timeout=5)
    assert exc.value.kind == "k8s_no_entry"
    assert counters.get("load", 0) == 0
    assert counters.get("api_client", 0) == 0
    assert counters.get("ws_client", 0) == 0


async def test_standalone_read_file_raises_k8s_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_file entry guard mirrors exec (k8s_no_entry before any API call)."""

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    target = _build_target(entry=None)
    with pytest.raises(TargetError) as exc:
        await target.read_file("/etc/hostname")
    assert exc.value.kind == "k8s_no_entry"
    assert counters.get("api_client", 0) == 0
    assert counters.get("ws_client", 0) == 0


async def test_disabled_target_exec_does_not_dial_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:disabled k8s target exec 不触发 API server.

    ``_entry.enabled is False`` must raise ``target_disabled`` before any
    client construction or API-server dial.
    """

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    target = _build_target(entry=_FakeEntry(enabled=False))
    with pytest.raises(TargetError) as exc:
        await target.exec("echo hi", timeout=5)
    assert exc.value.kind == "target_disabled"
    assert counters.get("load", 0) == 0
    assert counters.get("api_client", 0) == 0
    assert counters.get("ws_client", 0) == 0


# ---------------------------------------------------------------------------
# two-client construction + reuse + routing
# ---------------------------------------------------------------------------


async def test_two_clients_each_built_once_and_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:KubernetesTarget 复用两个 client.

    Across 3 ``exec`` calls: auth load + each client factory called exactly
    once (reused thereafter); read pod goes through the plain ApiClient and
    exec goes through the WsApiClient.
    """

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    pod = _running_pod(["app"])
    read_apis: list[_FakeReadApi] = []

    # Wrap the resolved read api with a counting fake so we can assert the
    # pod read goes through the plain (non-ws) CoreV1Api.
    async def fake_ws_exec(
        self: KubernetesTarget,
        cmd: str,
        *,
        env: dict[str, str] | None,
        container: str,
        timeout: int,
    ) -> _RawExecOutcome:
        # Assert exec routes through the WsApiClient-backed api.
        assert self._ws_api.is_ws is True
        assert self._read_api.is_ws is False
        return _RawExecOutcome(exit_code=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(KubernetesTarget, "_ws_exec", fake_ws_exec)

    target = _build_target(entry=_FakeEntry(container="app"))

    # Replace the read api after clients are built so read_namespaced_pod
    # returns our running pod; do it by patching CoreV1Api routing via a
    # post-build hook on _ensure_clients result.
    orig_build = KubernetesTarget._build_clients

    async def patched_build(self: KubernetesTarget) -> None:
        await orig_build(self)
        # Swap the plain read api for a counting fake that returns the pod.
        fake = _FakeReadApi(pod)
        fake.is_ws = False  # type: ignore[attr-defined]
        read_apis.append(fake)
        self._read_api = fake

    monkeypatch.setattr(KubernetesTarget, "_build_clients", patched_build)

    for _ in range(3):
        result = await target.exec("echo hi", timeout=5)
        assert result.exit_code == 0
        assert result.timed_out is False

    assert counters.get("load", 0) == 1
    assert counters.get("api_client", 0) == 1
    assert counters.get("ws_client", 0) == 1
    # read_namespaced_pod is called once per exec (proactive resolve), all
    # on the single reused read api.
    assert len(read_apis) == 1
    assert read_apis[0].calls == 3


# ---------------------------------------------------------------------------
# capability lazy probe
# ---------------------------------------------------------------------------


async def test_capabilities_probed_after_first_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:KubernetesTarget capabilities 首次 exec 后才探测.

    Before exec: only {SHELL, FILE_READ}. After a successful exec where the
    probe finds systemctl (exit 0) but not docker (exit 1): SYSTEMD added,
    DOCKER_CLI not. Probe runs exactly once (not re-probed on 2nd exec).
    """

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    pod = _running_pod(["app"])

    probe_calls: list[str] = []

    async def fake_ws_exec(
        self: KubernetesTarget,
        cmd: str,
        *,
        env: dict[str, str] | None,
        container: str,
        timeout: int,
    ) -> _RawExecOutcome:
        if cmd.startswith("command -v"):
            probe_calls.append(cmd)
            ok = "systemctl" in cmd
            return _RawExecOutcome(exit_code=0 if ok else 1, stdout="", stderr="", timed_out=False)
        return _RawExecOutcome(exit_code=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(KubernetesTarget, "_ws_exec", fake_ws_exec)

    orig_build = KubernetesTarget._build_clients

    async def patched_build(self: KubernetesTarget) -> None:
        await orig_build(self)
        self._read_api = _FakeReadApi(pod)

    monkeypatch.setattr(KubernetesTarget, "_build_clients", patched_build)

    target = _build_target(entry=_FakeEntry(container="app"))
    assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}

    await target.exec("echo hi", timeout=5)
    assert target.capabilities == {
        Capability.SHELL,
        Capability.FILE_READ,
        Capability.SYSTEMD,
    }
    assert probe_calls == ["command -v systemctl", "command -v docker"]

    # Second exec: probe is not repeated.
    await target.exec("echo hi", timeout=5)
    assert probe_calls == ["command -v systemctl", "command -v docker"]


# ---------------------------------------------------------------------------
# proactive pod / container resolution
# ---------------------------------------------------------------------------


def _build_target_with_pod(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry: _FakeEntry,
    pod: _FakePod,
    read_exc: Exception | None = None,
) -> KubernetesTarget:
    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    async def fake_ws_exec(
        self: KubernetesTarget,
        cmd: str,
        *,
        env: dict[str, str] | None,
        container: str,
        timeout: int,
    ) -> _RawExecOutcome:
        return _RawExecOutcome(exit_code=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(KubernetesTarget, "_ws_exec", fake_ws_exec)

    orig_build = KubernetesTarget._build_clients

    class _ExcReadApi:
        is_ws = False

        async def read_namespaced_pod(self, name: str, namespace: str) -> Any:
            assert read_exc is not None
            raise read_exc

    async def patched_build(self: KubernetesTarget) -> None:
        await orig_build(self)
        if read_exc is not None:
            self._read_api = _ExcReadApi()
        else:
            self._read_api = _FakeReadApi(pod)

    monkeypatch.setattr(KubernetesTarget, "_build_clients", patched_build)
    return _build_target(entry=entry)


async def test_pod_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """read pod 404 → pod_not_found."""

    exc = k8s_mod.ApiException(status=404)
    target = _build_target_with_pod(
        monkeypatch, entry=_FakeEntry(), pod=_running_pod(["app"]), read_exc=exc
    )
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "pod_not_found"


async def test_api_error_non_404_k8s_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """read pod 403 → k8s_unavailable (not pod_not_found)."""

    exc = k8s_mod.ApiException(status=403)
    target = _build_target_with_pod(
        monkeypatch, entry=_FakeEntry(), pod=_running_pod(["app"]), read_exc=exc
    )
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "k8s_unavailable"


async def test_pod_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """phase != Running → pod_not_running (carries phase)."""

    pod = _FakePod(
        spec=_FakePodSpec(["app"]),
        status=_FakePodStatus(phase="Pending", container_statuses=None),
    )
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(), pod=pod)
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "pod_not_running"
    assert e.value.extra.get("phase") == "Pending"


async def test_named_container_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """specified container not in spec → container_not_found (with available)."""

    pod = _running_pod(["app", "sidecar"])
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(container="nope"), pod=pod)
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "container_not_found"
    assert "app" in str(e.value.extra.get("available", ""))


async def test_default_container_resolves_to_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """container=None resolves to spec.containers[0].name and runs."""

    pod = _running_pod(["primary", "sidecar"])
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(container=None), pod=pod)
    result = await target.exec("echo hi", timeout=5)
    assert result.exit_code == 0


async def test_container_statuses_none_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status.container_statuses is None (kubelet race) → container_not_running."""

    pod = _FakePod(
        spec=_FakePodSpec(["app"]),
        status=_FakePodStatus(phase="Running", container_statuses=None),
    )
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(container="app"), pod=pod)
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "container_not_running"


async def test_container_state_running_none_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matching container status with state.running None → container_not_running."""

    pod = _FakePod(
        spec=_FakePodSpec(["app"]),
        status=_FakePodStatus(
            phase="Running",
            container_statuses=[_FakeContainerStatus("app", running=None)],
        ),
    )
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(container="app"), pod=pod)
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "container_not_running"


# ---------------------------------------------------------------------------
# env-key validation (injection guard) — happens before any API call
# ---------------------------------------------------------------------------


async def test_invalid_env_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §exec env key 校验: bad key → invalid_env_key before API call."""

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)
    target = _build_target(entry=_FakeEntry(container="app"))
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5, env={"; rm -rf /": "x"})
    assert e.value.kind == "invalid_env_key"
    assert counters.get("api_client", 0) == 0


# ---------------------------------------------------------------------------
# SDK not installed
# ---------------------------------------------------------------------------


async def test_sdk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK not installed → k8s_sdk_unavailable with pip install hint."""

    monkeypatch.setattr(k8s_mod, "k8s_client", None)
    monkeypatch.setattr(k8s_mod, "k8s_config", None)
    monkeypatch.setattr(k8s_mod, "WsApiClient", None)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    target = _build_target(entry=_FakeEntry(container="app"))
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "k8s_sdk_unavailable"
    assert "hostlens[k8s]" in str(e.value.extra.get("hint", ""))


# ---------------------------------------------------------------------------
# _ws_exec: real websocket framing (group C)
# ---------------------------------------------------------------------------
#
# These tests drive the *real* ``_ws_exec`` body — they mock only the ws
# boundary (a fake ``_ws_api.connect_get_namespaced_pod_exec`` returning a
# fake async-context-manager whose ``__aenter__`` yields a fake ws). The
# fake ws records outbound ``send_bytes`` frames and replays preset inbound
# channel frames, so exit-code parsing, env-over-stdin framing, empty-frame
# skipping, and timeout are exercised without a cluster.


def _frame(channel: int, payload: bytes) -> bytes:
    """Build a k8s exec channel frame: ``channel byte + payload``."""

    return bytes([channel]) + payload


def _success_status() -> bytes:
    return b'{"metadata":{},"status":"Success"}'


def _exit_status(code: int) -> bytes:
    """A non-Success ``v1.Status`` carrying ``code`` as the SDK reads it."""

    return (
        b'{"metadata":{},"status":"Failure","reason":"NonZeroExitCode",'
        b'"details":{"causes":[{"reason":"ExitCode","message":"' + str(code).encode() + b'"}]}}'
    )


class _FakeWsMessage:
    def __init__(self, data: object) -> None:
        self.data = data


class _FakeWs:
    """Fake ``aiohttp.ClientWebSocketResponse`` for one exec round-trip.

    ``send_bytes`` records outbound frames; iterating the ws replays the
    preset inbound frames (already channel-prefixed). ``close`` records the
    best-effort teardown.
    """

    def __init__(self, inbound: list[bytes]) -> None:
        self._inbound = inbound
        self.sent: list[bytes] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for frame in self._inbound:
                yield _FakeWsMessage(frame)

        return gen()


class _FakeExecCtx:
    """Async-context-manager returned by ``connect_get_namespaced_pod_exec``.

    Mirrors the SDK's ``_WSRequestContextManager``: the connect call is
    awaitable (returns this), and ``async with`` it yields the ws.
    """

    def __init__(self, ws: _FakeWs) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWs:
        return self._ws

    async def __aexit__(self, *args: Any) -> None:
        await self._ws.close()


class _FakeExecApi:
    """Fake ``_ws_api`` (WsApiClient-backed CoreV1Api) for exec framing tests."""

    is_ws = True

    def __init__(
        self,
        *,
        inbound: list[bytes] | None = None,
        connect_exc: Exception | None = None,
        ws_iter_exc: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self._inbound = inbound or []
        self._connect_exc = connect_exc
        self._ws_iter_exc = ws_iter_exc
        self._hang = hang
        self.calls: list[dict[str, Any]] = []
        self.ws_list: list[_FakeWs] = []
        self.last_ws: _FakeWs | None = None

    async def connect_get_namespaced_pod_exec(
        self, name: str, namespace: str, **kwargs: Any
    ) -> _FakeExecCtx:
        self.calls.append({"name": name, "namespace": namespace, **kwargs})
        if self._connect_exc is not None:
            raise self._connect_exc
        ws: _FakeWs
        if self._hang:
            ws = _HangingWs()
        elif self._ws_iter_exc is not None:
            ws = _RaisingWs(self._ws_iter_exc)
        else:
            ws = _FakeWs(self._inbound)
        self.last_ws = ws
        self.ws_list.append(ws)
        return _FakeExecCtx(ws)


class _RaisingWs(_FakeWs):
    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self._exc = exc

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            raise self._exc
            yield  # pragma: no cover - makes this an async generator

        return gen()


class _HangingWs(_FakeWs):
    def __init__(self) -> None:
        super().__init__([])

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            import asyncio as _asyncio

            await _asyncio.sleep(3600)
            yield  # pragma: no cover

        return gen()


def _build_exec_target(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry: _FakeEntry,
    pod: _FakePod,
    exec_api: _FakeExecApi,
) -> KubernetesTarget:
    """Wire a target whose ``_ws_api`` is ``exec_api`` and ``_read_api`` returns ``pod``."""

    counters: dict[str, int] = {}
    _install_fake_sdk(monkeypatch, counters=counters)

    orig_build = KubernetesTarget._build_clients

    async def patched_build(self: KubernetesTarget) -> None:
        await orig_build(self)
        self._read_api = _FakeReadApi(pod)
        self._ws_api = exec_api

    monkeypatch.setattr(KubernetesTarget, "_build_clients", patched_build)
    return _build_target(entry=entry)


async def test_exec_env_via_stdin_not_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec 经 stdin 注入 env 且不在 argv 泄露.

    command 必须严格 ``["/bin/sh"]``(不含 env/cmd/secret), env 经 stdin
    以 ``export MY_VAR='x'`` 喂入, stdin 脚本含 ``exit $?``。
    """

    exec_api = _FakeExecApi(inbound=[_frame(1, b"x\n"), _frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    result = await target.exec("echo $MY_VAR", timeout=5, env={"MY_VAR": "x"})
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "x" in result.stdout

    call = exec_api.calls[0]
    assert call["command"] == ["/bin/sh"]
    assert "export" not in "".join(call["command"])
    # stdin frame: channel 0 + script bytes; secret only on stdin.
    # ws_list[0] is the real exec (subsequent ws are the capability probe).
    ws = exec_api.ws_list[0]
    assert ws.sent, "stdin frame must be sent"
    stdin_frame = ws.sent[0]
    assert stdin_frame[0] == 0  # STDIN channel
    script = stdin_frame[1:].decode()
    assert "export MY_VAR='x'" in script
    assert "echo $MY_VAR" in script
    assert script.rstrip().endswith("exit $?")


async def test_exec_secret_not_in_command_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec secret 不出现在 command argv.

    command 严格 ``["/bin/sh"]``; secret 与 cmd 都仅经 stdin。
    """

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    await target.exec("ps auxw", timeout=5, env={"SECRET_TOKEN": "abc"})

    command = exec_api.calls[0]["command"]
    assert command == ["/bin/sh"]
    joined = "".join(command)
    assert "SECRET_TOKEN" not in joined
    assert "abc" not in joined
    assert "ps auxw" not in joined


async def test_exec_invalid_env_key_before_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec 非法 env key raise invalid_env_key(不连 ws)."""

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5, env={"; rm -rf /": "x"})
    assert e.value.kind == "invalid_env_key"
    assert exec_api.calls == []


async def test_exec_skips_empty_and_one_byte_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §exec demux: 空帧(``b""``)/1-byte 帧不崩 IndexError, 被跳过/吸收."""

    exec_api = _FakeExecApi(
        inbound=[
            b"",  # zero-length frame — must not raise IndexError
            _FakeWsMessage("ping").data if False else b"",  # keepalive-ish empty
            _frame(1, b""),  # 1-byte frame: channel only, empty payload
            _frame(1, b"hello"),
            _frame(3, _success_status()),
        ]
    )
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    result = await target.exec("echo hello", timeout=5)
    assert result.exit_code == 0
    assert result.stdout == "hello"


async def test_exec_non_zero_exit_via_channel3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec 非零退出返回 ExecResult 不 raise(exit_code=3 经 channel-3)."""

    exec_api = _FakeExecApi(inbound=[_frame(2, b"boom\n"), _frame(3, _exit_status(3))])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    result = await target.exec("exit 3", timeout=5)
    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.stderr == "boom\n"


async def test_exec_no_channel3_exit_code_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No channel-3 frame (ws dropped) → exit_code=None, not timed_out (contract-legal)."""

    exec_api = _FakeExecApi(inbound=[_frame(1, b"partial")])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    result = await target.exec("echo partial", timeout=5)
    assert result.exit_code is None
    assert result.timed_out is False
    assert result.stdout == "partial"


async def test_exec_timeout_returns_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec 超时返回 timed_out 且 exit_code 为 None."""

    exec_api = _FakeExecApi(hang=True)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    result = await target.exec("sleep 60", timeout=0.05)
    assert result.timed_out is True
    assert result.exit_code is None
    # Capability probe must be skipped on timeout.
    assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}


async def test_exec_toctou_pod_vanished_pod_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:exec 阶段 pod 消失 raise pod_not_found(TOCTOU).

    读 pod 时 Running, 但 connect exec 时 404 → pod_not_found(不崩裸 ApiException)。
    """

    exc = k8s_mod.ApiException(status=404)
    exc.body = '{"message":"pods \\"my-pod\\" not found"}'
    exec_api = _FakeExecApi(connect_exc=exc)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "pod_not_found"


async def test_exec_no_sh_distroless_exec_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:pod 内无 /bin/sh raise exec_failed(不归 k8s_unavailable)."""

    status = (
        b'{"metadata":{},"status":"Failure","reason":"InternalError",'
        b'"message":"command terminated: exec: \\"/bin/sh\\": executable file not found in $PATH",'
        b'"details":{"causes":[{"message":"executable file not found"}]}}'
    )
    exec_api = _FakeExecApi(inbound=[_frame(3, status)])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "exec_failed"


async def test_exec_connect_non_404_k8s_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """exec-phase non-404 ApiException (403/500) → k8s_unavailable (not bare ApiException)."""

    exc = k8s_mod.ApiException(status=403)
    exec_api = _FakeExecApi(connect_exc=exc)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "k8s_unavailable"


async def test_exec_ws_iter_apiexception_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport drop mid-stream (ApiException while iterating ws) is classified, not bare."""

    exc = k8s_mod.ApiException(status=500)
    exec_api = _FakeExecApi(ws_iter_exc=exc)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "k8s_unavailable"


# ---------------------------------------------------------------------------
# read_file: tar-over-exec (group C2)
# ---------------------------------------------------------------------------
#
# read_file drives the same ``connect_get_namespaced_pod_exec`` websocket as
# exec but with ``command=["tar","cf","-",<path>]`` and ``stdin=False``; the
# stdout channel carries a tar byte stream. These tests feed constructed tar
# bytes (built with stdlib ``tarfile``) as channel-1 frames plus a channel-3
# Status, exercising single-regular-file / 10 MiB / not_a_file / FileNotFound
# / no-tar handling without a cluster.


def _tar_regular(name: str, data: bytes) -> bytes:
    """Build a tar archive containing one regular file ``name`` with ``data``."""

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w") as tar:
        info = _tarfile.TarInfo(name=name)
        info.size = len(data)
        info.type = _tarfile.REGTYPE
        tar.addfile(info, _io.BytesIO(data))
    return buf.getvalue()


def _tar_directory_with_member(dir_name: str, member: str, data: bytes) -> bytes:
    """Build a tar archive whose first entry is a directory then a regular file.

    Mirrors what ``tar cf - <dir>`` emits: a DIRTYPE entry followed by the
    directory's contents — so the directory entry is hit first (not_a_file).
    """

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w") as tar:
        dinfo = _tarfile.TarInfo(name=dir_name)
        dinfo.type = _tarfile.DIRTYPE
        tar.addfile(dinfo)
        finfo = _tarfile.TarInfo(name=member)
        finfo.size = len(data)
        finfo.type = _tarfile.REGTYPE
        tar.addfile(finfo, _io.BytesIO(data))
    return buf.getvalue()


def _chunked_frames(channel: int, payload: bytes, *, chunk: int = 8192) -> list[bytes]:
    """Split ``payload`` into multiple channel frames (simulate ws fragmentation)."""

    return [_frame(channel, payload[i : i + chunk]) for i in range(0, len(payload), chunk)] or [
        _frame(channel, b"")
    ]


def _no_such_file_status() -> bytes:
    """A channel-3 Status for a tar that exited non-zero (file missing).

    The message is the generic "non-zero exit code" wording — crucially NOT
    an OCI "executable file not found" (that is the no-tar case). The
    exit-code cause carries ``1`` so read_file's exit-code-led FileNotFound
    judgement fires.
    """

    return (
        b'{"metadata":{},"status":"Failure","reason":"NonZeroExitCode",'
        b'"message":"command terminated with non-zero exit code: error executing '
        b'command [tar cf - /nope], exit status 1",'
        b'"details":{"causes":[{"reason":"ExitCode","message":"1"}]}}'
    )


def _no_tar_status() -> bytes:
    """A channel-3 Status for an OCI failure to start ``tar`` (distroless)."""

    return (
        b'{"metadata":{},"status":"Failure","reason":"InternalError",'
        b'"message":"command terminated: exec: \\"tar\\": executable file not found in $PATH",'
        b'"details":{"causes":[{"message":"executable file not found"}]}}'
    )


async def test_read_file_small(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 读小文件 — tar stream → b"hello"."""

    tar_bytes = _tar_regular("tmp/hello.txt", b"hello")
    exec_api = _FakeExecApi(inbound=[_frame(1, tar_bytes), _frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    data = await target.read_file("/tmp/hello.txt")
    assert data == b"hello"

    # command must be the tar form, NOT /bin/sh; no stdin frame sent.
    call = exec_api.calls[0]
    assert call["command"] == ["tar", "cf", "-", "/tmp/hello.txt"]
    assert call["stdin"] is False
    assert exec_api.ws_list[0].sent == []


async def test_read_file_exactly_10mb_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 恰好 10MB 放行 (boundary ``>``)."""

    payload = b"a" * (10 * 1024 * 1024)
    tar_bytes = _tar_regular("tmp/exact.bin", payload)
    inbound = [*_chunked_frames(1, tar_bytes), _frame(3, _success_status())]
    exec_api = _FakeExecApi(inbound=inbound)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    data = await target.read_file("/tmp/exact.bin")
    assert len(data) == 10 * 1024 * 1024


async def test_read_file_over_10mb_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 超过 10MB raise file_too_large."""

    payload = b"a" * (10 * 1024 * 1024 + 1)
    tar_bytes = _tar_regular("tmp/big.bin", payload)
    inbound = [*_chunked_frames(1, tar_bytes), _frame(3, _success_status())]
    exec_api = _FakeExecApi(inbound=inbound)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/big.bin")
    assert e.value.kind == "file_too_large"
    assert e.value.extra.get("path") == "/tmp/big.bin"


async def test_read_file_ws_memory_backstop_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw ``tar`` stdout exceeding ``_WS_TAR_MAX_BYTES`` raises file_too_large.

    Memory-DoS backstop: ``_ws_tar`` aborts buffering before the whole stream
    is materialised, independent of the per-member 10 MiB extractor cap.
    ``_WS_TAR_MAX_BYTES`` is patched to a small value so the test feeds a
    modest stream (not a real 80 MiB archive) that still crosses the
    high-water mark while accumulating.
    """

    monkeypatch.setattr(k8s_mod, "_WS_TAR_MAX_BYTES", 4096)
    # Raw channel-1 bytes well above the patched 4 KiB backstop; the content
    # need not be a valid tar — the backstop fires during accumulation,
    # before any tar parsing.
    raw = b"a" * (4096 * 4)
    inbound = [*_chunked_frames(1, raw), _frame(3, _success_status())]
    exec_api = _FakeExecApi(inbound=inbound)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/huge.bin")
    assert e.value.kind == "file_too_large"
    assert e.value.extra.get("path") == "/tmp/huge.bin"


async def test_read_file_directory_not_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 路径指向目录 raise not_a_file (first DIRTYPE entry)."""

    tar_bytes = _tar_directory_with_member("etc/", "etc/passwd", b"root:x:0:0")
    exec_api = _FakeExecApi(inbound=[_frame(1, tar_bytes), _frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/etc")
    assert e.value.kind == "not_a_file"


async def test_read_file_multi_entry_oversize_prefers_not_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:read_file 多条目超大归档优先报 not_a_file (非 file_too_large).

    A directory archive whose member exceeds 10 MiB: the DIRTYPE entry is hit
    first, so not_a_file wins over file_too_large.
    """

    big = b"a" * (10 * 1024 * 1024 + 1)
    tar_bytes = _tar_directory_with_member("data/", "data/big.bin", big)
    inbound = [*_chunked_frames(1, tar_bytes), _frame(3, _success_status())]
    exec_api = _FakeExecApi(inbound=inbound)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/data")
    assert e.value.kind == "not_a_file"


@pytest.mark.parametrize(
    "bad_path",
    ["tmp/x", "relative.txt", "./x"],
)
async def test_read_file_relative_path_invalid(
    monkeypatch: pytest.MonkeyPatch, bad_path: str
) -> None:
    """Spec §场景:read_file 相对路径 raise invalid_path (no exec issued)."""

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file(bad_path)
    assert e.value.kind == "invalid_path"
    assert exec_api.calls == []


async def test_read_file_nul_byte_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 路径含 NUL 字节 raise invalid_path."""

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x\x00.txt")
    assert e.value.kind == "invalid_path"
    assert exec_api.calls == []


async def test_read_file_newline_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 路径含换行 raise invalid_path."""

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x\n.txt")
    assert e.value.kind == "invalid_path"
    assert exec_api.calls == []


async def test_read_file_dotdot_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 绝对路径含 .. 经 normpath 折叠后读取.

    ``/a/../b/c.txt`` folds to ``/b/c.txt`` (posixpath.normpath, not
    PurePosixPath); the folded path is what tar receives.
    """

    tar_bytes = _tar_regular("b/c.txt", b"folded")
    exec_api = _FakeExecApi(inbound=[_frame(1, tar_bytes), _frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    data = await target.read_file("/a/../b/c.txt")
    assert data == b"folded"
    assert exec_api.calls[0]["command"] == ["tar", "cf", "-", "/b/c.txt"]


async def test_read_file_missing_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file 不存在 raise FileNotFoundError.

    tar non-zero exit + no stdout bytes → stdlib FileNotFoundError (not
    TargetError); exit-code-led, no English stderr substring used.
    """

    exec_api = _FakeExecApi(inbound=[_frame(3, _no_such_file_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(FileNotFoundError):
        await target.read_file("/nonexistent")


async def test_read_file_no_tar_exec_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:read_file pod 内无 tar raise exec_failed (with hint)."""

    exec_api = _FakeExecApi(inbound=[_frame(3, _no_tar_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x")
    assert e.value.kind == "exec_failed"
    assert "tar" in str(e.value.extra.get("hint", ""))


async def test_read_file_empty_stream_not_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A success exit with no/empty tar bytes (truncated stream) → not_a_file.

    The shared tar reader maps ``tarfile.ReadError`` (empty / malformed
    stream) to not_a_file rather than letting the raw error escape.
    """

    exec_api = _FakeExecApi(inbound=[_frame(3, _success_status())])
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )

    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/empty")
    assert e.value.kind == "not_a_file"


async def test_read_file_proactive_pod_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_file does proactive read_pod: pod 404 → pod_not_found (not exec_failed)."""

    exc = k8s_mod.ApiException(status=404)
    target = _build_target_with_pod(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), read_exc=exc
    )
    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x")
    assert e.value.kind == "pod_not_found"


async def test_read_file_proactive_container_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_file resolves container running state: not-running → container_not_running."""

    pod = _FakePod(
        spec=_FakePodSpec(["app"]),
        status=_FakePodStatus(
            phase="Running",
            container_statuses=[_FakeContainerStatus("app", running=None)],
        ),
    )
    target = _build_target_with_pod(monkeypatch, entry=_FakeEntry(container="app"), pod=pod)
    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x")
    assert e.value.kind == "container_not_running"


async def test_read_file_toctou_container_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_file exec-phase TOCTOU: connect 404 (container) → container_not_found."""

    exc = k8s_mod.ApiException(status=404)
    exc.body = '{"message":"container \\"app\\" not found"}'
    exec_api = _FakeExecApi(connect_exc=exc)
    target = _build_exec_target(
        monkeypatch, entry=_FakeEntry(container="app"), pod=_running_pod(["app"]), exec_api=exec_api
    )
    with pytest.raises(TargetError) as e:
        await target.read_file("/tmp/x")
    assert e.value.kind == "container_not_found"


# ---------------------------------------------------------------------------
# fault classification + scrub (group C2)
# ---------------------------------------------------------------------------


def test_scrub_redacts_bearer_token() -> None:
    """Spec §场景:transport 异常经 scrub 脱敏 bearer token."""

    exc = k8s_mod.ApiException(status=401)
    exc.reason = "Unauthorized: Authorization: Bearer sk-secret-abc123 rejected"
    scrubbed = k8s_mod._scrub(exc)
    assert "sk-secret-abc123" not in scrubbed


def test_scrub_redacts_home_path() -> None:
    """Spec §场景:transport 异常经 scrub 脱敏 home 路径."""

    exc = RuntimeError("kubeconfig load failed: /Users/alice/.kube/config not readable")
    scrubbed = k8s_mod._scrub(exc)
    assert "/Users/alice" not in scrubbed


async def test_k8s_unavailable_message_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-404 ApiException with an embedded credential → k8s_unavailable, scrubbed.

    The full TargetError string must not leak the token but must keep kind +
    target.
    """

    exc = k8s_mod.ApiException(status=403)
    exc.reason = "Forbidden Authorization: Bearer sk-leak-9999"
    target = _build_target_with_pod(
        monkeypatch, entry=_FakeEntry(), pod=_running_pod(["app"]), read_exc=exc
    )
    with pytest.raises(TargetError) as e:
        await target.exec("echo hi", timeout=5)
    assert e.value.kind == "k8s_unavailable"
    rendered = str(e.value)
    assert "sk-leak-9999" not in rendered
    assert "k8s_unavailable" in rendered
    assert "k8s-unit" in rendered


def test_decode_status_message_scrubbed() -> None:
    """read_file's status_message path runs through the scrubber."""

    payload = (
        b'{"status":"Failure","message":"exec: failed for /Users/bob/.kube/config",'
        b'"details":{"causes":[]}}'
    )
    msg = k8s_mod._decode_status_message(payload)
    assert "/Users/bob" not in msg


# ---------------------------------------------------------------------------
# aclose best-effort teardown
# ---------------------------------------------------------------------------


def test_aclose_suppresses_client_close_errors() -> None:
    """aclose is best-effort: a client whose async close() raises must not
    propagate, and both client handles must still be reset to None (idempotent
    teardown). Covers the contextlib.suppress(Exception) branch in aclose.
    """
    import asyncio as _asyncio

    class _RaisingClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("boom on close")

    target = KubernetesTarget(name="x")
    read_client = _RaisingClient()
    ws_client = _RaisingClient()
    target._read_client = read_client  # type: ignore[assignment]
    target._ws_client = ws_client  # type: ignore[assignment]
    target._read_api = object()  # type: ignore[assignment]
    target._ws_api = object()  # type: ignore[assignment]

    # Must not raise despite both close() raising.
    _asyncio.run(target.aclose())

    assert read_client.closed is True
    assert ws_client.closed is True
    assert target._read_client is None
    assert target._ws_client is None
    assert target._read_api is None
    assert target._ws_api is None


def test_aclose_idempotent_when_clients_none() -> None:
    """aclose on a target that never built clients is a no-op (no error)."""
    import asyncio as _asyncio

    target = KubernetesTarget(name="x")
    _asyncio.run(target.aclose())
    assert target._read_client is None
    assert target._ws_client is None
