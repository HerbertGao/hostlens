"""Pydantic schema for `hostlens doctor --json` output.

The shape defined here is the **stable contract** documented in
`openspec/changes/bootstrap-project-skeleton/specs/cli-foundation/spec.md`
and `design.md` D-9.

Schema evolution policy (D-9):
- Required fields (top-level `version` / `timestamp` / `checks` / `ready`;
  each check's `status`) are snapshot-locked. Any change is breaking and
  MUST bump `DoctorReport.version`.
- Optional fields (`detail` / `path` / additional metadata) may be added
  without bumping `version`. Deletion or semantic change is breaking.

`extra="forbid"` rejects undeclared fields so accidental drift in producers
fails loudly instead of leaking through into the JSON contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
]


CheckStatus = Literal["ok", "present", "missing", "unreadable", "error"]
"""Enum of valid `status` values for any check.

- `ok`         : health-style check passed (e.g. python_version, config_dir)
- `present`    : existence-style check found the resource (e.g. env var set)
- `missing`    : existence-style check did NOT find the resource
- `unreadable` : resource exists but cannot be accessed (e.g. perms)
- `error`      : unexpected failure inside the checker itself
"""


class CheckResult(BaseModel):
    """Single check entry inside `DoctorReport.checks`."""

    model_config = ConfigDict(extra="forbid")

    status: CheckStatus
    detail: str | None = None
    path: str | None = None


class DoctorReport(BaseModel):
    """Top-level JSON contract emitted by `hostlens doctor --json`."""

    model_config = ConfigDict(extra="forbid")

    version: str = "0.1.0"
    timestamp: datetime
    checks: dict[str, CheckResult]
    ready: bool
