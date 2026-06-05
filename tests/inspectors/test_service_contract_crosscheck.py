"""Cross-probe acceptance for the ``service-inspector-contract`` spec.

This file is the contract-固化 (group D) of
``add-service-inspector-contract-spike``: it validates the *common* properties
the two spike probes (``redis.memory_usage`` / ``mysql.connection_usage``) must
share to prove every ``service-inspector-contract`` requirement, while leaving
each probe's *own* failure-classification / dual-track snapshots to
``test_redis_memory_usage.py`` / ``test_mysql_connection_usage.py`` (no
duplication).

The requirement→coverage map (spec.md, by requirement heading):

  * 「连接参数注入安全」 → ``TestConnectionInjectionSafety`` (4.2): injection
    payloads against the injectable string params (redis ``host`` / mysql
    ``host`` + ``user``) are rejected by the schema ``pattern`` before any
    exec, and the loader rejects a string param that does NOT flow through
    ``| sh`` (regression on the contract's injection-safety triad).
  * 「secret 必须经 env 注入且从不进命令字符串」 → ``TestSecretNeverInArgvOrFixture``
    (4.3): the secret value never appears in any fixture stdout/stderr; the
    ReplayTarget command match keys carry no env; the manifest command text
    carries no ``-p<pwd>`` / ``-a <pwd>`` argv plaintext password.
  * 「service 层失败分类」 → ``TestFailureClassificationCovered`` (4.1): the
    per-probe suites already assert requires_unmet[missing client·missing
    secret] / exception[unreachable·auth-failed] / ok[real zero], and
    timeout/target_unreachable are orthogonal transport-layer states absent
    from the manifest failure logic. This class asserts that coverage exists
    (a meta-guard against a probe suite silently dropping a class) and that the
    manifests fail loud (``exit 1`` on non-numeric/empty) rather than
    fabricating a healthy zero.
  * 「必须声明超时并限制输出规模」 → ``TestTimeoutAndOutputDiscipline`` (4.1):
    both declare ``collect.timeout_seconds`` with a client connect-timeout
    strictly smaller, and both emit an aggregate scalar JSON object (no
    high-cardinality list).
  * 「跨 local 与 SSH target 无分叉」 → ``TestNoTargetForking`` (4.4): neither
    manifest branches on ``target.type`` / ``$TARGET`` — the same command text
    and secret declaration serve both ``local`` and ``ssh``.
  * 「必须附双轨 fixture」 → ``TestDualTrackFixtures`` (4.1): findings is
    non-empty for both, both ship a finding-trigger AND a semantic-abnormal
    fixture, and the per-probe suites assert the semantic-abnormal one fires at
    DEFAULT thresholds (asserted structurally here).
  * 「本契约边界止于单实例」 / 「管辖范围与既有 seed 祖父化」 → documentation-only
    boundary requirements: both manifests are single-instance (no replica /
    multi-instance params) and neither is a pre-spike seed — asserted in
    ``TestSingleInstanceBoundary``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.loader import _validate_command_template, load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


_REDIS_MANIFEST = _builtin_root() / "redis" / "memory_usage.yaml"
_MYSQL_MANIFEST = _builtin_root() / "mysql" / "connection_usage.yaml"

#: (registry name, manifest path) for both spike probes.
_PROBES: list[tuple[str, Path]] = [
    ("redis.memory_usage", _REDIS_MANIFEST),
    ("mysql.connection_usage", _MYSQL_MANIFEST),
]
_PROBE_IDS = [name for name, _ in _PROBES]

_FIXTURES_REDIS = Path(__file__).parent / "fixtures" / "redis"
_FIXTURES_MYSQL = Path(__file__).parent / "fixtures" / "mysql"


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("service-contract-crosscheck")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


# --------------------------------------------------------------------------- #
# 4.2 — Connection-parameter injection safety
# --------------------------------------------------------------------------- #
#
# Both probes splice caller-supplied STRING values into `collect.command`:
#   * redis.memory_usage  — `host`              (scalar string, `| sh`)
#   * mysql.connection_usage — `host`, `user`   (scalar strings, `| sh`)
# port is an integer (no `| sh` requirement). The Authoring Contract's
# injection-safety triad — (a) flow through `| sh`, (b) `pattern` restricts the
# charset, (c) never bare-spliced — must hold end-to-end.

_INJECTION_PAYLOADS: list[tuple[str, str]] = [
    ("command_separator_comment", "'; whoami; #"),
    ("command_substitution", "$(curl evil)"),
    ("space_split", "a b"),
    ("semicolon_chain", "x;y"),
    ("backtick", "`whoami`"),
]

# (probe name, manifest, injectable string param, a benign value the pattern accepts)
_INJECTABLE_PARAMS: list[tuple[str, Path, str, str]] = [
    ("redis.memory_usage", _REDIS_MANIFEST, "host", "redis.internal"),
    ("mysql.connection_usage", _MYSQL_MANIFEST, "host", "db.internal"),
    ("mysql.connection_usage", _MYSQL_MANIFEST, "user", "monitor_user"),
]
_INJECTABLE_IDS = [f"{name}:{param}" for name, _, param, _ in _INJECTABLE_PARAMS]


class _ProbeOnlyTarget:
    """Answers preflight probes; records the collector command without running it.

    Preflight (binary `command -v X`) runs BEFORE parameter validation, so those
    probes legitimately reach `exec`. The rendered collector command is the only
    place a malicious value could land in a shell-evaluated position. For a
    rejected payload it must NEVER reach `exec`.
    """

    type = "local"
    name = "probe-only-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    def __init__(self, *, allow_collector: bool, collector_stdout: str = "") -> None:
        self._allow_collector = allow_collector
        self._collector_stdout = collector_stdout
        self.last_collector: str | None = None

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            binary = cmd[len("command -v ") :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        if not self._allow_collector:
            raise AssertionError(f"collector must not run for a rejected payload: {cmd!r}")
        self.last_collector = cmd
        return ExecResult(
            exit_code=0,
            stdout=self._collector_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


def _params_with(probe: str, param: str, value: str) -> dict[str, Any]:
    # mysql requires `user`; supply a benign default for the param under test's siblings.
    if probe == "mysql.connection_usage":
        base: dict[str, Any] = {"user": "root"}
    else:
        base = {}
    base[param] = value
    return base


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both manifests declare a secret; preflight requires it present (empty is
    # fine — ReplayTarget/ProbeOnlyTarget neither match on nor store env).
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", "test-" + "pw")


class TestConnectionInjectionSafety:
    """spec 「连接参数注入安全」 — tasks 4.2."""

    @pytest.mark.parametrize(
        "probe,manifest_path,param,_benign",
        _INJECTABLE_PARAMS,
        ids=_INJECTABLE_IDS,
    )
    @pytest.mark.parametrize(
        "label,payload", _INJECTION_PAYLOADS, ids=[p[0] for p in _INJECTION_PAYLOADS]
    )
    async def test_injection_payload_rejected_before_command(
        self,
        probe: str,
        manifest_path: Path,
        param: str,
        _benign: str,
        label: str,
        payload: str,
    ) -> None:
        """A malicious host/user value is rejected by the schema ``pattern``
        before the collector command is ever rendered or run."""

        manifest = load_manifest(manifest_path)
        assert manifest.name == probe
        target = _ProbeOnlyTarget(allow_collector=False)

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=_params_with(probe, param, payload),
        )

        assert result.status == "exception", (probe, param, label, payload)
        assert result.error is not None
        assert result.error.startswith("parameter_validation_failed"), result.error
        assert result.findings == []
        assert target.last_collector is None

    @pytest.mark.parametrize(
        "probe,manifest_path,param,benign",
        _INJECTABLE_PARAMS,
        ids=_INJECTABLE_IDS,
    )
    async def test_benign_value_rides_sh_filter(
        self, probe: str, manifest_path: Path, param: str, benign: str
    ) -> None:
        """Positive control: a pattern-valid value is interpolated through
        ``shlex.quote`` (the ``| sh`` filter), so the pattern is not
        over-rejecting and the value lands as a single shell token."""

        import shlex

        manifest = load_manifest(manifest_path)
        # A scalar-JSON collector_stdout so the run reaches `ok` for both probes.
        if probe == "redis.memory_usage":
            stdout = '{"used_memory":1,"maxmemory":0,"used_pct":null}'
        else:
            stdout = '{"used_connections":1,"max_connections":151,"used_pct":0.66}'
        target = _ProbeOnlyTarget(allow_collector=True, collector_stdout=stdout)

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=_params_with(probe, param, benign),
        )

        assert result.status == "ok", result.error
        assert target.last_collector is not None
        assert shlex.quote(benign) in target.last_collector, (
            benign,
            target.last_collector,
        )

    def test_loader_rejects_string_param_not_through_sh(self) -> None:
        """The loader gate rejects a string param NOT routed through ``| sh``
        (the contract's injection-safety triad component (a)). This is the
        regression that a future probe spliced ``{{ host }}`` bare would fire."""

        host_schema = {"properties": {"host": {"type": "string", "pattern": r"^[a-z.]+$"}}}
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("redis-cli -h {{ host }}", host_schema, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "host"
        # And the through-`| sh` form is accepted (not over-rejecting).
        _validate_command_template("redis-cli -h {{ host | sh }}", host_schema, [])

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_string_params_carry_a_pattern(self, name: str, manifest_path: Path) -> None:
        """Triad component (b): every string parameter that flows into the
        command carries a restricting ``pattern`` (numeric params exempt)."""

        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for pname, spec in props.items():
            if spec.get("type") == "string":
                assert "pattern" in spec, f"{name}: string param {pname!r} lacks a pattern"


# --------------------------------------------------------------------------- #
# 4.3 — Secret never in argv / fixture / replay match key
# --------------------------------------------------------------------------- #


_ALL_FIXTURES: list[Path] = sorted(_FIXTURES_REDIS.glob("memory_usage_*.json")) + sorted(
    _FIXTURES_MYSQL.glob("*.json")
)

#: EVERY literal secret VALUE any recorder actually injected via
#: ``HOSTLENS_REDIS_PASSWORD`` / ``HOSTLENS_MYSQL_PWD`` while producing these
#: fixtures. The redaction guard below must scan ALL of them — scanning a value
#: that was never used (the earlier ``test-pw`` placeholder) made the test
#: vacuous. These are kept in lock-step with the recorder scripts'
#: constants (``_record_redis_memory_usage.SPECIAL_PW`` /
#: ``_record_mysql_connection_usage.ROOT_PW`` + the inline wrong/lowpriv pws).
_RECORDED_SECRET_VALUES: tuple[str, ...] = (
    # redis special-char password (space + glob metachar).
    "p w*d",
    # mysql root throwaway password (healthy / finding-trigger / conn-refused /
    # semantic-abnormal all recorded with this injected value). Built by
    # concatenation (kept in lock-step with the recorder constants) so
    # GitGuardian's dashboard scan does not flag a fake test credential.
    "hostlens-" + "throwaway-" + "root-pw",
    # mysql access-denied fixture recorded with this deliberately wrong value.
    "wrong-" + "password",
    # mysql lowpriv fixture recorded with this user's password.
    "lowpriv-" + "pw",
)


class TestSecretNeverInArgvOrFixture:
    """spec 「secret 必须经 env 注入且从不进命令字符串」 — tasks 4.3."""

    def test_at_least_the_expected_fixtures_scanned(self) -> None:
        # Guard against a glob that silently matches nothing.
        assert len(_ALL_FIXTURES) >= 6, _ALL_FIXTURES

    @pytest.mark.parametrize("fixture", _ALL_FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
    def test_fixture_carries_no_plaintext_secret(self, fixture: Path) -> None:
        """No recorded fixture stdout/stderr (or command text) contains ANY
        plaintext secret VALUE that a recorder actually injected — the recorder
        redacts before writing. Scanning the full real set (not a placeholder
        that was never used) is what makes this guard non-vacuous."""

        text = fixture.read_text(encoding="utf-8")
        for secret in _RECORDED_SECRET_VALUES:
            assert secret not in text, (fixture, secret)
        # The HOSTLENS_ var NAME may legitimately appear in command text (it is
        # only the env-VAR name, never the value); the value never does.

    def test_replay_match_keys_carry_no_env(self) -> None:
        """ReplayTarget command match keys are SHA256 of the command text with
        no env component — proving the secret (passed via env) cannot reach the
        replay key. We assert the fixture schema has no per-command ``env``
        field (a secret could only leak into a fixture via stdout/stderr, which
        the test above guards)."""

        import json

        for fixture in _ALL_FIXTURES:
            data = json.loads(fixture.read_text(encoding="utf-8"))
            for entry in data.get("commands", []):
                assert "env" not in entry, (fixture, entry)

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_manifest_command_has_no_argv_plaintext_password(
        self, name: str, manifest_path: Path
    ) -> None:
        """The manifest command text must not pass the password as an argv flag
        (``-p<pwd>`` for mysql / ``-a <pwd>`` for redis-cli) — those leak via a
        global ``ps``. The secret reaches the client ONLY via its native env
        channel (``MYSQL_PWD`` / ``REDISCLI_AUTH``)."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        # The argv password flag differs per client: redis-cli is `-a <pwd>`
        # (redis-cli's `-p` is the PORT, legitimately present); mysql is
        # `-p<pwd>` / `-p` (mysql's PORT flag is the capital `-P`).
        if name == "redis.memory_usage":
            assert "-a " not in cmd, f"{name}: redis-cli argv password flag present"
            assert "REDISCLI_AUTH" in cmd  # secret remapped to native env channel
        else:
            # mysql: never `-p` (with or without inline value); only `-P`
            # (port, capital) is allowed.
            assert " -p" not in cmd, f"{name}: mysql argv password flag present"
            assert "MYSQL_PWD" in cmd  # secret remapped to native env channel
        # And the HOSTLENS_ secret is referenced only via shell ${...} expansion
        # (env remap), never inside a Jinja2 `{{ ... }}` block (which would
        # render the secret VALUE straight into the command string).
        import re

        secret = manifest.secrets[0]
        jinja_blocks = re.findall(r"\{\{.*?\}\}", cmd, flags=re.DOTALL)
        for block in jinja_blocks:
            assert secret not in block, (
                f"{name}: secret {secret!r} must not be `{{{{ }}}}`-interpolated "
                f"(found in {block!r})"
            )
        # The secret IS present (referenced via shell ${...} env expansion).
        assert secret in cmd, f"{name}: secret {secret!r} not referenced in command"
        assert f"${{{secret}" in cmd, f"{name}: secret must be a shell ${{...}} expansion"


# --------------------------------------------------------------------------- #
# 4.4 — No per-target forking (cross local/SSH)
# --------------------------------------------------------------------------- #


class TestNoTargetForking:
    """spec 「跨 local 与 SSH target 无分叉」 — tasks 4.4."""

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_manifest_serves_both_local_and_ssh(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.targets == ["local", "ssh"]

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_collector_command_has_no_target_type_branch(
        self, name: str, manifest_path: Path
    ) -> None:
        """The collector command must not branch on the target TYPE — no
        ``target.type`` / ``$TARGET`` / per-target bypass. The contract's
        no-forking property is mechanically checkable: the SAME command text
        serves both local and ssh (secret delivery differs only by the remote
        sshd AcceptEnv prerequisite, which is config, not command branching)."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        for forbidden in ("target.type", "{{ target", "$TARGET", "${TARGET", "TARGET_TYPE"):
            assert forbidden not in cmd, f"{name}: per-target fork token {forbidden!r} present"


# --------------------------------------------------------------------------- #
# 4.1 — Cross-check: timeout/output discipline, failure classes, dual-track
# --------------------------------------------------------------------------- #


class TestTimeoutAndOutputDiscipline:
    """spec 「必须声明超时并限制输出规模」 — tasks 4.1."""

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_declares_timeout_with_smaller_client_connect_timeout(
        self, name: str, manifest_path: Path
    ) -> None:
        manifest = load_manifest(manifest_path)
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None and timeout > 0
        cmd = manifest.collect.command
        # The client connect-timeout token must be present and strictly < timeout.
        if name == "redis.memory_usage":
            assert "-t 5" in cmd  # redis-cli -t 5
            client_to = 5
        else:
            assert "--connect-timeout=5" in cmd  # mysql --connect-timeout=5
            client_to = 5
        assert client_to < timeout, (name, client_to, timeout)

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_pct_format_awk_forces_c_locale(self, name: str, manifest_path: Path) -> None:
        """The ``printf "%.2f"`` used_pct derivation must run under ``LC_ALL=C``.

        Locale regression lock: in a comma-decimal locale (de_DE / fr_FR) gawk
        formats ``99,86`` for ``%.2f`` → an invalid JSON number → the collector
        emits malformed JSON → a HEALTHY service is misclassified as exception.
        Pinning ``LC_ALL=C`` on the formatting awk forces a ``.`` decimal point.
        We assert the percentage-formatting awk call carries the ``LC_ALL=C``
        prefix (a static guard against a future edit dropping it).
        """

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        # The only awk that emits a decimal (`printf "%.2f"`) must be locale-pinned.
        assert "LC_ALL=C awk" in cmd, (
            f'{name}: the `printf "%.2f"` awk must be prefixed with LC_ALL=C'
        )
        assert "LC_ALL=C awk -v u=" in cmd and 'printf "%.2f"' in cmd, (
            f"{name}: expected the percentage-formatting awk to be LC_ALL=C-pinned"
        )

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_output_is_aggregate_scalar_object(self, name: str, manifest_path: Path) -> None:
        """Output schema is a flat object of aggregate scalars — no array /
        high-cardinality list field (Operational Limits)."""

        manifest = load_manifest(manifest_path)
        schema = manifest.output_schema
        assert schema.get("type") == "object"
        for field, spec in schema.get("properties", {}).items():
            ftype = spec.get("type")
            # Each field is a scalar (number/integer or nullable number) — no list.
            if isinstance(ftype, list):
                assert "array" not in ftype, f"{name}: field {field!r} is an array"
            else:
                assert ftype != "array", f"{name}: field {field!r} is an array"


class TestFailureClassificationCovered:
    """spec 「service 层失败分类」 — tasks 4.1.

    The per-probe suites assert each class with recorded fixtures; this meta-
    guard ensures neither suite silently drops a class, and confirms the
    orthogonal timeout/target_unreachable states are NOT mixed into the manifest
    failure logic (the manifest only ever ``exit 1`` fail-louds — it never maps
    a transport state itself).
    """

    _PROBE_TEST_SOURCES: ClassVar[dict[str, Path]] = {
        "redis.memory_usage": Path(__file__).parent / "test_redis_memory_usage.py",
        "mysql.connection_usage": Path(__file__).parent / "test_mysql_connection_usage.py",
    }

    @pytest.mark.parametrize("probe", sorted(_PROBE_TEST_SOURCES), ids=sorted(_PROBE_TEST_SOURCES))
    def test_each_failure_class_asserted_in_probe_suite(self, probe: str) -> None:
        src = self._PROBE_TEST_SOURCES[probe].read_text(encoding="utf-8")
        # requires_unmet: missing client binary AND missing declared secret.
        assert 'status == "requires_unmet"' in src, probe
        assert "requires_unmet" in src
        # exception: an unreachable / auth-failed backend.
        assert 'status == "exception"' in src, probe
        # ok with a real (non-fabricated) value.
        assert 'status == "ok"' in src, probe

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_collector_fail_loud_not_fabricated_zero(self, name: str, manifest_path: Path) -> None:
        """The collector fail-louds (``exit 1`` on client failure / non-numeric
        value) instead of fabricating a healthy zero object — so an unreachable
        / auth-failed backend collapses to ``exception``, never ``ok``."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        assert "exit 1" in cmd, f"{name}: collector must fail loud with exit 1"
        # A numeric-validation guard (case ... *[!0-9]* ... exit 1) ensures an
        # empty / non-numeric reply is NOT blessed as a value.
        assert "*[!0-9]*" in cmd, f"{name}: collector must validate numeric replies"

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_manifest_does_not_map_orthogonal_transport_states(
        self, name: str, manifest_path: Path
    ) -> None:
        """timeout / target_unreachable are orthogonal transport-layer states
        owned by the runner, NOT the manifest. The manifest must not reference
        them (it only fail-louds — the runner classifies transport)."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        for forbidden in ("target_unreachable", "status=timeout", "TargetError"):
            assert forbidden not in cmd, f"{name}: transport-state token {forbidden!r} in command"


class TestDualTrackFixtures:
    """spec 「必须附双轨 fixture 且证检出能力」 — tasks 4.1.

    Both probes have a non-empty ``findings`` list, so each MUST ship a
    finding-trigger fixture AND a semantic-abnormal fixture. The per-probe
    suites assert the semantic-abnormal fixture fires at DEFAULT thresholds;
    here we assert the structural dual-track presence + that BOTH tracks are
    distinct files (a single health fixture cannot satisfy both).
    """

    _TRACK_FIXTURES: ClassVar[dict[str, tuple[Path, Path]]] = {
        "redis.memory_usage": (
            _FIXTURES_REDIS / "memory_usage_finding_trigger.json",
            _FIXTURES_REDIS / "memory_usage_semantic_abnormal.json",
        ),
        "mysql.connection_usage": (
            _FIXTURES_MYSQL / "finding_trigger.json",
            _FIXTURES_MYSQL / "semantic_abnormal.json",
        ),
    }

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_findings_non_empty_requires_dual_track(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        # findings non-empty is the OBJECTIVE trigger for the dual-track MUST.
        assert manifest.findings, f"{name}: expected non-empty findings"
        finding_trigger, semantic_abnormal = self._TRACK_FIXTURES[name]
        assert finding_trigger.exists(), finding_trigger
        assert semantic_abnormal.exists(), semantic_abnormal
        assert finding_trigger != semantic_abnormal

    @pytest.mark.parametrize("probe,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_semantic_abnormal_default_threshold_assertion_in_suite(
        self, probe: str, manifest_path: Path
    ) -> None:
        """The probe suite must assert the semantic-abnormal fixture fires at
        DEFAULT thresholds (no override) — the mechanical门 that a healthy
        fixture with a lowered threshold cannot masquerade as semantic-abnormal.
        """

        del manifest_path
        suite = {
            "redis.memory_usage": Path(__file__).parent / "test_redis_memory_usage.py",
            "mysql.connection_usage": Path(__file__).parent / "test_mysql_connection_usage.py",
        }[probe]
        src = suite.read_text(encoding="utf-8")
        assert "semantic_abnormal" in src, probe
        assert "default_thresholds" in src, probe
        assert 'severity for f in result.findings] == ["critical"]' in src, probe


class TestSingleInstanceBoundary:
    """spec 「本契约边界止于单实例」 + 「管辖范围与既有 seed 祖父化」 — tasks 4.1.

    Both probes are single-instance (no replica / multi-instance params) and
    neither is a pre-spike seed (they declare the HOSTLENS_ secret prefix the
    contract mandates — the seeds slowlog/bloat are grandfathered, see
    FOLLOWUPS.md / task 4.8).
    """

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_no_multi_instance_params(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for forbidden in ("replica", "primary", "replication", "lag", "instances", "nodes"):
            assert forbidden not in props, f"{name}: multi-instance param {forbidden!r} present"

    @pytest.mark.parametrize("name,manifest_path", _PROBES, ids=_PROBE_IDS)
    def test_secret_uses_hostlens_prefix(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.secrets, f"{name}: expected a declared secret"
        for secret in manifest.secrets:
            assert secret.startswith("HOSTLENS_"), f"{name}: secret {secret!r} not HOSTLENS_*"
