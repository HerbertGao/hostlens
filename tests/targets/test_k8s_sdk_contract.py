"""SDK-fidelity self-check for ``kubernetes-asyncio`` (regression gate).

Spec: ``openspec/changes/add-kubernetes-target/specs/kubernetes-execution-target/spec.md``.

``KubernetesTarget`` relies on a number of *mechanical* SDK shapes that are
not part of any public stability contract: channel constants, the
``WsApiClient.parse_error_data`` classmethod blindly reading
``details.causes[0]["message"]`` (no ``reason`` filter), the async/sync
asymmetry of config loaders, the ``connect_get_namespaced_pod_exec`` /
``read_namespaced_pod`` call returning awaitable coroutines, and a handful
of model field defaults. The proposal phase introspected these against
``kubernetes-asyncio`` 36.1.0 and froze them here so a future SDK **major
bump** that drifts any of these surfaces fails this test instead of
silently breaking exec / read_file at runtime.

This test needs the ``[k8s]`` extra; if the SDK is missing it skips (the
CI dev extra includes it via ``hostlens[k8s]``).
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("kubernetes_asyncio")

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream import ws_client as ws_client_mod


def test_ws_api_client_importable() -> None:
    """``WsApiClient`` (the exec/read_file websocket client) must be importable."""

    assert WsApiClient is not None


def test_channel_constants() -> None:
    """Channel byte constants must be STDIN/STDOUT/STDERR/ERROR = 0/1/2/3."""

    assert ws_client_mod.STDIN_CHANNEL == 0
    assert ws_client_mod.STDOUT_CHANNEL == 1
    assert ws_client_mod.STDERR_CHANNEL == 2
    assert ws_client_mod.ERROR_CHANNEL == 3


def test_no_close_channel_constant() -> None:
    """There is no ``CLOSE_CHANNEL`` (the v4 protocol has no stdin half-close).

    The implementation relies on a trailing ``exit $?`` in the stdin script
    to terminate the session; a ``CLOSE_CHANNEL`` appearing would signal an
    SDK protocol change the implementation must react to.
    """

    assert not hasattr(ws_client_mod, "CLOSE_CHANNEL")


def test_parse_error_data_is_classmethod() -> None:
    """``parse_error_data`` must be a classmethod (called as ``WsApiClient.parse_error_data``)."""

    static = inspect.getattr_static(WsApiClient, "parse_error_data")
    assert isinstance(static, classmethod)


def test_parse_error_data_blindly_reads_first_cause() -> None:
    """``parse_error_data`` blindly reads ``causes[0]["message"]`` (no reason filter).

    The implementation deliberately mirrors this: it does NOT filter on
    ``reason == "ExitCode"``. A Status whose first cause lacks ``ExitCode``
    reason but carries a numeric message must still yield that number.
    """

    status = b'{"status":"Failure","details":{"causes":[{"reason":"SomethingElse","message":"7"}]}}'
    assert int(WsApiClient.parse_error_data(status)) == 7


def test_parse_error_data_success_is_zero() -> None:
    """A ``status == "Success"`` Status parses to exit code 0."""

    assert int(WsApiClient.parse_error_data(b'{"status":"Success"}')) == 0


def test_parse_error_data_raises_on_missing_causes() -> None:
    """Missing causes/message must raise (the impl wraps the call in try/except).

    The implementation's ``_parse_exit_code`` degrades any raise to ``None``;
    this asserts the SDK *does* raise so that contract is meaningful.
    """

    with pytest.raises(Exception):  # noqa: B017 - SDK raises KeyError/IndexError/ValueError
        WsApiClient.parse_error_data(b'{"status":"Failure","details":{"causes":[]}}')


def test_config_loader_async_sync_asymmetry() -> None:
    """``load_kube_config`` is async; ``load_incluster_config`` is sync.

    The implementation awaits the former and calls the latter without await;
    a drift here would deadlock or raise at construction.
    """

    assert inspect.iscoroutinefunction(k8s_config.load_kube_config)
    assert not inspect.iscoroutinefunction(k8s_config.load_incluster_config)


def test_load_kube_config_accepts_expected_kwargs() -> None:
    """``load_kube_config`` must accept ``client_configuration`` / ``config_file`` / ``context``.

    The implementation passes an explicit ``Configuration`` via
    ``client_configuration=`` (passing ``None`` would mutate the global
    default and cross-talk between targets) plus ``config_file`` / ``context``
    from the entry.
    """

    params = inspect.signature(k8s_config.load_kube_config).parameters
    for required in ("client_configuration", "config_file", "context"):
        assert required in params, f"load_kube_config missing kwarg {required}"


def test_api_client_close_is_async() -> None:
    """``ApiClient.close`` must be a coroutine function (awaited on teardown)."""

    assert inspect.iscoroutinefunction(k8s_client.ApiClient.close)


def test_pod_status_container_statuses_defaults_none() -> None:
    """``V1PodStatus.container_statuses`` defaults to ``None`` (the kubelet race).

    The running-state check must be None-safe because a just-turned-Running
    pod can have ``container_statuses is None`` before the kubelet writes
    status; a bare iteration would raise ``TypeError``.
    """

    assert k8s_client.V1PodStatus().container_statuses is None


def test_model_fields_exist() -> None:
    """``V1ContainerState.running`` / ``V1Pod.spec`` / ``V1Pod.status`` must exist."""

    assert hasattr(k8s_client.V1ContainerState(), "running")
    pod = k8s_client.V1Pod()
    assert hasattr(pod, "spec")
    assert hasattr(pod, "status")


async def test_api_calls_return_awaitable_coroutines() -> None:
    """``connect_get_namespaced_pod_exec`` / ``read_namespaced_pod`` calls return coroutines.

    In kubernetes-asyncio these are generated methods (NOT flagged by
    ``inspect.iscoroutinefunction`` on the function object), but *calling*
    them returns an awaitable coroutine that the implementation ``await``-s.
    ``ApiClient`` construction itself requires a running event loop (aiohttp
    connector), so this assertion runs inside an async test.
    """

    api = k8s_client.CoreV1Api(api_client=k8s_client.ApiClient())
    try:
        read = api.read_namespaced_pod("p", "ns")
        assert inspect.isawaitable(read)
        read.close()  # type: ignore[attr-defined]

        exec_call = api.connect_get_namespaced_pod_exec(
            "p", "ns", command=["/bin/sh"], _preload_content=False
        )
        assert inspect.isawaitable(exec_call)
        exec_call.close()  # type: ignore[attr-defined]
    finally:
        await api.api_client.close()
