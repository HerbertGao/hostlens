"""End-to-end contract checks for ``hostlens fix`` (M9 P2, group C).

Maps to spec §需求:`hostlens fix` 必须默认 dry-run、拒绝 root、解析 target、稳健处理输入
错误 + §需求:dry-run 与真实执行必须共享同一编排. Drives the full ``hostlens.cli.main``
pipeline (so the UsageError 2→3 remap and the project exit-code ladder are
exercised honestly), with a real tmp ``targets.yaml`` (local target) and a tmp
``XDG_DATA_HOME`` audit dir. Each safety gate really fires:

- EUID==0 refusal is the earliest gate (mock ``os.geteuid`` → 0; assert exit 1
  AND that no plan step command reached stdout/stderr).
- default dry-run executes nothing and writes no audit (assert exec spy
  uncalled + audit.log absent).
- ``--dry-run --yes`` → dry-run wins (zero exec, zero audit).
- ``--yes`` real execution runs commands for real and writes two-phase audit.
- exit-2 (illegal plan / file IO) and exit-3 (target unresolved / corrupt
  targets.yaml / unreadable) paths, none leaking a traceback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from hostlens.cli import fix as fix_module
from hostlens.cli import main

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "targets.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


@pytest.fixture
def audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    return home / "hostlens" / "audit.log"


@pytest.fixture
def nonroot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force EUID != 0 so the root gate never fires by accident in CI."""
    monkeypatch.setattr(fix_module.os, "geteuid", lambda: 1000)


def _write_plan(
    dir_path: Path,
    *,
    target_name: str = "local",
    forward: str = "true",
    verify: str = "true",
    rollback: str | None = "true",
    risk_level: str = "low",
    precheck: str | None = None,
) -> Path:
    step: dict[str, object] = {
        "description": "noop step",
        "precheck_cmd": precheck,
        "forward_cmd": forward,
        "rollback_cmd": rollback,
        "verify_cmd": verify,
        "risk_level": risk_level,
    }
    plan = {
        "finding_id": "disk-full",
        "target_name": target_name,
        "rationale": "r",
        "steps": [step],
        "estimated_duration_seconds": 1,
    }
    path = dir_path / "plan.json"
    path.write_text(json.dumps(plan))
    return path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
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
# EUID==0 — earliest gate, no plan-content leak
# --------------------------------------------------------------------------- #


def test_root_refused_before_load_or_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
) -> None:
    plan = _write_plan(tmp_path, forward="SECRET_FORWARD_CMD", verify="SECRET_VERIFY_CMD")
    monkeypatch.setattr(fix_module.os, "geteuid", lambda: 0)

    code, out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 1
    # Earliest gate: no plan step command may have been rendered anywhere.
    assert "SECRET_FORWARD_CMD" not in out and "SECRET_FORWARD_CMD" not in err
    assert "SECRET_VERIFY_CMD" not in out and "SECRET_VERIFY_CMD" not in err
    assert "approval-rejected:" in err
    assert "root" in err.lower()
    assert not audit_home.exists()


# --------------------------------------------------------------------------- #
# Default dry-run — no exec, no audit
# --------------------------------------------------------------------------- #


def test_explicit_dry_run_previews_without_exec_or_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    plan = _write_plan(tmp_path, forward="echo would-run")
    code, out, _err = _run_main(["fix", str(plan), "--dry-run"], capsys, monkeypatch)
    assert code == 0
    assert "dry-run" in out
    # Preview shows the command sequence...
    assert "echo would-run" in out
    # ...but nothing was executed and no audit was written.
    assert "no audit record was written" in out
    assert not audit_home.exists()


def test_dry_run_yes_together_dry_run_wins(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # A side-effecting command that would create a file if really executed.
    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, forward=f"touch {flag}", verify=f"test -f {flag}")
    code, _out, _err = _run_main(["fix", str(plan), "--dry-run", "--yes"], capsys, monkeypatch)
    assert code == 0
    # dry-run wins: zero execution (flag never created), zero audit.
    assert not flag.exists()
    assert not audit_home.exists()


def test_default_no_flags_non_tty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # No --dry-run, no --yes, non-TTY: the flag model means "no execution
    # signal" -> the preview still renders (the operator must see what would
    # run) but the approval gate refuses (exit 1) and no audit is written.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    plan = _write_plan(tmp_path, forward="echo would-run")
    code, out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 1
    # Preview was shown before the gate refused.
    assert "echo would-run" in out
    # Refused by the safety gate (no signal -> no execution).
    assert "approval-rejected:" in err
    assert "non_interactive_no_yes" in err
    # Default-safe: nothing executed, no audit record.
    assert not audit_home.exists()


# --------------------------------------------------------------------------- #
# 6.7 dry-run shares orchestration: ExecutionTarget.exec zero-called
# --------------------------------------------------------------------------- #


def test_dry_run_never_calls_target_exec(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    calls: list[str] = []

    async def _spy_exec(self: object, cmd: str, *, timeout: int, env: object = None) -> object:
        calls.append(cmd)
        raise AssertionError("ExecutionTarget.exec must not be called in dry-run")

    from hostlens.targets.local import LocalTarget

    monkeypatch.setattr(LocalTarget, "exec", _spy_exec)
    plan = _write_plan(tmp_path, forward="echo x")
    code, _out, _err = _run_main(["fix", str(plan), "--dry-run"], capsys, monkeypatch)
    assert code == 0
    assert calls == []  # exec never invoked
    assert not audit_home.exists()  # audit never touched


# --------------------------------------------------------------------------- #
# Real execution via --yes (non-TTY)
# --------------------------------------------------------------------------- #


def test_yes_real_execution_runs_and_writes_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, forward=f"touch {flag}", verify=f"test -f {flag}")
    code, out, _err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 0
    assert flag.exists()  # real side effect
    assert "succeeded=True" in out
    # Two-phase audit written.
    lines = [json.loads(line) for line in audit_home.read_text().splitlines() if line.strip()]
    assert [line["type"] for line in lines] == ["intent", "result"]
    assert lines[1]["succeeded"] is True


def test_non_tty_without_yes_rejected_exit_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # capsys makes stdin non-interactive; without --yes the gate refuses.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    plan = _write_plan(tmp_path, forward="touch should-not-exist")
    code, _out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 1
    assert "approval-rejected:" in err
    assert "non_interactive_no_yes" in err
    assert not audit_home.exists()


# --------------------------------------------------------------------------- #
# Risk-tiered divergence — medium/high plans are propose-only (runbook, exit 4)
# --------------------------------------------------------------------------- #


def test_high_risk_renders_runbook_exit_4_no_exec_no_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # A high-risk plan is propose-only: even with --yes it renders a runbook and
    # exits 4 — never executes, never writes audit, no double-confirm path.
    flag = tmp_path / "applied"
    plan = _write_plan(
        tmp_path,
        risk_level="high",
        precheck="true",
        rollback=None,
        forward=f"touch {flag}",
    )
    code, out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 4
    assert "Remediation Runbook" in out
    assert "本工具未执行任何命令" in out
    assert "not-executed:" in err
    assert not flag.exists()  # zero execution
    assert not audit_home.exists()  # zero audit


def test_medium_risk_renders_runbook_exit_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, risk_level="medium", forward=f"touch {flag}", rollback="true")
    code, out, _err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 4
    assert "risk=medium" in out
    assert not flag.exists()
    assert not audit_home.exists()


def test_medium_dry_run_is_noop_still_runbook(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # --dry-run is a no-op for elevated plans: they never execute regardless, so
    # the divergence (exit 4 + runbook) wins over the dry-run branch.
    plan = _write_plan(tmp_path, risk_level="medium", forward="echo x", rollback="true")
    code, out, _err = _run_main(["fix", str(plan), "--dry-run"], capsys, monkeypatch)
    assert code == 4
    assert "Remediation Runbook" in out
    assert "dry-run complete" not in out  # the all-low dry-run path was never taken


def test_elevated_runbook_written_to_out_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    out_file = tmp_path / "runbook.md"
    plan = _write_plan(tmp_path, risk_level="medium", forward="echo x", rollback="true")
    code, out, _err = _run_main(["fix", str(plan), "--out", str(out_file)], capsys, monkeypatch)
    assert code == 4
    assert out_file.exists()
    assert "Remediation Runbook" in out_file.read_text()
    assert f"written to {out_file}" in out


def test_high_risk_root_refused_before_runbook_render(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
) -> None:
    # EUID==0 is the earliest gate — it must fire before the runbook (which
    # renders plan commands) for an elevated plan too.
    plan = _write_plan(
        tmp_path,
        risk_level="high",
        precheck="true",
        rollback=None,
        forward="SECRET_FORWARD_CMD",
    )
    monkeypatch.setattr(fix_module.os, "geteuid", lambda: 0)
    code, out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 1
    assert "SECRET_FORWARD_CMD" not in out and "SECRET_FORWARD_CMD" not in err
    assert "root" in err.lower()


def test_execution_failure_prefixed_distinct_from_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # forward succeeds, verify fails -> execution-failed (NOT approval-rejected).
    plan = _write_plan(tmp_path, forward="true", verify="false", rollback="true")
    code, _out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 1
    assert "execution-failed:" in err
    assert "approval-rejected:" not in err
    # Audit still written (real attempt happened).
    assert audit_home.exists()


def test_rollback_incomplete_reported_execution_failed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    # forward succeeds, verify fails (verify-failed includes self) -> rollback
    # runs; rollback `false` exits non-zero -> rollback-failed ->
    # rollback_complete=False. The CLI must surface exit 1 with the
    # execution-failed prefix AND the rollback-incomplete hint.
    plan = _write_plan(tmp_path, forward="true", verify="false", rollback="false")
    code, _out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 1
    assert "execution-failed:" in err
    assert "rollback incomplete" in err
    # Real attempt happened -> audit written.
    assert audit_home.exists()


def test_audit_unwritable_precheck_aborts_before_exec(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    nonroot: None,
) -> None:
    # XDG points at a path whose parent is a FILE → audit mkdir fails →
    # precheck_writable raises → CLI aborts before any exec (exit 1).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, forward=f"touch {flag}", verify=f"test -f {flag}")
    code, _out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 1
    assert "audit-precheck:" in err
    # Execution-front gate: no side effect occurred.
    assert not flag.exists()
    assert "Traceback" not in err


def test_intent_write_failure_aborts_with_zero_side_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    import hostlens.remediation.audit as audit_mod

    def _boom(self: object, record: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(audit_mod.AuditLog, "_append", _boom)
    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, forward=f"touch {flag}", verify=f"test -f {flag}")
    code, _out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 1
    assert "audit-intent:" in err
    assert "no command was executed" in err
    # Intent failed before exec → zero side effects.
    assert not flag.exists()


def test_result_write_failure_surfaces_side_effects_occurred(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    import hostlens.remediation.audit as audit_mod

    flag = tmp_path / "applied"
    plan = _write_plan(tmp_path, forward=f"touch {flag}", verify=f"test -f {flag}")

    original_append = audit_mod.AuditLog._append
    state = {"calls": 0}

    def _fail_second(self: object, record: object) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            return original_append(self, record)  # type: ignore[arg-type]
        raise OSError("disk full")  # result write fails

    monkeypatch.setattr(audit_mod.AuditLog, "_append", _fail_second)
    code, _out, err = _run_main(["fix", str(plan), "--yes"], capsys, monkeypatch)
    assert code == 1
    assert "audit-result:" in err
    assert "side effects HAVE OCCURRED" in err
    # Side effects really happened (forward ran before the result write failed).
    assert flag.exists()


# --------------------------------------------------------------------------- #
# Exit 2 — illegal plan / file IO
# --------------------------------------------------------------------------- #


def test_missing_plan_file_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    code, out, err = _run_main(["fix", str(tmp_path / "nope.json")], capsys, monkeypatch)
    assert code == 2
    assert "invalid plan:" in err
    assert "Traceback" not in err and "Traceback" not in out


def test_malformed_json_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    path = tmp_path / "plan.json"
    path.write_text("{not valid json")
    code, _out, err = _run_main(["fix", str(path)], capsys, monkeypatch)
    assert code == 2
    assert "invalid plan:" in err
    assert "Traceback" not in err


def test_duplicate_key_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    path = tmp_path / "plan.json"
    # Two finding_id keys — load_json rejects duplicate keys.
    path.write_text(
        '{"finding_id":"a","finding_id":"b","target_name":"local","rationale":"r",'
        '"estimated_duration_seconds":1,"steps":[{"description":"d","precheck_cmd":null,'
        '"forward_cmd":"true","rollback_cmd":"true","verify_cmd":"true","risk_level":"low"}]}'
    )
    code, _out, err = _run_main(["fix", str(path)], capsys, monkeypatch)
    assert code == 2
    assert "Traceback" not in err


def test_schema_violation_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    path = tmp_path / "plan.json"
    # high-risk with no precheck violates the P1a invariant.
    path.write_text(
        json.dumps(
            {
                "finding_id": "f",
                "target_name": "local",
                "rationale": "r",
                "estimated_duration_seconds": 1,
                "steps": [
                    {
                        "description": "d",
                        "precheck_cmd": None,
                        "forward_cmd": "true",
                        "rollback_cmd": None,
                        "verify_cmd": "true",
                        "risk_level": "high",
                    }
                ],
            }
        )
    )
    code, _out, err = _run_main(["fix", str(path)], capsys, monkeypatch)
    assert code == 2
    assert "schema" in err.lower()
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# Exit 3 — target / config resolution
# --------------------------------------------------------------------------- #


def test_target_not_registered_exit_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    targets_yaml: Path,
    audit_home: Path,
    nonroot: None,
) -> None:
    plan = _write_plan(tmp_path, target_name="ghost-host")
    code, _out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 3
    assert "target not found: ghost-host" in err
    assert "Traceback" not in err


def test_corrupt_targets_yaml_schema_exit_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    audit_home: Path,
    nonroot: None,
) -> None:
    # Unknown target type -> ValidationError from build_registry_from_config.
    bad = tmp_path / "targets.yaml"
    bad.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local", "type": "bogus-type"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(bad))
    plan = _write_plan(tmp_path, target_name="local")
    code, _out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 3
    assert "Traceback" not in err


def test_targets_yaml_is_a_directory_exit_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    audit_home: Path,
    nonroot: None,
) -> None:
    # A directory in place of targets.yaml -> OSError (IsADirectoryError) on
    # read_text, which the catch contract maps to exit 3.
    bad_dir = tmp_path / "targets.yaml"
    bad_dir.mkdir()
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(bad_dir))
    plan = _write_plan(tmp_path, target_name="local")
    code, _out, err = _run_main(["fix", str(plan)], capsys, monkeypatch)
    assert code == 3
    assert "Traceback" not in err
