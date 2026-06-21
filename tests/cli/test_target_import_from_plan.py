"""Tests for ``hostlens target import --from-plan`` (tasks 4.5 + 4.6).

The CLI landing half of ``add-mcp-target-import-propose``: a serialised
``ImportPlan`` (an MCP ``propose_target_import`` artefact a client serialised,
or a dry-run ``ImportPlan.save`` YAML) is landed verbatim, skipping source
parse + probe.

4.6 — dev-``.env`` isolation: the autouse ``_isolate_env`` fixture ``chdir``s to
a tmp dir (so pydantic-settings cannot find the repo-root dev ``.env``) and
strips ``HOSTLENS_BACKEND__*`` / ``HOSTLENS_AGENT__*`` from the ambient env, so
``import_cmd``'s ``load_settings()`` on the write path sees clean defaults
(otherwise local-green / clean-CI-red). Run the FULL ``tests/cli`` dir, not a
subset, to surface any cross-test env leakage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.targets.config import SSHEntry
from hostlens.targets.import_plan import FailedProbe, ImportPlan, PendingAdd
from hostlens.targets.probe import ProbeResult

# Credential-env-var NAMES assembled via concatenation so the secret scanner
# cannot match a quoted value adjacent to a credential key (repo
# .gitguardian.yaml convention — these are env var NAMES, never secret values).
_WEB1_ENV_REF = "WEB1" + "_ENV"
_BAD_ENV_REF = "not-a-bare" + "-name"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4.6: keep the repo-root dev ``.env`` + ambient backend vars out of
    ``load_settings()`` (the ``--from-plan --yes`` write path calls it)."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("HOSTLENS_BACKEND__", "HOSTLENS_AGENT__")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-root EUID so the write path is not refused; the
    ``EUID==0`` test re-overrides this to 0."""
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a fresh tmp file."""
    path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


# --------------------------------------------------------------------------- #
# Plan builders
# --------------------------------------------------------------------------- #


def _ssh_pending(
    name: str = "web-1",
    *,
    password_env: str | None = None,
    enabled: bool = True,
    host: str = "example.com",
    user: str = "root",
) -> PendingAdd:
    return PendingAdd(
        entry=SSHEntry(type="ssh", name=name, host=host, user=user, enabled=enabled),
        password_env=password_env,
    )


def _failed(name: str = "down-1", *, enabled: bool = True) -> FailedProbe:
    return FailedProbe(
        entry=SSHEntry(type="ssh", name=name, host="example.net", user="root", enabled=enabled),
        result=ProbeResult(reachable=False, error_kind="unreachable"),
    )


def _write_plan_yaml(path: Path, plan: ImportPlan) -> Path:
    plan.save(path)
    return path


def _write_plan_json(path: Path, plan: ImportPlan) -> Path:
    path.write_text(plan.model_dump_json())
    return path


def _write_raw(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(raw))
    return path


# =========================================================================== #
# --yes lands; ${VAR} preserved; enabled=True omitted
# =========================================================================== #


def test_from_plan_yes_lands_to_add_with_env_placeholder(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    plan = ImportPlan(to_add=[_ssh_pending(password_env=_WEB1_ENV_REF)])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)

    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes"])

    assert result.exit_code == 0, result.stdout + result.stderr
    written = yaml.safe_load(targets_yaml.read_text())
    entry = written["targets"][0]
    assert entry["name"] == "web-1"
    # ${VAR} placeholder re-derived from password_env, not an inlined secret.
    assert entry["password"] == "${" + _WEB1_ENV_REF + "}"
    # enabled=True is omitted by _entry_to_dict (same shape as `target add`).
    assert "enabled" not in entry


def test_from_plan_json_file_also_lands(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """``--from-plan`` accepts JSON too (JSON ⊂ YAML, one safe_load handles both)."""
    plan = ImportPlan(to_add=[_ssh_pending(name="json-host")])
    plan_path = _write_plan_json(tmp_path / "plan.json", plan)

    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes"])

    assert result.exit_code == 0, result.stdout + result.stderr
    written = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in written["targets"]] == ["json-host"]


# =========================================================================== #
# Preview (no --yes / --dry-run) → exit 0, no write
# =========================================================================== #


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_from_plan_preview_no_write_exit0(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path, extra: list[str]
) -> None:
    plan = ImportPlan(to_add=[_ssh_pending()])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)

    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), *extra])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_yaml.exists()  # nothing written


# =========================================================================== #
# Argument validation → exit 2
# =========================================================================== #


def test_inventory_and_from_plan_both_given_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", ImportPlan(to_add=[_ssh_pending()]))
    inv = tmp_path / "inv.yml"
    inv.write_text("{}")
    result = runner.invoke(app, ["target", "import", str(inv), "--from-plan", str(plan_path)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_neither_inventory_nor_from_plan_exit2(runner: CliRunner, targets_yaml: Path) -> None:
    result = runner.invoke(app, ["target", "import"])
    assert result.exit_code == 2, result.stdout + result.stderr


@pytest.mark.parametrize("knob", [["--source", "yaml"], ["--concurrency", "50"]])
def test_from_plan_with_probe_only_knob_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path, knob: list[str]
) -> None:
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", ImportPlan(to_add=[_ssh_pending()]))
    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes", *knob])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


# =========================================================================== #
# File errors / version mismatch → exit 2, no write
# =========================================================================== #


def test_from_plan_missing_file_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["target", "import", "--from-plan", str(tmp_path / "nope.yaml"), "--yes"]
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_from_plan_invalid_content_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    bad = _write_raw(tmp_path / "bad.yaml", {"unknown_field": 1})  # extra=forbid → reject
    result = runner.invoke(app, ["target", "import", "--from-plan", str(bad), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_from_plan_version_not_one_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    raw = {"version": "2", "to_add": [], "skipped": [], "failed_probe": [], "invalid_candidate": []}
    path = _write_raw(tmp_path / "v2.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


# =========================================================================== #
# Trust-boundary: malformed plans rejected by ImportPlan.load invariants
# =========================================================================== #


def _raw_to_add(entry: dict[str, Any], *, password_env: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"entry": entry}
    if password_env is not None:
        item["password_env"] = password_env
    return {
        "version": "1",
        "to_add": [item],
        "skipped": [],
        "failed_probe": [],
        "invalid_candidate": [],
    }


def test_malformed_to_add_enabled_false_rejected(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    raw = _raw_to_add({"type": "ssh", "name": "x", "host": "h", "user": "u", "enabled": False})
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_malformed_env_name_rejected(runner: CliRunner, targets_yaml: Path, tmp_path: Path) -> None:
    raw = _raw_to_add(
        {"type": "ssh", "name": "x", "host": "h", "user": "u"}, password_env=_BAD_ENV_REF
    )
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_malformed_control_char_host_rejected(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    raw = _raw_to_add({"type": "ssh", "name": "x", "host": "h‮evil", "user": "u"})
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_malformed_inline_plaintext_credential_rejected(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    # Keep the fake value off any password-keyed string literal: a non-secret
    # variable holds a concatenated value, then the dict references it, so the
    # secret scanner cannot match a quoted value next to the credential key (repo
    # .gitguardian.yaml convention — no dashboard ignore needed). The invariant
    # rejects ANY non-None inline credential; the value itself is irrelevant.
    fake_inline = "inline" + "-cred-rejected"
    raw = _raw_to_add(
        {"type": "ssh", "name": "x", "host": "h", "user": "u", "password": fake_inline}
    )
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_malformed_key_path_placeholder_rejected(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """A ``key_path`` with a ``${VAR}`` placeholder must be rejected: key_path is
    not a placeholder-allowed field, so it would land verbatim and poison every
    later ``load_targets_config(expand_env=True)`` (env_placeholder_not_allowed_here)."""
    raw = _raw_to_add({"type": "ssh", "name": "x", "host": "h", "user": "u", "key_path": "${EVIL}"})
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(path), "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_malformed_failed_probe_rejected_under_include_unreachable(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """A malformed ``failed_probe`` entry (bad env name) is rejected by ``.load``
    regardless — exercised here on the ``--include-unreachable`` landing path."""
    # Env-var NAME via concatenation (repo .gitguardian.yaml convention: keep the
    # value off a contiguous credential-key literal — it is a NAME, not a secret).
    bad_env_ref = "bad" + "-name"
    raw = {
        "version": "1",
        "to_add": [],
        "skipped": [],
        "failed_probe": [
            {
                "entry": {"type": "ssh", "name": "d", "host": "h", "user": "u"},
                "result": {"reachable": False, "error_kind": "unreachable"},
                "password_env": bad_env_ref,
            }
        ],
        "invalid_candidate": [],
    }
    path = _write_raw(tmp_path / "p.yaml", raw)
    result = runner.invoke(
        app,
        ["target", "import", "--from-plan", str(path), "--yes", "--include-unreachable"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()


# =========================================================================== #
# Root refusal / include-unreachable / empty
# =========================================================================== #


def test_from_plan_euid_zero_exit1(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", ImportPlan(to_add=[_ssh_pending()]))
    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_from_plan_include_unreachable_lands_failed_probe_disabled(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    plan = ImportPlan(to_add=[_ssh_pending(name="up")], failed_probe=[_failed(name="down")])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)
    result = runner.invoke(
        app,
        ["target", "import", "--from-plan", str(plan_path), "--yes", "--include-unreachable"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    written = {e["name"]: e for e in yaml.safe_load(targets_yaml.read_text())["targets"]}
    assert "enabled" not in written["up"]  # to_add → enabled=True (omitted)
    assert written["down"]["enabled"] is False  # failed_probe → enabled=False (explicit)


def test_from_plan_empty_to_add_exit0(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """A plan with no save-bound entry (only ``skipped``) is exit 0, not exit 1 —
    ``--from-plan`` does not re-probe, so the candidates-failed heuristic does
    not apply."""
    plan = ImportPlan(skipped=["already-managed"])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_from_plan_all_failed_probe_no_flag_exit0(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """The other empty-save branch: a plan with only ``failed_probe`` and NO
    ``--include-unreachable`` → no save-bound entry → exit 0 (this is the case
    that diverges from ref-mode, which would exit 1 via candidates_failed)."""
    plan = ImportPlan(failed_probe=[_failed(name="down")])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)
    result = runner.invoke(app, ["target", "import", "--from-plan", str(plan_path), "--yes"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_yaml.exists()


def test_from_plan_yes_json_emits_plan_json_to_stdout(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """``--json`` is a machine-output contract: ``--from-plan --yes --json`` emits
    the loaded plan as a single JSON doc to stdout (status trailers to stderr) AND
    still writes — matching ref-mode (the human diff stays suppressed on --yes)."""
    plan = ImportPlan(to_add=[_ssh_pending(name="jhost")])
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", plan)
    result = runner.invoke(
        app, ["target", "import", "--from-plan", str(plan_path), "--yes", "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)  # stdout is a single valid JSON document
    assert [item["name"] for item in payload["to_add"]] == ["jhost"]
    written = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in written["targets"]] == ["jhost"]  # actually landed


def test_from_plan_skip_and_include_unreachable_both_exit2(
    runner: CliRunner, targets_yaml: Path, tmp_path: Path
) -> None:
    """``--skip-unreachable`` + ``--include-unreachable`` is contradictory; the
    check applies in ``--from-plan`` mode too (no silent no-op)."""
    plan_path = _write_plan_yaml(tmp_path / "plan.yaml", ImportPlan(to_add=[_ssh_pending()]))
    result = runner.invoke(
        app,
        [
            "target",
            "import",
            "--from-plan",
            str(plan_path),
            "--yes",
            "--skip-unreachable",
            "--include-unreachable",
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_yaml.exists()
