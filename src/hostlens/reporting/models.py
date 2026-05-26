"""Report data model SOT — `Severity` / `Evidence` / `Finding` / `Report`.

All four are Pydantic v2 frozen models with `extra="forbid"`. They are
the unified contract consumed by:

- M1 `hostlens inspect` CLI (renders markdown / json reports)
- M2 Planner Agent (aggregates multi-inspector runs into one Report)
- M3 Diagnostician (extends Finding / Report with id / fingerprint, add-only)
- M5 Notifier adapters (consume `Report` to produce channel payloads)

Circular-import note: `Report.inspector_results` references
`hostlens.inspectors.result.InspectorResult`; meanwhile
`inspectors.result.Finding` is a type-alias re-export of the `Finding`
defined here. The cycle is broken by:

1. `from __future__ import annotations` (PEP 563 deferred evaluation).
2. `InspectorResult` imported only under `TYPE_CHECKING`.
3. `Report.inspector_results` typed via a forward-ref string.
4. `inspectors/result.py` finalises the forward-ref by calling
   `Report.model_rebuild(_types_namespace={"InspectorResult": ...},
   force=True)` at the bottom of its own module load (handled by the
   group implementing inspectors-side changes; this module deliberately
   does not call `model_rebuild` itself to keep `__init__` side-effect free).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if TYPE_CHECKING:
    from hostlens.inspectors.result import InspectorResult

__all__ = [
    "Evidence",
    "Finding",
    "Report",
    "Severity",
    "Tag",
]


Severity = Literal["info", "warning", "critical"]
"""Closed three-value severity ladder used by both Finding and the
finding DSL in inspector manifests. Extra values (`debug`, `error`,
`fatal`) are deliberately rejected — extension must be add-only via a
follow-up OpenSpec proposal."""


Tag = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
"""Tag string constraint shared by `Finding.tags` (and any future
producer of routing tags). Matches the spec §需求:`Finding` Pydantic
模型必须严格四字段 contract — every tag is lowercase ASCII, starts with
a letter, and may contain digits / underscores / hyphens. Empty strings
and uppercase letters are rejected so Notifier `only_if` expressions
can rely on stable token shape."""


_EVIDENCE_KIND_RULES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # kind -> (required-non-None fields, forbidden fields that must be None)
    "command_output": (
        frozenset({"command", "stdout"}),
        frozenset({"path", "excerpt", "metric_name", "metric_value", "data"}),
    ),
    "file_excerpt": (
        frozenset({"path", "excerpt"}),
        frozenset(
            {
                "command",
                "stdout",
                "stderr",
                "exit_code",
                "metric_name",
                "metric_value",
                "data",
            }
        ),
    ),
    "metric": (
        frozenset({"metric_name", "metric_value"}),
        frozenset(
            {
                "command",
                "stdout",
                "stderr",
                "exit_code",
                "path",
                "excerpt",
                "data",
            }
        ),
    ),
    "structured": (
        frozenset({"data"}),
        frozenset(
            {
                "command",
                "stdout",
                "stderr",
                "exit_code",
                "path",
                "excerpt",
                "metric_name",
                "metric_value",
            }
        ),
    ),
}


class Evidence(BaseModel):
    """A single structured piece of evidence attached to a `Finding`.

    The model is intentionally flat (single Pydantic class, not a
    discriminated union) — see `design.md` §决策 2 for why. Field
    membership is enforced via a `model_validator(mode="after")` that
    consults `_EVIDENCE_KIND_RULES`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["command_output", "file_excerpt", "metric", "structured"]
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    path: str | None = None
    excerpt: str | None = None
    metric_name: str | None = None
    metric_value: float | str | None = None
    data: dict[str, Any] | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def _validate_kind_field_set(self) -> Self:
        required, forbidden = _EVIDENCE_KIND_RULES[self.kind]

        for field_name in required:
            if getattr(self, field_name) is None:
                raise ValueError(f'kind="{self.kind}" requires {field_name} (got None)')

        for field_name in forbidden:
            if getattr(self, field_name) is not None:
                raise ValueError(
                    f'kind="{self.kind}" forbids {field_name} (got {getattr(self, field_name)!r})'
                )

        return self


class Finding(BaseModel):
    """Inspector finding — the SOT used across reporting, inspectors,
    and tool schemas.

    Field set is intentionally minimal (four fields). M3 will extend with
    identity fields (`id`, `fingerprint`, etc.) as add-only — these four
    field names and types are stable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity
    message: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)


class Report(BaseModel):
    """Container aggregating one or more `InspectorResult`s into a
    user-facing report.

    Construction via `Report.from_inspector_results(...)` is preferred —
    it auto-generates `report_id`, locks `schema_version="1.0"`, and
    flattens findings. Direct `Report(**kwargs)` construction is allowed
    (Pydantic idiom) but the caller must populate all required fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    schema_version: Literal["1.0"]
    intent: str | None = None
    target_name: str = Field(min_length=1)
    inspector_results: list[InspectorResult] = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        return self

    def total_evidence_bytes(self) -> int:
        """Total UTF-8 byte count of all string-valued evidence fields.

        Iterates every `Evidence` attached to every `Finding` on every
        `InspectorResult` and sums `len(value.encode("utf-8"))` for the
        text-shaped attributes (`command`, `stdout`, `stderr`, `excerpt`,
        `path`, `metric_name`). Non-string attributes (`exit_code` /
        float `metric_value` / `data` dict / `truncated`) are skipped —
        the `data` dict can in principle hold large strings recursively
        but pricing them in is intentionally deferred (see
        `docs/operations/inspect.md` Known accepted risks: large reports
        warn but do not fail).

        CLI / CI sinks call this to decide whether to emit the
        `>8 MiB` warning documented in §Failure Modes of the
        report-data-model OpenSpec proposal; downstream notifier
        adapters may use it for the same purpose. Returning an `int`
        keeps the threshold comparison cheap and avoids leaking the
        threshold constant into the model layer.
        """
        total = 0
        for ir in self.inspector_results:
            for finding in ir.findings:
                for evidence in finding.evidence:
                    for attr in (
                        "command",
                        "stdout",
                        "stderr",
                        "excerpt",
                        "path",
                        "metric_name",
                    ):
                        val = getattr(evidence, attr, None)
                        if isinstance(val, str):
                            total += len(val.encode("utf-8"))
                    if isinstance(evidence.metric_value, str):
                        total += len(evidence.metric_value.encode("utf-8"))
        return total

    @classmethod
    def from_inspector_results(
        cls,
        target_name: str,
        inspector_results: list[InspectorResult],
        *,
        intent: str | None = None,
        started_at: datetime,
        finished_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Report:
        """Construct a Report by mechanically flattening findings across
        the supplied `InspectorResult` list. No deduplication, sorting,
        or filtering — order is preserved.
        """
        if not inspector_results:
            raise ValueError("from_inspector_results requires at least one InspectorResult")

        flattened_findings: list[Finding] = [f for ir in inspector_results for f in ir.findings]

        return cls(
            report_id=uuid4(),
            schema_version="1.0",
            intent=intent,
            target_name=target_name,
            inspector_results=inspector_results,
            findings=flattened_findings,
            started_at=started_at,
            finished_at=finished_at,
            metadata=metadata if metadata is not None else {},
        )
