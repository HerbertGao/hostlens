"""``KubernetesTarget`` — kubernetes-asyncio-backed read-only execution target.

Spec: ``openspec/changes/add-kubernetes-target/specs/kubernetes-execution-target/spec.md``.

Key invariants enforced by this module (each maps to a spec scenario; do
not relax without amending the spec):

- **kubernetes-asyncio is optional-dep** (D5): the module-level imports
  are wrapped in ``try/except ImportError`` so an environment without the
  ``[k8s]`` extra can still ``import hostlens.targets.kubernetes`` (the
  registry imports this module unconditionally to reference
  ``KubernetesTarget`` in its branch). Actual use raises
  ``TargetError(kind="k8s_sdk_unavailable")`` with an install hint.
- **Native async, NOT ``asyncio.to_thread``** (D1): kubernetes-asyncio is
  an async SDK; every API call is awaited directly — never wrapped in
  ``asyncio.to_thread`` (the key asymmetry with the synchronous docker-py).
- **Two clients per target instance** (D2): ① a plain ``ApiClient`` →
  ``CoreV1Api`` for reading pod status (``read_namespaced_pod``), and ②
  a ``WsApiClient`` → ``CoreV1Api`` for the exec/read_file websocket.
  Both are built lazily on first use, reused thereafter, and each
  ``await client.close()``-d on shutdown.
- **Two ordered entry guards**: ``exec`` / ``read_file`` first check
  ``_entry is None`` (→ ``k8s_no_entry``, without touching ``.enabled``),
  then ``_entry.enabled is False`` (→ ``target_disabled``) — both
  *before* any k8s API call (no client constructed, no API-server dial).
- **Proactive pod/container resolution** (D6): before any exec/read_file
  stream we ``read_namespaced_pod`` and decide ``pod_not_found`` /
  ``pod_not_running`` / ``container_not_found`` / ``container_not_running``
  from the pod object's distinct subtrees (``spec.containers`` vs
  ``status.container_statuses``) — never from locale-fragile exec error
  text. The running-state check is None-safe (``container_statuses``
  defaults to ``None`` in the kubelet-not-yet-written race window).

NOTE (group boundary): the exec / read_file websocket bodies (env-over-
stdin framing, channel demux, exit-code parse, tar handling) are owned by
group C. This module establishes the construction / client-lifecycle /
entry-guard / pod-resolution / capability-probe skeleton and routes exec
through an internal ``_ws_exec`` seam that group C fills in.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import posixpath
import re
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

try:
    from kubernetes_asyncio import client as k8s_client
    from kubernetes_asyncio import config as k8s_config
    from kubernetes_asyncio.client.rest import ApiException
    from kubernetes_asyncio.stream import WsApiClient
except ImportError:  # kubernetes-asyncio is an optional-dep (``hostlens[k8s]``).
    k8s_client = None
    k8s_config = None
    ApiException = None  # type: ignore[assignment,misc,unused-ignore]
    WsApiClient = None

from hostlens.core.exceptions import TargetError
from hostlens.targets._tar import READ_FILE_MAX_BYTES, extract_single_regular_file
from hostlens.targets.base import Capability, ExecResult

if TYPE_CHECKING:

    class TargetEntry(Protocol):
        # Structural shape injected by ``TargetRegistry.register`` — the
        # concrete ``K8sEntry`` (``hostlens.targets.config``) structurally
        # satisfies this. Kept TYPE_CHECKING-only so the module has no
        # runtime import dependency on config.
        name: str
        pod: str
        namespace: str
        container: str | None
        kubeconfig: str | None
        context: str | None
        enabled: bool


__all__ = ["KubernetesTarget"]


_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")

_PIP_INSTALL_HINT: Final[str] = 'pip install "hostlens[k8s]"'

# Env-key allowlist: env values are injected into the pod via shell
# ``export`` over stdin, so each key must be a valid shell identifier or
# it constructs an injection (``export ; rm -rf /=...``). Unlike docker's
# ``environment=`` dict (which never touches a shell), k8s must validate.
_ENV_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Memory-DoS backstop for ``read_file``: the whole ``tar cf -`` stdout is
# currently buffered before the shared extractor applies the precise 10 MiB
# per-member cap, so a multi-GB path would balloon the buffer. Abort
# accumulation once the raw stdout crosses this high-water mark (8x the
# per-file cap, ~80 MiB -- far above any legitimate single <=10 MiB file plus
# tar framing, and well above the multi-entry "not_a_file" test archive so
# that case is never misclassified as ``file_too_large``). The precise
# 10 MiB / ``not_a_file`` semantics still live in ``extract_single_regular_file``;
# true lazy streaming of the tar member is a follow-up.
_WS_TAR_MAX_BYTES: Final[int] = 8 * READ_FILE_MAX_BYTES


class _RawExecOutcome:
    """Internal result of a single websocket exec (pre-``ExecResult``).

    Carries the demuxed stdout/stderr and the exit code parsed from the
    error channel. ``timed_out`` distinguishes the ``asyncio.wait_for``
    cancellation path. Group C populates these from the websocket; group B
    only consumes the shape (capability probe / ``exec`` wrapping).
    """

    def __init__(
        self,
        *,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class _RawReadOutcome:
    """Internal result of the ``tar`` read websocket (raw stdout bytes).

    Unlike ``_RawExecOutcome`` (which UTF-8-decodes stdout for ``exec``),
    ``read_file`` must keep the tar stream as **raw bytes** — decoding
    would corrupt the binary archive. Carries the exit code parsed from the
    channel-3 ``v1.Status`` and a ``command_not_found`` flag so the caller
    can distinguish "no ``tar`` in the container" (``exec_failed``) from
    "``tar`` ran but the file is missing" (``FileNotFoundError``).
    """

    def __init__(
        self,
        *,
        exit_code: int | None,
        stdout: bytes,
        command_not_found: bool,
        status_message: str,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.command_not_found = command_not_found
        self.status_message = status_message


class KubernetesTarget:
    """Read-only execution target backed by per-instance kubernetes-asyncio clients.

    Construction is pure: no IO and no k8s call happens in ``__init__``
    (only the name-regex check and class-attribute assignment). The two
    clients (plain ``ApiClient`` for pod reads, ``WsApiClient`` for exec)
    are built lazily on the first ``exec`` / ``read_file`` and reused.

    Instances are built by ``build_registry_from_config`` and then
    ``TargetRegistry.register`` injects ``self._entry`` with the source
    ``K8sEntry`` so we can read the pod / namespace / container reference
    and the optional kubeconfig / context. Constructed standalone (no
    ``_entry``) the target raises ``TargetError(kind="k8s_no_entry")``
    when ``exec`` / ``read_file`` is called.
    """

    type: Literal["k8s"] = "k8s"

    def __init__(self, name: str) -> None:
        if _NAME_PATTERN.fullmatch(name) is None:
            raise TargetError(kind="invalid_target_name", target=name)
        self.name: str = name
        # Initial capability set; SYSTEMD / DOCKER_CLI are probed lazily
        # on the first successful exec. Stored as a fresh instance-level
        # set so two instances never share mutations.
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}
        self._probed_caps: set[Capability] | None = None

        # The two API clients, built lazily on first use and reused.
        # ``_read_api`` (plain ApiClient) reads pod status; ``_ws_api``
        # (WsApiClient) runs the exec/read_file websocket. The underlying
        # ApiClient objects are kept so we can ``await .close()`` them.
        self._read_api: Any = None
        self._ws_api: Any = None
        self._read_client: Any = None
        self._ws_client: Any = None

        # Serialises the lazy client build so concurrent ``exec`` /
        # ``read_file`` calls authenticate + construct clients at most
        # once. Materialised on first ``_get_lock`` call from a coroutine
        # so it binds to the running event loop (mirrors SSHTarget).
        self._lock: asyncio.Lock | None = None

        # Injected by ``TargetRegistry.register`` after name validation.
        self._entry: TargetEntry | None = None

    # ------------------------------------------------------------------
    # Entry guards
    # ------------------------------------------------------------------

    def _require_entry(self) -> TargetEntry:
        """Enforce the two ordered entry guards before any k8s API call.

        Order is fixed (spec §需求 两道入口防线顺序): the ``_entry is None``
        check comes first so we never evaluate ``None.enabled`` (which
        would raise a bare ``AttributeError``).
        """

        entry = self._entry
        if entry is None:
            raise TargetError(kind="k8s_no_entry", target=self.name)
        if entry.enabled is False:
            raise TargetError(kind="target_disabled", target=self.name)
        return entry

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        """Return the client-build lock, materialising on first access.

        Lazy creation guarantees the ``asyncio.Lock`` binds to the
        currently-running event loop (mirrors SSHTarget / DockerTarget).
        Pure sync code with no ``await`` between the None check and the
        assignment, so coroutine scheduling makes initialisation atomic
        under concurrent callers.
        """

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _load_configuration(self, entry: TargetEntry) -> Any:
        """Build a per-target ``Configuration`` from kubeconfig / in-cluster.

        Authentication has an async/sync asymmetry (D5, SDK-verified):

        - ``config.load_kube_config`` is **async** and returns a
          ``KubeConfigLoader`` (not a ``Configuration``); passing
          ``client_configuration=None`` mutates the global default and
          cross-talks between targets, so we build an explicit
          ``Configuration`` and pass it in.
        - ``config.load_incluster_config`` is **sync** (not awaited) but
          also accepts ``client_configuration=``.

        Returns the populated ``Configuration``; any failure surfaces as
        ``k8s_unavailable`` (kubeconfig load / API-server reachability /
        auth are all classified here).
        """

        import os

        cfg = k8s_client.Configuration()
        try:
            if os.environ.get("KUBERNETES_SERVICE_HOST"):
                k8s_config.load_incluster_config(client_configuration=cfg)
            else:
                await k8s_config.load_kube_config(
                    config_file=entry.kubeconfig,
                    context=entry.context,
                    client_configuration=cfg,
                )
        except Exception as exc:
            raise TargetError(
                kind="k8s_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        return cfg

    async def _build_clients(self) -> None:
        """Construct the two ``CoreV1Api`` clients under the build lock.

        ``k8s_client is None`` (extra not installed) → ``k8s_sdk_unavailable``
        with the pip install hint. Authentication / construction failures →
        ``k8s_unavailable``.
        """

        if k8s_client is None or WsApiClient is None or k8s_config is None:
            raise TargetError(
                kind="k8s_sdk_unavailable",
                target=self.name,
                hint=_PIP_INSTALL_HINT,
            )
        entry = self._require_entry()
        cfg = await self._load_configuration(entry)
        read_client = ws_client = None
        try:
            read_client = k8s_client.ApiClient(cfg)
            ws_client = WsApiClient(configuration=cfg)
            read_api = k8s_client.CoreV1Api(api_client=read_client)
            ws_api = k8s_client.CoreV1Api(api_client=ws_client)
        except Exception as exc:
            for _c in (read_client, ws_client):
                if _c is not None:
                    with contextlib.suppress(Exception):
                        await _c.close()
            raise TargetError(
                kind="k8s_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        self._read_client, self._ws_client = read_client, ws_client
        self._read_api, self._ws_api = read_api, ws_api

    async def _ensure_clients(self) -> None:
        """Build both clients lazily under the lock; reuse on later calls."""

        async with self._get_lock():
            if self._read_api is None or self._ws_api is None:
                await self._build_clients()

    # ------------------------------------------------------------------
    # Pod / container resolution
    # ------------------------------------------------------------------

    async def _resolve_container(self) -> str:
        """Read the pod via the plain ``ApiClient`` and resolve the target container.

        Proactive, from the pod object's distinct subtrees (spec §container
        选择 + running 态判定) — never from exec error text:

        - ``ApiException(status=404)`` → ``pod_not_found``; any other
          ``ApiException`` (401/403/connection) → ``k8s_unavailable``.
        - ``status.phase != "Running"`` → ``pod_not_running`` (carries phase).
        - target container name = ``_entry.container`` if set, else the
          default container ``spec.containers[0].name``.
        - a *named* container not in ``spec.containers[].name`` →
          ``container_not_found`` (carries available names).
        - None-safe running check: ``status.container_statuses is None``,
          the name not in that list, or the matching entry's
          ``state.running is None`` → ``container_not_running``.

        Returns the resolved concrete container name.
        """

        entry = self._require_entry()
        try:
            pod = await self._read_api.read_namespaced_pod(entry.pod, entry.namespace)
        except ApiException as exc:
            if getattr(exc, "status", None) == 404:
                raise TargetError(
                    kind="pod_not_found",
                    target=self.name,
                    message=_scrub(exc),
                ) from exc
            raise TargetError(
                kind="k8s_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc

        if pod.status is None or pod.spec is None:
            # A Running pod always carries status/spec (apiserver invariant);
            # converge a theoretical malformed-object AttributeError into the
            # structured transport-boundary contract (never bare exception).
            raise TargetError(kind="pod_not_running", target=self.name)

        phase = pod.status.phase
        if phase != "Running":
            raise TargetError(
                kind="pod_not_running",
                target=self.name,
                phase=phase,
            )

        spec_containers = pod.spec.containers or []
        spec_names = [c.name for c in spec_containers]
        if entry.container is not None:
            target_name = entry.container
            if target_name not in spec_names:
                raise TargetError(
                    kind="container_not_found",
                    target=self.name,
                    container=target_name,
                    available=",".join(spec_names),
                )
        else:
            # Default container = first container in the pod spec (k8s exec
            # ``container=None`` resolves to it). Resolve to a concrete name
            # so the running-state check below can look it up by name.
            if not spec_names:
                raise TargetError(
                    kind="container_not_found",
                    target=self.name,
                    available="",
                )
            target_name = spec_names[0]

        self._assert_container_running(pod, target_name)
        return target_name

    def _assert_container_running(self, pod: Any, target_name: str) -> None:
        """None-safe running-state assertion for ``target_name``.

        ``status.container_statuses`` defaults to ``None`` (kubelet has not
        yet written status in the just-turned-Running race window), so a
        bare iteration would raise ``TypeError``. ``None`` / name absent /
        ``state.running is None`` all map to ``container_not_running``.
        """

        statuses = pod.status.container_statuses
        if statuses is None:
            raise TargetError(
                kind="container_not_running",
                target=self.name,
                container=target_name,
            )
        match = next((s for s in statuses if s.name == target_name), None)
        if match is None or match.state is None or match.state.running is None:
            raise TargetError(
                kind="container_not_running",
                target=self.name,
                container=target_name,
            )

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` inside the target pod's container via a websocket exec.

        - Entry guards (``k8s_no_entry`` / ``target_disabled``) run before
          any k8s API call.
        - The target pod must exist + be Running and the resolved
          container must be in running state (proactive ``read_pod``).
        - ``cmd`` and ``env`` are fed over stdin (env as ``export`` lines);
          ``command`` is strictly ``["/bin/sh"]`` so secrets never reach
          the pod process argv (group C).
        - Timeout is enforced by an outer ``asyncio.wait_for``; on expiry
          we return ``ExecResult(timed_out=True, exit_code=None)``.
        - A non-zero exit is a normal ``ExecResult`` and does NOT raise;
          only transport-level failures raise.
        """

        self._require_entry()
        _validate_env_keys(env, target_name=self.name)
        await self._ensure_clients()
        container_name = await self._resolve_container()

        t0 = time.monotonic()
        outcome = await self._ws_exec(
            cmd,
            env=env,
            container=container_name,
            timeout=timeout,
        )
        duration = time.monotonic() - t0
        result = ExecResult(
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_seconds=duration,
            timed_out=outcome.timed_out,
        )
        if not outcome.timed_out:
            await self._probe_capabilities(container_name)
        return result

    async def _ws_exec(
        self,
        cmd: str,
        *,
        env: dict[str, str] | None,
        container: str,
        timeout: int,
    ) -> _RawExecOutcome:
        """Run a single websocket exec and return the raw outcome.

        Drives the bare k8s exec websocket (no high-level ``stream()``
        helper exists in kubernetes-asyncio): ``command=["/bin/sh"]`` so
        neither ``cmd`` nor ``env`` reach the pod process argv; the
        rendered ``export`` lines + ``cmd`` + a trailing ``exit $?`` are
        fed over the stdin channel (the trailing ``exit`` is what makes the
        v4 protocol terminate deterministically — it has no stdin
        half-close and ``ws.close()`` discards the channel-3 status).

        Inbound demux skips frames whose ``msg.data`` is shorter than one
        byte (empty / keepalive) so ``msg.data[0]`` never raises
        ``IndexError``; the first byte selects stdout (1) / stderr (2) /
        error (3). The exit code is parsed from the channel-3 ``v1.Status``
        via ``WsApiClient.parse_error_data``; a parse failure that is not a
        timeout yields ``exit_code=None`` (the ``ExecResult`` contract
        permits "no exit code, not timed out").

        The whole send/receive is wrapped in ``asyncio.wait_for`` here (not
        in the caller): a timeout returns ``_RawExecOutcome(timed_out=True,
        exit_code=None, ...)`` and best-effort-closes the websocket.
        """

        stdin_script = _render_stdin_script(cmd, env)
        try:
            return await asyncio.wait_for(
                self._run_ws_exec(container, stdin_script),
                timeout=timeout,
            )
        except TimeoutError:
            return _RawExecOutcome(
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=True,
            )

    async def _run_ws_exec(self, container: str, stdin_script: str) -> _RawExecOutcome:
        """Open the exec websocket, push the stdin script, demux the reply.

        Separated from ``_ws_exec`` so the ``asyncio.wait_for`` timeout
        boundary wraps exactly the send/receive coroutine. Transport / OCI
        failures are classified by ``_classify_ws_exception``: an exec-phase
        TOCTOU 404 maps to ``pod_not_found`` / ``container_not_found``, a
        missing ``/bin/sh`` maps to ``exec_failed``, everything else to
        ``k8s_unavailable``.
        """

        from kubernetes_asyncio.stream.ws_client import (
            ERROR_CHANNEL,
            STDERR_CHANNEL,
            STDIN_CHANNEL,
            STDOUT_CHANNEL,
        )

        try:
            ctx = await self._ws_api.connect_get_namespaced_pod_exec(
                self._entry.pod,  # type: ignore[union-attr]
                self._entry.namespace,  # type: ignore[union-attr]
                command=["/bin/sh"],
                container=container,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
            )
        except ApiException as exc:
            raise self._classify_ws_exception(exc, container) from exc

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        error_payload: bytes | None = None
        try:
            async with ctx as ws:
                await ws.send_bytes(bytes([STDIN_CHANNEL]) + stdin_script.encode("utf-8"))
                async for msg in ws:
                    data = msg.data
                    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
                        # Empty / keepalive / text frame — guard against the
                        # ``data[0]`` ``IndexError`` on a zero-length frame.
                        continue
                    channel = data[0]
                    payload = data[1:]
                    if channel == STDOUT_CHANNEL:
                        stdout_buf.extend(payload)
                    elif channel == STDERR_CHANNEL:
                        stderr_buf.extend(payload)
                    elif channel == ERROR_CHANNEL:
                        error_payload = bytes(payload)
        except ApiException as exc:
            raise self._classify_ws_exception(exc, container) from exc

        if _is_command_not_found(error_payload):
            # OCI runtime could not start ``/bin/sh`` (distroless / busybox
            # without sh): the channel-3 Status reports the failure rather
            # than an OS exit code. Classify ``exec_failed`` (API + pod are
            # healthy, only the command could not launch).
            raise TargetError(
                kind="exec_failed",
                target=self.name,
                message=_decode_status_message(error_payload),
            )
        exit_code = _parse_exit_code(error_payload)
        return _RawExecOutcome(
            exit_code=exit_code,
            stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
            stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
            timed_out=False,
        )

    def _classify_ws_exception(self, exc: Any, container: str) -> TargetError:
        """Map an exec-phase ``ApiException`` to a structured ``TargetError``.

        Exec-phase TOCTOU: the pod / container may vanish between the
        proactive ``read_namespaced_pod`` and the exec websocket dial. A
        404 here maps to ``pod_not_found`` (or ``container_not_found`` when
        the body names the container); a "command not found" / "no such
        file" (no ``/bin/sh``, distroless) maps to ``exec_failed`` — never
        ``k8s_unavailable``, which would misdiagnose a healthy API/pod as
        unreachable. Everything else is a transport failure.
        """

        message = _scrub(exc)
        status = getattr(exc, "status", None)
        if status == 404:
            lowered = message.lower()
            if "container" in lowered:
                return TargetError(
                    kind="container_not_found",
                    target=self.name,
                    container=container,
                    message=message,
                )
            return TargetError(kind="pod_not_found", target=self.name, message=message)
        lowered = message.lower()
        if "executable file not found" in lowered or "no such file" in lowered:
            return TargetError(kind="exec_failed", target=self.name, message=message)
        return TargetError(kind="k8s_unavailable", target=self.name, message=message)

    async def _probe_capabilities(self, container: str) -> None:
        """Detect ``SYSTEMD`` / ``DOCKER_CLI`` once on first successful exec.

        Uses POSIX ``command -v`` (not ``which`` — distroless / busybox
        compatible). Probe failures leave ``self._probed_caps`` set to
        whatever subset was detected so we do not re-probe, and never
        affect the triggering ``exec``'s result (best-effort side path,
        symmetric with DockerTarget).
        """

        if self._probed_caps is not None:
            return
        probed: set[Capability] = set()
        for binary, cap in (
            ("systemctl", Capability.SYSTEMD),
            ("docker", Capability.DOCKER_CLI),
        ):
            try:
                outcome = await self._ws_exec(
                    f"command -v {binary}",
                    env=None,
                    container=container,
                    timeout=10,
                )
            except Exception:
                # Probe is a best-effort side path; settle for what was
                # already detected and stop probing.
                break
            if outcome.exit_code == 0:
                probed.add(cap)
        self._probed_caps = probed
        self.capabilities |= probed

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    def _validate_and_normalize_path(self, path: str) -> str:
        """Reject paths ``tar`` must not see; fold ``..`` for absolute paths.

        Mirrors ``DockerTarget._validate_and_normalize_path`` (spec
        §read_file path 预校验):

        - NUL / newline → ``invalid_path`` (no exec issued; a newline in
          the path would also break the channel-3 / argv framing).
        - Relative path (not ``/``-absolute) → ``invalid_path``: the
          container cwd basis for a relative ``tar`` path is undefined.
        - Absolute path with ``..`` → folded with ``posixpath.normpath``
          (``PurePosixPath`` intentionally does NOT fold ``..``); the
          result is still a container-internal absolute path.
        """

        if "\x00" in path:
            raise TargetError(kind="invalid_path", target=self.name, path=path, reason="nul_byte")
        if "\n" in path:
            raise TargetError(kind="invalid_path", target=self.name, path=path, reason="newline")
        if not PurePosixPath(path).is_absolute():
            raise TargetError(
                kind="invalid_path", target=self.name, path=path, reason="relative_path"
            )
        return posixpath.normpath(path)

    async def read_file(self, path: str) -> bytes:
        """Read up to 10 MiB from ``path`` via ``tar cf -``-over-exec.

        K8s has no docker ``get_archive`` equivalent, so the file is read
        back as a tar stream produced by the container's own ``tar``
        (``kubectl cp`` mechanism). Failure modes (each a distinct
        ``TargetError`` kind, except missing files which surface the stdlib
        ``FileNotFoundError`` to align with LocalTarget / SSHTarget /
        DockerTarget):

        - NUL / newline / relative path → ``invalid_path`` (pre-request).
        - pod / container resolution (proactive ``read_pod``, symmetric
          with ``exec``) → ``pod_not_found`` / ``pod_not_running`` /
          ``container_not_found`` / ``container_not_running``.
        - File not found → ``FileNotFoundError`` (``tar`` non-zero exit
          **and** no stdout bytes — exit-code-led, not locale-fragile
          stderr-substring).
        - Path resolves to a directory / symlink / non-regular entry, or
          the archive contains more than one regular file → ``not_a_file``
          (decided before ``file_too_large``).
        - Regular file > 10 MiB → ``file_too_large``.
        - No ``tar`` binary in the container → ``exec_failed`` (with a hint
          that ``tar`` must be present).
        """

        self._require_entry()
        normalized = self._validate_and_normalize_path(path)
        await self._ensure_clients()
        container_name = await self._resolve_container()

        outcome = await self._ws_tar(normalized, container=container_name)

        if outcome.command_not_found:
            # The container has no ``tar`` (distroless): ``tar`` could not
            # launch at all → ``exec_failed`` (not FileNotFoundError, which
            # implies tar ran and reported the path missing).
            raise TargetError(
                kind="exec_failed",
                target=self.name,
                message=outcome.status_message,
                hint="read_file on k8s target requires `tar` in the container",
            )

        # File-not-found: tar started (we have a channel-3 exit code) but
        # exited non-zero and produced no archive bytes. Exit-code-led so a
        # non-English locale's stderr text never decides this (spec §read_file
        # 文件不存在判据).
        if outcome.exit_code not in (0, None) and not outcome.stdout:
            raise FileNotFoundError(path)

        return await asyncio.to_thread(self._extract_tar_bytes, outcome.stdout, path)

    def _extract_tar_bytes(self, tar_bytes: bytes, path: str) -> bytes:
        """Parse the buffered tar stream → the single regular file's bytes.

        Delegates to the shared ``extract_single_regular_file`` so the
        single-regular-file / ``not_a_file``-before-``file_too_large`` /
        10 MiB-backstop semantics stay identical to ``DockerTarget``. Runs
        in a worker thread (``tarfile`` is sync CPU work). The raw bytes are
        already buffered (captured from the exec websocket), but the
        per-member backstop in the shared helper still enforces the 10 MiB
        cap as the member is read.
        """

        return extract_single_regular_file(
            io.BytesIO(tar_bytes),
            not_a_file=lambda: TargetError(kind="not_a_file", target=self.name, path=path),
            file_too_large=lambda size: TargetError(
                kind="file_too_large", target=self.name, path=path, size=size
            ),
        )

    async def _ws_tar(self, path: str, *, container: str) -> _RawReadOutcome:
        """Run ``tar cf - <path>`` over the WsApiClient, capturing raw stdout.

        Symmetric with ``_ws_exec`` but for ``read_file``: ``stdin=False``
        (no channel-0 frame), ``command=["tar","cf","-",<path>]``, and the
        stdout is kept as **raw bytes** (not UTF-8-decoded — the tar archive
        is binary). The channel-3 ``v1.Status`` is parsed for the exit code
        (reused from ``exec``: ``WsApiClient.parse_error_data``) and sniffed
        for the OCI "command not found" signal so the caller can classify
        ``exec_failed`` (no ``tar``) vs ``FileNotFoundError`` (tar ran, file
        absent).
        """

        from kubernetes_asyncio.stream.ws_client import (
            ERROR_CHANNEL,
            STDERR_CHANNEL,
            STDOUT_CHANNEL,
        )

        try:
            ctx = await self._ws_api.connect_get_namespaced_pod_exec(
                self._entry.pod,  # type: ignore[union-attr]
                self._entry.namespace,  # type: ignore[union-attr]
                command=["tar", "cf", "-", path],
                container=container,
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
            )
        except ApiException as exc:
            raise self._classify_ws_exception(exc, container) from exc

        stdout_buf = bytearray()
        error_payload: bytes | None = None
        try:
            async with ctx as ws:
                async for msg in ws:
                    data = msg.data
                    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
                        # Empty / keepalive / text frame — guard against the
                        # ``data[0]`` ``IndexError`` on a zero-length frame.
                        continue
                    channel = data[0]
                    payload = data[1:]
                    if channel == STDOUT_CHANNEL:
                        stdout_buf.extend(payload)
                        if len(stdout_buf) > _WS_TAR_MAX_BYTES:
                            # Memory-DoS backstop: stop buffering before a
                            # multi-GB stream exhausts memory, then hand the
                            # ~80 MiB buffer to ``extract_single_regular_file``
                            # which decides the kind (directory tar -> not_a_file
                            # on the first DIRTYPE member; single regular file ->
                            # file_too_large at the 10 MiB per-member cap).
                            break
                    elif channel == STDERR_CHANNEL:
                        # ``tar``'s diagnostic text (busybox / GNU / locale-
                        # specific) is intentionally not used as a decision
                        # input — exit code + byte production decide.
                        continue
                    elif channel == ERROR_CHANNEL:
                        error_payload = bytes(payload)
        except ApiException as exc:
            raise self._classify_ws_exception(exc, container) from exc

        return _RawReadOutcome(
            exit_code=_parse_exit_code(error_payload),
            stdout=bytes(stdout_buf),
            command_not_found=_is_command_not_found(error_payload),
            status_message=_decode_status_message(error_payload),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close both API clients (best-effort).

        Both ``ApiClient.close`` are async, so each is awaited. Idempotent.
        """

        for attr in ("_read_client", "_ws_client"):
            client_obj = getattr(self, attr)
            if client_obj is not None:
                with contextlib.suppress(Exception):  # best-effort teardown
                    await client_obj.close()
                setattr(self, attr, None)
        self._read_api = None
        self._ws_api = None


def _shell_single_quote(value: str) -> str:
    """Wrap ``value`` in single quotes, escaping embedded single quotes.

    POSIX single quotes suppress all expansion ($(...) / backticks /
    newlines / ``$VAR``), so the only character needing care is ``'``
    itself, rendered as ``'\\''`` (close quote, escaped quote, reopen).
    """

    return "'" + value.replace("'", "'\\''") + "'"


def _render_stdin_script(cmd: str, env: dict[str, str] | None) -> str:
    """Render the stdin script fed to ``/bin/sh`` (env exports + cmd + exit).

    Env values are injected as ``export <K>=<single-quoted-V>`` lines so
    secrets never appear in the pod process argv (``command`` is strictly
    ``["/bin/sh"]``). The trailing ``exit $?`` makes ``sh`` terminate with
    the command's status deterministically — the v4 exec protocol has no
    stdin half-close, so the script itself must end the session (we do not
    rely on stdin EOF or the timeout).
    """

    lines: list[str] = []
    if env:
        for key, value in env.items():
            lines.append(f"export {key}={_shell_single_quote(value)}")
    lines.append(cmd)
    lines.append("exit $?")
    return "\n".join(lines) + "\n"


def _parse_exit_code(error_payload: bytes | None) -> int | None:
    """Parse the channel-3 ``v1.Status`` exit code (``None`` on absence/parse fail).

    Delegates to ``WsApiClient.parse_error_data`` (Success → 0, else the
    SDK blindly reads ``details.causes[0]["message"]`` as int — no
    ``reason`` filtering, matching SDK behaviour). The classmethod raises
    on a Status missing causes/message, so any parse failure (or absence
    of a channel-3 frame) degrades to ``None`` rather than a magic value.
    """

    if not error_payload:
        return None
    from kubernetes_asyncio.stream.ws_client import WsApiClient as _WsApiClient

    try:
        return int(_WsApiClient.parse_error_data(error_payload))
    except Exception:
        return None


def _decode_status_message(error_payload: bytes | None) -> str:
    """Best-effort human message from a channel-3 ``v1.Status`` payload."""

    if not error_payload:
        return ""
    return _scrub_text(error_payload.decode("utf-8", errors="replace"))


def _is_command_not_found(error_payload: bytes | None) -> bool:
    """Detect an OCI "could not start command" channel-3 Status.

    When ``/bin/sh`` is absent the channel-3 Status carries a non-Success
    reason whose message says the executable was not found (rather than a
    numeric exit-code cause). We sniff the raw Status text for that signal
    so the caller can classify ``exec_failed`` instead of degrading to a
    ``None`` exit code. The match is intentionally narrowed to container-
    runtime "executable does not exist" wording — a bare
    ``"no such file or directory"`` is too broad (it also fires on a
    missing *target* file across distros) and is left to the exit-code-led
    file-not-found path instead.
    """

    if not error_payload:
        return False
    import json

    try:
        status = json.loads(error_payload)
    except Exception:
        return False
    if not isinstance(status, dict) or status.get("status") == "Success":
        return False
    text = (status.get("message") or "").lower()
    return "executable file not found" in text or "starting container process" in text


def _scrub_text(text: str) -> str:
    """Run a plain string through the shared secret scrubber."""

    from hostlens.agent.tools_adapter import scrub_exception_message

    return scrub_exception_message(text)


def _validate_env_keys(env: dict[str, str] | None, *, target_name: str) -> None:
    """Reject env keys that are not valid shell identifiers (injection guard).

    env values are injected into the pod via shell ``export`` over stdin,
    so a key like ``"; rm -rf /"`` would render ``export ; rm -rf /=...``.
    Each key must match ``^[A-Za-z_][A-Za-z0-9_]*$`` (defense-in-depth;
    keys originate from controlled inspector parameters).
    """

    if not env:
        return
    for key in env:
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            raise TargetError(kind="invalid_env_key", target=target_name)


def _scrub(exc: BaseException) -> str:
    """Extract a k8s exception message and scrub incidental secrets.

    kubernetes-asyncio's ``ApiException`` exposes ``.reason`` (and a
    verbose ``str`` with headers/body). We stringify the reason (falling
    back to ``str(exc)``) and run the shared ``scrub_exception_message`` so
    any incidentally-embedded bearer token / ``*_KEY=`` / home path / IP is
    redacted before it reaches ``TargetError.__str__`` / structlog.
    """

    from hostlens.agent.tools_adapter import scrub_exception_message

    reason = getattr(exc, "reason", None)
    text = reason if isinstance(reason, str) and reason else str(exc)
    return scrub_exception_message(text)
