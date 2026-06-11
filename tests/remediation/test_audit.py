"""Executable contract checks for the remediation `AuditLog` (M9 P2, group C).

Maps to spec §需求:audit 必须 append-only JSONL、两段式(intent + result)、记三态、
脱敏、不可写不静默. Real file IO into a tmp `XDG_DATA_HOME` (no mocking the
filesystem). Each test really drives a write and parses the resulting JSONL,
asserting: two-phase intent+result, the three failure states machine-distinct,
`timed_out:true` vs dropped-connection (`exit_code:null,timed_out:false`),
`who` from `pwd` (not `$USER`) with a `getpwuid` KeyError fallback, best-effort
redaction, and the intent-vs-result write-failure timing distinction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hostlens.remediation.audit import (
    AuditError,
    AuditLog,
    default_audit_path,
    resolve_actor,
)
from hostlens.remediation.executor import (
    PhaseOutcome,
    PlanExecutionResult,
    RollbackOutcome,
    StepOutcome,
)
from hostlens.remediation.models import RemediationPlan, RemediationStep
from hostlens.targets.base import ExecResult

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _plan() -> RemediationPlan:
    return RemediationPlan(
        finding_id="disk-full",
        target_name="prod-web-01",
        rationale="r",
        steps=[
            RemediationStep(
                description="d",
                precheck_cmd=None,
                forward_cmd="echo go",
                rollback_cmd="echo undo",
                verify_cmd="true",
                risk_level="low",
            )
        ],
        estimated_duration_seconds=1,
    )


def _ok(exit_code: int = 0) -> ExecResult:
    return ExecResult(
        exit_code=exit_code, stdout="", stderr="", duration_seconds=0.0, timed_out=False
    )


def _phase(
    phase: str, cmd: str, result: ExecResult | None, *, transport: str | None = None
) -> PhaseOutcome:
    return PhaseOutcome(phase=phase, cmd=cmd, result=result, transport_error=transport)  # type: ignore[arg-type]


def _step_outcome(status: str, phases: list[PhaseOutcome], *, index: int = 0) -> StepOutcome:
    return StepOutcome(
        index=index,
        description="d",
        risk_level="low",
        status=status,
        phases=phases,  # type: ignore[arg-type]
    )


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "hostlens" / "audit.log"


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# default path honours XDG_DATA_HOME
# --------------------------------------------------------------------------- #


def test_default_audit_path_uses_xdg_data_home(audit_path: Path) -> None:
    assert default_audit_path() == audit_path


# --------------------------------------------------------------------------- #
# Two-phase intent + result
# --------------------------------------------------------------------------- #


def test_two_phase_intent_then_result_jsonl(audit_path: Path) -> None:
    plan = _plan()
    log = AuditLog()
    log.precheck_writable()
    log.write_intent(plan)
    result = PlanExecutionResult(
        steps=[
            _step_outcome(
                "succeeded",
                [
                    _phase("forward", "echo go", _ok()),
                    _phase("verify", "true", _ok()),
                ],
            )
        ],
        rollbacks=[],
        succeeded=True,
        rollback_complete=True,
    )
    log.write_result(plan, result)

    lines = _read_lines(audit_path)
    assert len(lines) == 2
    intent, res = lines
    assert intent["type"] == "intent"
    assert intent["phase"] == "started"
    assert intent["finding_id"] == "disk-full"
    assert intent["target_name"] == "prod-web-01"
    assert isinstance(intent["plan_sha256"], str) and len(intent["plan_sha256"]) == 64
    assert res["type"] == "result"
    assert res["succeeded"] is True
    # intent and result share the same plan hash (same plan content).
    assert res["plan_sha256"] == intent["plan_sha256"]


def test_append_only_does_not_truncate(audit_path: Path) -> None:
    plan = _plan()
    log = AuditLog()
    log.precheck_writable()
    log.write_intent(plan)
    log.write_intent(plan)
    assert len(_read_lines(audit_path)) == 2  # second write appended, not overwrote


# --------------------------------------------------------------------------- #
# Three failure states machine-distinct + timed_out vs dropped connection
# --------------------------------------------------------------------------- #


def test_three_failure_states_and_timeout_vs_dropped_distinguishable(audit_path: Path) -> None:
    plan = _plan()
    log = AuditLog()
    log.precheck_writable()

    timed_out = ExecResult(
        exit_code=None, stdout="", stderr="", duration_seconds=0.0, timed_out=True
    )
    dropped = ExecResult(
        exit_code=None, stdout="", stderr="", duration_seconds=0.0, timed_out=False
    )
    result = PlanExecutionResult(
        steps=[
            _step_outcome("precheck-blocked", [_phase("precheck", "pc", _ok(1))], index=0),
            _step_outcome("forward-failed", [_phase("forward", "fw", timed_out)], index=1),
            _step_outcome("forward-failed", [_phase("forward", "fw2", dropped)], index=2),
            _step_outcome("verify-failed", [_phase("verify", "vf", _ok(1))], index=3),
        ],
        rollbacks=[],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(plan, result)

    res = _read_lines(audit_path)[0]
    steps = res["steps"]
    assert isinstance(steps, list)
    by_status = {s["status"]: s for s in steps}  # type: ignore[index]
    assert set(by_status) == {"precheck-blocked", "forward-failed", "verify-failed"}

    # Two forward-failed steps: one timed_out, one dropped — mechanically distinct.
    forward_failed = [s for s in steps if s["status"] == "forward-failed"]  # type: ignore[index]
    timed_phase = forward_failed[0]["phases"][0]  # type: ignore[index]
    dropped_phase = forward_failed[1]["phases"][0]  # type: ignore[index]
    assert timed_phase["exit_code"] is None and timed_phase["timed_out"] is True
    assert dropped_phase["exit_code"] is None and dropped_phase["timed_out"] is False


def test_transport_error_recorded_in_audit(audit_path: Path) -> None:
    plan = _plan()
    log = AuditLog()
    log.precheck_writable()
    result = PlanExecutionResult(
        steps=[
            _step_outcome(
                "forward-failed",
                [_phase("forward", "fw", None, transport="transport_error:ssh_connection_lost")],
            )
        ],
        rollbacks=[
            RollbackOutcome(index=0, description="d", status="rollback-unavailable", phase=None)
        ],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(plan, result)
    res = _read_lines(audit_path)[0]
    phase = res["steps"][0]["phases"][0]  # type: ignore[index]
    assert phase["exit_code"] is None
    assert phase["transport_error"] == "transport_error:ssh_connection_lost"
    # rollback-unavailable recorded with no phase fields.
    assert res["rollbacks"][0]["status"] == "rollback-unavailable"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# who from pwd, not $USER; getpwuid KeyError fallback
# --------------------------------------------------------------------------- #


def test_who_from_pwd_not_user_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import hostlens.remediation.audit as audit_mod

    monkeypatch.setenv("USER", "spoofed-attacker")
    monkeypatch.setattr(audit_mod.os, "geteuid", lambda: 4242)

    @dataclass
    class _PwEntry:
        pw_name: str

    monkeypatch.setattr(audit_mod.pwd, "getpwuid", lambda uid: _PwEntry(pw_name="realuser"))
    actor = resolve_actor()
    assert actor == "realuser(4242)"
    assert "spoofed-attacker" not in actor


def test_who_falls_back_to_uid_when_no_passwd_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import hostlens.remediation.audit as audit_mod

    monkeypatch.setattr(audit_mod.os, "geteuid", lambda: 100777)

    def _raise(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.setattr(audit_mod.pwd, "getpwuid", _raise)
    # Must not crash on a container arbitrary UID with no passwd entry.
    assert resolve_actor() == "100777"


# --------------------------------------------------------------------------- #
# best-effort redaction (key=value / Bearer / JWT / sk- plus known-tool
# flag-form; unknown-tool flag-form is still a residual passthrough)
# --------------------------------------------------------------------------- #


def test_audit_redacts_recognizable_secret_form(audit_path: Path) -> None:
    plan = RemediationPlan(
        finding_id="f",
        target_name="t",
        rationale="r",
        steps=[
            RemediationStep(
                description="d",
                precheck_cmd=None,
                forward_cmd="deploy --token=supersecretvalue123",
                rollback_cmd="echo undo",
                verify_cmd="true",
                risk_level="low",
            )
        ],
        estimated_duration_seconds=1,
    )
    log = AuditLog()
    log.precheck_writable()
    result = PlanExecutionResult(
        steps=[
            _step_outcome(
                "forward-failed",
                [_phase("forward", "deploy --token=supersecretvalue123", _ok(1))],
            )
        ],
        rollbacks=[],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(plan, result)
    raw = audit_path.read_text()
    # key=value form is redacted by redact_text.
    assert "supersecretvalue123" not in raw


def test_audit_redacts_transport_error_secret_form(audit_path: Path) -> None:
    # M1 regression: transport_error must pass through redact_text just like
    # cmd. executor._run_phase's broad catch puts `f"{type}: {exc}"` into
    # transport_error, and an exec exception message can echo the command
    # string (e.g. OSError from create_subprocess_shell), leaking a secret into
    # the append-only audit log if not redacted at this persistence boundary.
    log = AuditLog()
    log.precheck_writable()
    result = PlanExecutionResult(
        steps=[
            _step_outcome(
                "forward-failed",
                [
                    _phase(
                        "forward",
                        "redis-cli set k v",
                        None,
                        transport="OSError: token=sk-supersecretvalue123 failed",
                    )
                ],
            )
        ],
        rollbacks=[],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(_plan(), result)
    raw = audit_path.read_text()
    # key=value form inside transport_error is redacted by redact_text.
    assert "sk-supersecretvalue123" not in raw
    phase = _read_lines(audit_path)[0]["steps"][0]["phases"][0]  # type: ignore[index]
    # The field is still present (recognizable as an auth-shaped error) but
    # masked — the kept-prefix marker proves redact_text ran.
    assert "token=" in phase["transport_error"]
    assert "..." in phase["transport_error"]


def test_audit_redacts_transport_error_known_tool_flag_form(audit_path: Path) -> None:
    # A known-tool glued-flag secret echoed inside a transport_error message
    # (when the tool is the command head of the quoted segment) must be masked
    # before it reaches the append-only audit log, just like the cmd field.
    log = AuditLog()
    log.precheck_writable()
    result = PlanExecutionResult(
        steps=[
            _step_outcome(
                "forward-failed",
                [
                    _phase(
                        "forward",
                        "mysql ping",
                        None,
                        transport="mysql -psupersecretpw connect failed",
                    )
                ],
            )
        ],
        rollbacks=[],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(_plan(), result)
    raw = audit_path.read_text()
    assert "supersecretpw" not in raw


def test_audit_redacts_known_tool_flag_form_but_unknown_is_residual(
    audit_path: Path,
) -> None:
    # redact_text covers known-client flag-form secrets (`mysql -p<pw>`), so they
    # are masked before reaching the permanent audit log. Unknown tools
    # (`myhack -p<pw>`) remain a known residual passthrough — best-effort, not a
    # security boundary (the EUID==0 gate + env injection still cover them).
    log = AuditLog()
    log.precheck_writable()
    result = PlanExecutionResult(
        steps=[
            _step_outcome("forward-failed", [_phase("forward", "mysql -psupersecretpw", _ok(1))]),
            _step_outcome("forward-failed", [_phase("forward", "myhack -psupersecretpw", _ok(1))]),
        ],
        rollbacks=[],
        succeeded=False,
        rollback_complete=True,
    )
    log.write_result(_plan(), result)
    raw = audit_path.read_text()
    # known tool: the glued `-p` password is masked, no full secret survives.
    assert "mysql -psupersecretpw" not in raw
    assert "supe...etpw" in raw
    # unknown tool: still a residual passthrough.
    assert "myhack -psupersecretpw" in raw


# --------------------------------------------------------------------------- #
# dry-run never writes (the audit module is only invoked on real execution)
# --------------------------------------------------------------------------- #


def test_no_write_means_no_file(audit_path: Path) -> None:
    # Constructing the log and never calling write_* leaves no audit.log (a
    # dry-run path never invokes the writer).
    AuditLog()
    assert not audit_path.exists()


# --------------------------------------------------------------------------- #
# unwritable / intent-failure / result-failure timing distinction
# --------------------------------------------------------------------------- #


def test_precheck_writable_raises_on_unwritable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point XDG at a path whose parent is a FILE, so mkdir fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    log = AuditLog()
    with pytest.raises(AuditError) as exc:
        log.precheck_writable()
    assert exc.value.phase == "precheck"


def test_intent_write_failure_phase_is_intent(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hostlens.remediation.audit as audit_mod

    log = AuditLog()
    log.precheck_writable()

    def _boom(self: object, record: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(audit_mod.AuditLog, "_append", _boom)
    with pytest.raises(AuditError) as exc:
        log.write_intent(_plan())
    # phase="intent" -> exec not started, zero side effects, CLI aborts.
    assert exc.value.phase == "intent"


def test_result_write_failure_phase_is_result(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hostlens.remediation.audit as audit_mod

    log = AuditLog()
    log.precheck_writable()

    def _boom(self: object, record: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(audit_mod.AuditLog, "_append", _boom)
    result = PlanExecutionResult(steps=[], rollbacks=[], succeeded=True, rollback_complete=True)
    with pytest.raises(AuditError) as exc:
        log.write_result(_plan(), result)
    # phase="result" -> exec already happened, surface loudly (not silent).
    assert exc.value.phase == "result"
