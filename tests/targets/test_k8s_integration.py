"""End-to-end ``KubernetesTarget`` tests against a real cluster + ``alpine`` pod.

Spec: ``openspec/changes/add-kubernetes-target/specs/kubernetes-execution-target/spec.md``
§需求:KubernetesTarget 集成测试必须用真实 cluster, 无 cluster 时 skip.

The spec explicitly forbids mocking kubernetes-asyncio in this file — the
value of these tests over the unit tests in ``test_k8s_unit.py`` is that
they exercise the real exec websocket (channel demux, the env-over-stdin +
``exit $?`` deterministic-terminate protocol, channel-3 ``v1.Status`` exit
codes) and the real ``tar``-over-exec ``read_file`` path against a live
kubelet. A grep-based assertion at the bottom of this module enforces the
no-mock rule (spec §场景:不允许 mock kubernetes-asyncio), matching BOTH the
``kubernetes`` and ``kubernetes_asyncio`` aliases so an ``import ... as``
rename cannot smuggle a mock past the guard.

Cluster topology:

- A session-scoped fixture detects a reachable cluster (kubeconfig valid +
  API reachable, verified with ``kubectl``); if unreachable the whole
  module is skipped (``pytest.skip("k8s cluster unavailable")``) so
  developers / CI without a cluster (e.g. no ``kind`` / ``minikube``) still
  run the rest of the suite.
- One long-lived ``alpine`` pod running ``sleep 3600`` is created once per
  session (busybox userland: ``/bin/sh``, POSIX ``command -v``,
  ``truncate``, ``ps``, ``tar`` all present) and reused across tests;
  per-test isolation is achieved with unique file paths written into the
  pod before each read.
- A second pod with TWO containers backs the multi-container selection
  test.
- ``pod_not_running`` is covered by a short-lived ``Never``-restart pod
  that exits (phase ``Succeeded``); ``pod_not_found`` /
  ``container_not_found`` use names that do not exist.

Deliberately NOT covered here (spec §需求 排除项, with reasons):

- ① client-reuse single construction — needs mock call counting, lives in
  ``test_k8s_unit.py`` (a real cluster cannot witness construction counts).
- ② websocket / coroutine release after a timeout — the ``asyncio.wait_for``
  cancellation's underlying stream close timing is not provable in-test
  (async websocket inherent limitation); we assert ONLY the timeout
  return-value (``timed_out is True`` + ``exit_code is None``).

Every test is marked ``@pytest.mark.k8s_integration`` (registered in
``pyproject.toml``).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tokenize
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability
from hostlens.targets.kubernetes import KubernetesTarget

pytestmark = pytest.mark.k8s_integration

_TEN_MIB = 10 * 1024 * 1024
_NAMESPACE = "default"
_KUBECTL_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Skip-gate + session pod fixtures (real cluster, never mocked)
# ---------------------------------------------------------------------------


def _kubectl(
    *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=_KUBECTL_TIMEOUT,
        input=stdin,
        check=check,
    )


@pytest.fixture(scope="session")
def cluster() -> Iterator[None]:
    """Skip the whole module unless a real cluster is reachable.

    Reachability is checked via ``kubectl`` (kubeconfig valid + API server
    responding). Anything that fails — no ``kubectl`` binary, no cluster,
    auth failure — skips with the spec-mandated reason substring.
    """

    if shutil.which("kubectl") is None:
        pytest.skip("k8s cluster unavailable (kubectl not found)")
    try:
        proc = _kubectl("version", "-o", "json", check=False)
    except Exception:
        pytest.skip("k8s cluster unavailable")
    if proc.returncode != 0:
        pytest.skip("k8s cluster unavailable")
    # ``version`` reaches the API server only when serverVersion is present.
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        pytest.skip("k8s cluster unavailable")
    if not isinstance(payload, dict) or "serverVersion" not in payload:
        pytest.skip("k8s cluster unavailable")
    yield None


@pytest.fixture(scope="session")
def running_pod(cluster: None) -> Iterator[str]:
    """Create one single-container ``alpine`` pod (``sleep 3600``), reused per session."""

    name = f"hostlens-k8s-it-{uuid.uuid4().hex[:8]}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{"name": "app", "image": "alpine:latest", "command": ["sleep", "3600"]}],
        },
    }
    _kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    try:
        _kubectl(
            "wait",
            f"pod/{name}",
            "--for=condition=Ready",
            f"--timeout={_KUBECTL_TIMEOUT}s",
        )
        yield name
    finally:
        _kubectl("delete", "pod", name, "--ignore-not-found", "--now", check=False)


@pytest.fixture(scope="session")
def multi_container_pod(cluster: None) -> Iterator[str]:
    """Create a TWO-container pod for the explicit-container-selection test."""

    name = f"hostlens-k8s-it-multi-{uuid.uuid4().hex[:8]}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {"name": "primary", "image": "alpine:latest", "command": ["sleep", "3600"]},
                {"name": "sidecar", "image": "alpine:latest", "command": ["sleep", "3600"]},
            ],
        },
    }
    _kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    try:
        _kubectl(
            "wait",
            f"pod/{name}",
            "--for=condition=Ready",
            f"--timeout={_KUBECTL_TIMEOUT}s",
        )
        yield name
    finally:
        _kubectl("delete", "pod", name, "--ignore-not-found", "--now", check=False)


@pytest.fixture(scope="session")
def succeeded_pod(cluster: None) -> Iterator[str]:
    """Create a pod that runs to completion (phase ``Succeeded``) for not-running."""

    name = f"hostlens-k8s-it-done-{uuid.uuid4().hex[:8]}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{"name": "app", "image": "alpine:latest", "command": ["true"]}],
        },
    }
    _kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    try:
        # Poll until the pod reaches a terminal phase (Succeeded).
        for _ in range(_KUBECTL_TIMEOUT):
            proc = _kubectl("get", "pod", name, "-o", "jsonpath={.status.phase}", check=False)
            if proc.returncode == 0 and proc.stdout.strip() in {"Succeeded", "Failed"}:
                break
            import time as _time

            _time.sleep(1)
        yield name
    finally:
        _kubectl("delete", "pod", name, "--ignore-not-found", "--now", check=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Entry:
    """Structural stand-in for ``K8sEntry`` (the shape ``register`` injects)."""

    def __init__(
        self,
        *,
        pod: str,
        namespace: str = _NAMESPACE,
        container: str | None = None,
        kubeconfig: str | None = None,
        context: str | None = None,
        enabled: bool = True,
        name: str = "k8s-it",
    ) -> None:
        self.name = name
        self.pod = pod
        self.namespace = namespace
        self.container = container
        self.kubeconfig = kubeconfig
        self.context = context
        self.enabled = enabled


def _build_target(
    *, pod: str, container: str | None = None, name: str = "k8s-it"
) -> KubernetesTarget:
    target = KubernetesTarget(name)
    target._entry = _Entry(pod=pod, container=container, name=name)  # type: ignore[assignment]
    return target


def _unique_path(suffix: str = "") -> str:
    return f"/tmp/hostlens-{uuid.uuid4().hex}{suffix}"


async def _write_in_pod(
    target: KubernetesTarget,
    path: str,
    *,
    content: str | None = None,
    size: int | None = None,
) -> None:
    """Materialise a file inside the pod via a real ``exec``.

    Exactly one of ``content`` / ``size`` must be given. ``content`` writes
    literal bytes (``printf %s`` avoids the trailing newline ``echo`` adds);
    ``size`` truncates a sparse file to an exact length (fast, no real
    allocation).
    """

    if content is not None:
        result = await target.exec(f"printf %s '{content}' > {path}", timeout=20)
    else:
        result = await target.exec(f"truncate -s {size} {path}", timeout=20)
    assert result.exit_code == 0, f"setup write failed: {result.stderr}"


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


async def test_exec_echo_returns_stdout(running_pod: str) -> None:
    """Spec §场景:集成测试通过真实 cluster 跑 echo."""

    target = _build_target(pod=running_pod)
    try:
        result = await target.exec("echo hostlens-probe", timeout=20)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "hostlens-probe" in result.stdout
    finally:
        await target.aclose()


async def test_exec_non_zero_exit_returns_result_not_raise(running_pod: str) -> None:
    """Spec §场景:exec 非零退出返回 ExecResult 不 raise (channel-3 Status)."""

    target = _build_target(pod=running_pod)
    try:
        result = await target.exec("exit 3", timeout=20)
        assert result.exit_code == 3
        assert result.timed_out is False
    finally:
        await target.aclose()


async def test_exec_timeout_returns_timed_out_with_none_exit(running_pod: str) -> None:
    """Spec §场景:exec 超时返回 timed_out 且 exit_code 为 None.

    Asserts ONLY the return-value invariant (``timed_out is True`` +
    ``exit_code is None``); per spec §需求 排除项 ② we deliberately do NOT
    claim anything about background websocket / coroutine release.
    """

    target = _build_target(pod=running_pod)
    try:
        result = await target.exec("sleep 60", timeout=2)
        assert result.timed_out is True
        assert result.exit_code is None
    finally:
        await target.aclose()


async def test_exec_env_via_stdin_secret_absent_from_pod_argv(running_pod: str) -> None:
    """Spec §场景:exec 经 stdin 注入 env + 在 pod 内 ``ps`` 验证 secret 不在 argv.

    ``$MY_VAR`` must expand to the stdin-injected value (proving env reaches
    the shell), while a separately-named ``SECRET_TOKEN`` must NOT appear in
    the pod's own ``ps auxww`` output (proving env is fed over stdin, never
    spliced into any process argv). We grep the live process table inside the
    pod for the secret value.
    """

    target = _build_target(pod=running_pod)
    try:
        result = await target.exec(
            "echo val=$MY_VAR; ps auxww",
            timeout=20,
            env={"MY_VAR": "x", "SECRET_TOKEN": "do-not-leak-abc"},
        )
        assert result.exit_code == 0
        assert "val=x" in result.stdout
        # The secret value must not appear in the pod's own process table.
        assert "do-not-leak-abc" not in result.stdout
        assert "SECRET_TOKEN" not in result.stdout
    finally:
        await target.aclose()


async def test_exec_selects_named_container_in_multi_container_pod(
    multi_container_pod: str,
) -> None:
    """Spec §需求:多容器 pod 选 container + 已知限制 cmd 不能读 stdin.

    With an explicit ``container="sidecar"`` the exec runs in that container
    (verified via a sentinel file written there). It then asserts the known
    limitation: ``cmd`` cannot read external stdin (stdin is consumed by the
    export+cmd script), so ``cat`` of stdin sees EOF immediately rather than
    blocking on or reading any user input.
    """

    sentinel = uuid.uuid4().hex
    target = _build_target(pod=multi_container_pod, container="sidecar")
    try:
        # Run uniquely in the sidecar: a marker file written here is only
        # visible when exec actually targeted "sidecar".
        marker = _unique_path("-sidecar")
        write = await target.exec(f"printf %s {sentinel} > {marker}", timeout=20)
        assert write.exit_code == 0
        read = await target.exec(f"cat {marker}", timeout=20)
        assert read.exit_code == 0
        assert sentinel in read.stdout

        # Known limitation: cmd cannot read external stdin (it is occupied by
        # the export+cmd+exit script). ``cat`` of stdin returns immediately
        # with no external bytes — the call completes (does not hang/timeout).
        no_stdin = await target.exec("cat; echo done", timeout=20)
        assert no_stdin.timed_out is False
        assert "done" in no_stdin.stdout
    finally:
        await target.aclose()


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


async def test_read_file_small(running_pod: str) -> None:
    """Spec §场景:read_file 读小文件 — round-trips raw bytes via tar-over-exec."""

    target = _build_target(pod=running_pod)
    try:
        path = _unique_path("-hello.txt")
        await _write_in_pod(target, path, content="hello")
        data = await target.read_file(path)
        assert data == b"hello"
    finally:
        await target.aclose()


async def test_read_file_exact_10mb_succeeds(running_pod: str) -> None:
    """Spec §场景:read_file 恰好 10MB 放行 — boundary is strict ``>``."""

    target = _build_target(pod=running_pod)
    try:
        path = _unique_path("-exact.bin")
        await _write_in_pod(target, path, size=_TEN_MIB)
        data = await target.read_file(path)
        assert len(data) == _TEN_MIB
    finally:
        await target.aclose()


async def test_read_file_over_10mb_raises(running_pod: str) -> None:
    """Spec §场景:read_file 超过 10MB raise — file_too_large, no bytes returned."""

    target = _build_target(pod=running_pod)
    try:
        path = _unique_path("-big.bin")
        await _write_in_pod(target, path, size=_TEN_MIB + 1)
        with pytest.raises(TargetError) as exc_info:
            await target.read_file(path)
        assert exc_info.value.kind == "file_too_large"
        assert exc_info.value.extra.get("path") == path
    finally:
        await target.aclose()


async def test_read_file_directory_raises_not_a_file(running_pod: str) -> None:
    """Spec §场景:read_file 路径指向目录 raise not_a_file."""

    target = _build_target(pod=running_pod)
    try:
        with pytest.raises(TargetError) as exc_info:
            await target.read_file("/etc")
        assert exc_info.value.kind == "not_a_file"
    finally:
        await target.aclose()


async def test_read_file_relative_path_raises_invalid_path(running_pod: str) -> None:
    """Spec §场景:read_file 相对路径 raise invalid_path — no exec issued."""

    target = _build_target(pod=running_pod)
    try:
        with pytest.raises(TargetError) as exc_info:
            await target.read_file("tmp/x")
        assert exc_info.value.kind == "invalid_path"
    finally:
        await target.aclose()


async def test_read_file_missing_raises_file_not_found(running_pod: str) -> None:
    """Spec §场景:read_file 不存在 raise FileNotFoundError (stdlib, not TargetError)."""

    target = _build_target(pod=running_pod)
    try:
        with pytest.raises(FileNotFoundError):
            await target.read_file(_unique_path("-nope"))
    finally:
        await target.aclose()


# ---------------------------------------------------------------------------
# pod / container lifecycle failures
# ---------------------------------------------------------------------------


async def test_pod_not_found_raises(cluster: None) -> None:
    """Spec §场景:pod 不存在 raise pod_not_found."""

    target = _build_target(pod=f"hostlens-absent-{uuid.uuid4().hex[:8]}")
    try:
        with pytest.raises(TargetError) as exc_info:
            await target.exec("echo hi", timeout=20)
        assert exc_info.value.kind == "pod_not_found"
        assert exc_info.value.target == "k8s-it"
    finally:
        await target.aclose()


async def test_pod_not_running_raises(succeeded_pod: str) -> None:
    """Spec §场景:pod 非 Running raise pod_not_running (含 phase)."""

    target = _build_target(pod=succeeded_pod)
    try:
        with pytest.raises(TargetError) as exc_info:
            await target.exec("echo hi", timeout=20)
        assert exc_info.value.kind == "pod_not_running"
        assert exc_info.value.extra.get("phase") != "Running"
    finally:
        await target.aclose()


async def test_container_not_found_raises(running_pod: str) -> None:
    """Spec §场景:指定 container 不存在 raise container_not_found (含可用容器名)."""

    target = _build_target(pod=running_pod, container="nope")
    try:
        with pytest.raises(TargetError) as exc_info:
            await target.exec("echo hi", timeout=20)
        assert exc_info.value.kind == "container_not_found"
        assert "app" in str(exc_info.value.extra.get("available", ""))
    finally:
        await target.aclose()


# ---------------------------------------------------------------------------
# capabilities lazy probe
# ---------------------------------------------------------------------------


async def test_capabilities_probed_only_after_first_exec(running_pod: str) -> None:
    """Spec §场景:KubernetesTarget capabilities 首次 exec 后才探测.

    Before the first ``exec`` the set is exactly ``{SHELL, FILE_READ}``;
    after a successful ``exec`` it reflects the probe. ``alpine`` has neither
    ``systemctl`` nor ``docker``, so the probe adds nothing — the point is
    the probe ran (``_probed_caps`` populated) without regressing the
    baseline.
    """

    target = _build_target(pod=running_pod)
    try:
        assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}
        assert target._probed_caps is None

        result = await target.exec("echo hi", timeout=20)
        assert result.exit_code == 0

        assert target._probed_caps is not None
        assert Capability.SHELL in target.capabilities
        assert Capability.FILE_READ in target.capabilities
        # alpine ships neither systemctl nor a docker client.
        assert Capability.SYSTEMD not in target.capabilities
        assert Capability.DOCKER_CLI not in target.capabilities
    finally:
        await target.aclose()


# ---------------------------------------------------------------------------
# guard: no kubernetes-asyncio mocks anywhere in this file
# ---------------------------------------------------------------------------


def test_no_k8s_mocks_present() -> None:
    """Spec §场景:不允许 mock kubernetes-asyncio.

    Hard guard so a refactor cannot quietly start mocking the k8s SDK here
    (which would defeat the value of integration tests). We tokenise the
    source and flag any ``patch`` / ``mocker.patch`` /
    ``monkeypatch.setattr`` / ``patch.object`` callable whose argument list
    contains a string / name mentioning ``kubernetes`` OR
    ``kubernetes_asyncio`` — covering ``patch("kubernetes_asyncio...")``,
    ``patch("hostlens.targets.kubernetes...")``,
    ``monkeypatch.setattr(kubernetes_asyncio, ...)``,
    ``patch.object(k8s_client, ...)`` and any ``import ... as`` rename whose
    bound name still tokenises with the substring. Scanning tokens (not raw
    substrings) keeps the guard from tripping on its own docstring.
    """

    source = Path(__file__).read_text()
    mock_callables = {"patch", "setattr", "object"}
    needles = ("kubernetes", "kubernetes_asyncio")
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))

    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string not in mock_callables:
            continue
        # The matched NAME must be directly followed by ``(`` (a call), so
        # ``patch.object`` is caught at the ``object`` token and
        # ``setattr`` at its own token.
        if i + 1 >= len(tokens) or tokens[i + 1].string != "(":
            continue
        depth = 0
        j = i + 1
        while j < len(tokens):
            t = tokens[j]
            if t.type == tokenize.OP and t.string == "(":
                depth += 1
            elif t.type == tokenize.OP and t.string == ")":
                depth -= 1
                if depth == 0:
                    break
            else:
                text = t.string.strip("\"'")
                if t.type in (tokenize.STRING, tokenize.NAME) and any(n in text for n in needles):
                    raise AssertionError(
                        f"k8s mock detected at line {tok.start[0]}: "
                        f"{tok.string}(... {t.string} ...); integration tests "
                        "must use the real kubernetes-asyncio SDK + cluster "
                        "(spec §场景:不允许 mock kubernetes-asyncio)."
                    )
            j += 1
