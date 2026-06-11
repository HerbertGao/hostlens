"""Append-only JSONL audit log for `hostlens fix` (M9 P2).

Every **real** execution writes a **two-phase** record to
`$XDG_DATA_HOME/hostlens/audit.log` (default `~/.local/share/hostlens/`,
**append-only, never rotated, never deleted**, one JSON object per line):

- **intent** (written **before** execution): who / when / target / plan
  identity (finding_id + sha256 of the plan content) / `phase="started"`.
  This leaves a forensic trace ("this plan was attempted") even if the process
  is SIGKILLed mid-execution.
- **result** (written **after** execution): per-step three-state outcomes plus
  rollback outcomes.

`who` is `pwd.getpwuid(os.geteuid()).pw_name` + the numeric uid (never the
spoofable `$USER`); `getpwuid` raising `KeyError` (container arbitrary UID
with no passwd entry) falls back to `str(os.geteuid())` without crashing.

Command strings are passed through `core/redact.py` `redact_text`
(**best-effort**: covers `key=value` / `Bearer` / JWT / `sk-`; does **not**
cover CLI flag-form secrets — a known residual leak surface, see proposal
Security). **dry-run never writes** this log.

Write failures are never silent and distinguish timing: the directory is
pre-checked for writability; an **intent** write failure (exec not started →
zero side effects) aborts execution; a **result** write failure (exec already
completed → side effects happened) is surfaced loudly. The CLI maps these to
non-zero exits.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hostlens.core.redact import redact_text
from hostlens.remediation.executor import PhaseOutcome, PlanExecutionResult, StepOutcome
from hostlens.remediation.models import RemediationPlan

__all__ = ["AuditError", "AuditLog", "default_audit_path", "resolve_actor"]


class AuditError(Exception):
    """Raised when the audit log cannot be written.

    `phase` distinguishes timing for the CLI: `"precheck"` / `"intent"` mean
    *nothing was executed* (zero side effects, abort), while `"result"` means
    *execution already completed* (side effects happened, surface loudly).
    """

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message


def default_audit_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "hostlens" / "audit.log"


def resolve_actor() -> str:
    """`<pw_name>(<uid>)`, falling back to `<uid>` if there is no passwd entry
    (container arbitrary UID). Never reads `$USER` (spoofable)."""
    uid = os.geteuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)
    return f"{name}({uid})"


def _plan_hash(plan: RemediationPlan) -> str:
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _redact_cmd(cmd: str | None) -> str | None:
    return None if cmd is None else redact_text(cmd)


class AuditLog:
    """Append-only two-phase audit writer.

    `path` defaults to `default_audit_path()`. Construct once per `fix`
    invocation, call `precheck_writable()` before execution, then
    `write_intent(plan)` (before exec) and `write_result(plan, result)`
    (after exec).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else default_audit_path()

    @property
    def path(self) -> Path:
        return self._path

    def precheck_writable(self) -> None:
        """Ensure the audit directory exists and is writable. Raises
        `AuditError(phase="precheck")` on failure — an execution-front safety
        gate (zero side effects)."""
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AuditError(
                "precheck", f"audit directory not creatable: {directory} ({exc.strerror})"
            ) from exc
        if not os.access(directory, os.W_OK):
            raise AuditError("precheck", f"audit directory not writable: {directory}")

    def write_intent(self, plan: RemediationPlan) -> None:
        """Append the intent record (before execution). Raises
        `AuditError(phase="intent")` on write failure — caller must abort
        execution (zero side effects so far)."""
        record = {
            "type": "intent",
            "phase": "started",
            "who": resolve_actor(),
            "when": _now_iso(),
            "target_name": plan.target_name,
            "finding_id": plan.finding_id,
            "plan_sha256": _plan_hash(plan),
        }
        try:
            self._append(record)
        except OSError as exc:
            raise AuditError("intent", f"failed to write audit intent record: {exc}") from exc

    def write_result(self, plan: RemediationPlan, result: PlanExecutionResult) -> None:
        """Append the result record (after execution). Raises
        `AuditError(phase="result")` on write failure — side effects already
        happened; caller must surface loudly (not silently swallow)."""
        record = {
            "type": "result",
            "who": resolve_actor(),
            "when": _now_iso(),
            "target_name": plan.target_name,
            "finding_id": plan.finding_id,
            "plan_sha256": _plan_hash(plan),
            "succeeded": result.succeeded,
            "rollback_complete": result.rollback_complete,
            "steps": [_step_record(step) for step in result.steps],
            "rollbacks": [
                {
                    "index": rb.index,
                    "description": rb.description,
                    "status": rb.status,
                    **(
                        {}
                        if rb.phase is None
                        else {"cmd": _redact_cmd(rb.phase.cmd), **_exec_fields(rb.phase)}
                    ),
                }
                for rb in result.rollbacks
            ],
        }
        try:
            self._append(record)
        except OSError as exc:
            raise AuditError("result", f"failed to write audit result record: {exc}") from exc

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _step_record(step: StepOutcome) -> dict[str, Any]:
    return {
        "index": step.index,
        "description": step.description,
        "risk_level": step.risk_level,
        "status": step.status,
        "phases": [
            {"phase": ph.phase, "cmd": _redact_cmd(ph.cmd), **_exec_fields(ph)}
            for ph in step.phases
        ],
    }


def _exec_fields(phase: PhaseOutcome) -> dict[str, Any]:
    """Flatten a `PhaseOutcome` into audit fields. `exit_code` may be null;
    `timed_out` lets a consumer mechanically distinguish timeout
    (`timed_out: true`) from dropped connection (`exit_code: null,
    timed_out: false`). `transport_error` is set iff exec raised."""
    result = phase.result
    if result is None:
        return {
            "exit_code": None,
            "timed_out": False,
            "transport_error": (
                redact_text(phase.transport_error) if phase.transport_error is not None else None
            ),
        }
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "transport_error": None,
    }
