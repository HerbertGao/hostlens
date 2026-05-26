"""InspectorResult Pydantic model and `Finding` re-export.

`Finding` is the canonical model defined in
`hostlens.reporting.models` (the unified report-data-model SOT). This
module re-exports it as a type alias so existing
`from hostlens.inspectors.result import Finding` import paths keep
working without behaviour change.

`InspectorResult.status` is the M1-final five-value closed set; the
`model_validator` enforces cross-field invariants (`ok` ⇒ no error / no
missing; `requires_unmet` ⇒ non-empty missing; others ⇒ no missing).

`reporting.models.Report.inspector_results: list[InspectorResult]` is
declared with a forward-reference to `InspectorResult` so the two
modules can co-import without a runtime cycle. The
`Report.model_rebuild(...)` call at the bottom of this module resolves
that forward-ref once `InspectorResult` is fully defined — this is the
mechanism that lets either import order (`hostlens.inspectors.result`
first or `hostlens.reporting.models` first) work as long as
`hostlens.inspectors.result` is imported before the first `Report(...)`
construction.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hostlens.reporting.models import Finding as Finding

__all__ = [
    "Finding",
    "InspectorResult",
    "InspectorStatus",
]


InspectorStatus = Literal[
    "ok",
    "timeout",
    "target_unreachable",
    "requires_unmet",
    "exception",
]


class InspectorResult(BaseModel):
    """Result of one Inspector run on one target.

    `status` is the closed five-value enum the M2 Planner Agent expects.
    Cross-field rules:
      - `ok`              ⇒ `error is None` AND `missing == []`
      - `requires_unmet`  ⇒ `missing` non-empty
      - `timeout` / `target_unreachable` / `exception` ⇒ `missing == []`
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    status: InspectorStatus
    target_name: str
    duration_seconds: float
    output: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_status_invariants(self) -> InspectorResult:
        status = self.status

        if status == "ok":
            if self.error is not None:
                raise ValueError(
                    f"ok_status_with_error: status='ok' requires error is None, "
                    f"got error={self.error!r}"
                )
            if self.missing:
                raise ValueError(
                    f"ok_status_with_missing: status='ok' requires missing == [], "
                    f"got missing={self.missing!r}"
                )
        elif status == "requires_unmet":
            if not self.missing:
                raise ValueError(
                    "requires_unmet_status_without_missing: status='requires_unmet' "
                    "requires non-empty missing list"
                )
        elif status in ("timeout", "target_unreachable", "exception"):
            if self.missing:
                raise ValueError(
                    f"{status}_status_with_missing: status={status!r} requires "
                    f"missing == [], got missing={self.missing!r}"
                )

        # Archived inspector-plugin-system spec §需求:`InspectorResult` Pydantic
        # 模型字段集 — `status != "ok"` 时含错误简述 (`error` must be a
        # non-empty string); `status == "ok"` 时必须为 None (already enforced
        # above). `requires_unmet` is exempted because its `missing` list
        # already carries the structured "why" — duplicating it into `error`
        # would be redundant noise; the renderer surfaces `missing` instead.
        if status in ("timeout", "target_unreachable", "exception") and (
            self.error is None or not self.error.strip()
        ):
            raise ValueError(
                f"{status}_status_without_error: status={status!r} requires "
                "a non-empty error description"
            )

        return self


# ---------------------------------------------------------------------------
# Resolve `Report.inspector_results: list[InspectorResult]` forward ref.
# ---------------------------------------------------------------------------
#
# `hostlens.reporting.models.Report` declares its `inspector_results` field
# with a string forward-ref to `InspectorResult` (guarded by TYPE_CHECKING)
# to break the import cycle between this module and `reporting.models`.
# Resolution happens here, once `InspectorResult` is fully defined.
#
# Both arguments are mandatory:
#   * `_types_namespace={"InspectorResult": InspectorResult}` — the
#     TYPE_CHECKING guard hides `InspectorResult` from the module globals
#     `model_rebuild()` would otherwise consult, so a bare call raises
#     `PydanticUndefinedAnnotation`.
#   * `force=True` — Pydantic v2 short-circuits `model_rebuild()` to a
#     no-op if a partial build exists, which would leave the forward-ref
#     silently unresolved.
from hostlens.reporting.models import Report as _Report  # noqa: E402

_Report.model_rebuild(
    _types_namespace={"InspectorResult": InspectorResult},
    force=True,
)
