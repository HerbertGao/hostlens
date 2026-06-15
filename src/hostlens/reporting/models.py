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

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if TYPE_CHECKING:
    from hostlens.inspectors.result import InspectorResult, InspectorStatus

__all__ = [
    "BaselineRef",
    "Evidence",
    "Finding",
    "InspectorRun",
    "Report",
    "ReportMeta",
    "ReportStatus",
    "RootCauseHypothesis",
    "Severity",
    "Tag",
    "TokenUsage",
    "compute_finding_id",
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

    Four M1 core fields (`severity` / `message` / `evidence` / `tags`)
    plus three M3 add-only identity fields (`id` / `inspector_name` /
    `inspector_version`) plus one add-only source field (`target_name`).
    The identity fields default to `None` so that direct M1/M2
    construction and legacy schema-1.0 JSON load unchanged;
    `Report.from_inspector_results` populates them on the flattened
    copies it produces.

    `target_name` defaults to `None` (legacy / single-target paths leave
    it unset). The fleet (multi-target) assembly path stamps each
    flattened finding with its source `InspectorResult.target_name` so a
    single fleet Report can distinguish findings by origin target. It is
    deliberately **not** part of `compute_finding_id`: stamping it into
    the fingerprint would give the same check different ids across
    targets, breaking the same-id anchor per-target regression diff
    relies on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity
    message: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)

    id: str | None = None
    inspector_name: str | None = None
    inspector_version: str | None = None
    target_name: str | None = None


def compute_finding_id(inspector_name: str, inspector_version: str, message: str) -> str:
    """Deterministic, severity-agnostic content fingerprint for a finding.

    `sha256(f"{inspector_name}\\x00{inspector_version}\\x00{message}")[:16]`.
    Severity is deliberately excluded so the same finding keeps a stable
    `id` across runs even when its severity changes — that is what lets
    regression diff report `changed_severity` (same id, different
    severity) instead of a spurious resolved+added pair.

    `inspector_name` / `inspector_version` must be non-None: feeding the
    literal string ``"None"`` would silently collide findings from
    different inspectors. The factory always fills them before computing
    the id, so a None here is a programming error.
    """
    if inspector_name is None or inspector_version is None:
        raise ValueError(
            "compute_finding_id requires non-None inspector_name and inspector_version"
        )
    payload = f"{inspector_name}\x00{inspector_version}\x00{message}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ReportStatus(StrEnum):
    """Closed eight-value set for `Report.meta.status`, aligned with
    docs/ARCHITECTURE.md §9 Failure Semantics. `StrEnum` (a `str`
    subclass) gives the spec's required `str, Enum` semantics — string
    equality plus `ValueError` on an unknown value.

    This proposal *auto-derives* only three values: `OK` / `PARTIAL`
    (by `Report.from_inspector_results`) and `STORED_AS_ORPHAN` (by the
    `ReportStore` orphan fallback). The five `degraded_*` / `empty_response`
    members are defined here so the enum is complete and the factory's
    `status` override entry can accept them, but no code path in this
    proposal produces them — `add-diagnostician-agent` does, consuming
    `LoopResult.terminal_status`.

    `failed_api_unavailable` is intentionally absent: that scenario yields
    no Report at all and belongs to M4 `RunStatus` (§7 boundary).
    """

    OK = "ok"
    PARTIAL = "partial"
    DEGRADED_NO_PLANNER = "degraded_no_planner"
    DEGRADED_RATE_LIMITED = "degraded_rate_limited"
    DEGRADED_TOKEN_BUDGET = "degraded_token_budget"
    DEGRADED_MAX_TURNS = "degraded_max_turns"
    EMPTY_RESPONSE = "empty_response"
    STORED_AS_ORPHAN = "stored_as_orphan"


class TokenUsage(BaseModel):
    """Anthropic-shaped token usage tally. All fields default 0.

    Field names/types mirror `LoopUsage` (agent/loop.py) so a future
    Agent-path Report assembly can project via
    `TokenUsage(**loop_result.usage_totals.model_dump())`. This proposal
    only reaches the mechanical `--inspector` path (no LLM call), so the
    factory always emits `TokenUsage()` (all zero).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class InspectorRun(BaseModel):
    """Per-inspector run summary projected mechanically from an
    `InspectorResult` (name / version / status / duration + finding_count).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    status: Literal["ok", "timeout", "target_unreachable", "requires_unmet", "exception"]
    duration_seconds: float
    finding_count: int


class BaselineRef(BaseModel):
    """Reference to the baseline run selected for a regression diff.

    `inspector_versions` (name→version) is projected from the baseline
    report's `meta.inspectors_used` so diff version-alignment (diff rule
    5) has the data without reloading the full baseline blob.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    timestamp: datetime
    status: ReportStatus
    inspector_versions: dict[str, str] = Field(default_factory=dict)
    report_schema_version: str


class RootCauseHypothesis(BaseModel):
    """Root-cause hypothesis container.

    This proposal only *defines* the shape; `Report.hypotheses` stays `[]`
    until `add-diagnostician-agent` populates it. `supporting_findings`
    references `Finding.id` values (intra-report anchors).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    confidence: Literal["low", "medium", "high"]
    supporting_findings: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)


class ReportMeta(BaseModel):
    """Run metadata container — the forward-going authoritative source for
    target / timing / status / token information (the flat `Report`
    fields are retained only for M1/M2 consumer compatibility).

    `target_type` is intentionally a plain `str` rather than a Literal:
    canonical values are `local` / `ssh` / `docker` / `k8s` / `replay`,
    but the demo path uses `ReplayTarget` and future target kinds must
    not be blocked by a closed Literal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    report_schema_version: str = "1.1"
    timestamp: datetime
    target_id: str
    target_name: str
    target_type: str
    intent: str | None = None
    schedule_name: str | None = None
    status: ReportStatus
    inspectors_used: list[InspectorRun] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_seconds: float
    baseline_ref: BaselineRef | None = None
    diff_skipped_reason: str | None = None


class Report(BaseModel):
    """Container aggregating one or more `InspectorResult`s into a
    user-facing report.

    Construction via `Report.from_inspector_results(...)` is preferred —
    it auto-generates `report_id`, populates `meta`, locks
    `schema_version="1.1"`, and flattens findings (filling each flattened
    finding's identity fields). Direct `Report(**kwargs)` construction is
    allowed (Pydantic idiom) but the caller must populate all required
    fields.

    `meta` / `hypotheses` are M3 add-only containers. `meta` is the
    forward-going authoritative source; it is `None` only when loading a
    legacy schema-1.0 JSON (every report produced by the factory carries
    `meta` and writes `schema_version="1.1"`). `hypotheses` stays `[]`
    until `add-diagnostician-agent` populates it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    schema_version: Literal["1.0", "1.1"]
    intent: str | None = None
    target_name: str = Field(min_length=1)
    inspector_results: list[InspectorResult] = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)
    meta: ReportMeta | None = None
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)

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
        target_id: str | None = None,
        target_type: str = "local",
        token_usage: TokenUsage | None = None,
        status: ReportStatus | None = None,
        schedule_name: str | None = None,
    ) -> Report:
        """Construct a Report by mechanically flattening findings across
        the supplied `InspectorResult` list. No deduplication, sorting,
        or filtering — order is preserved.

        Each flattened finding is replaced with a `model_copy` that carries
        its source inspector's identity (`inspector_name` /
        `inspector_version`) and a deterministic `id` (see
        `compute_finding_id`). The report's `meta` is assembled and
        `schema_version` is locked to `"1.1"`.
        """
        if not inspector_results:
            raise ValueError("from_inspector_results requires at least one InspectorResult")

        flattened_findings: list[Finding] = []
        for ir in inspector_results:
            for finding in ir.findings:
                flattened_findings.append(
                    finding.model_copy(
                        update={
                            "inspector_name": ir.name,
                            "inspector_version": ir.version,
                            "id": compute_finding_id(ir.name, ir.version, finding.message),
                        }
                    )
                )

        report_id = uuid4()
        derived_status = status if status is not None else _derive_report_status(inspector_results)
        meta = ReportMeta(
            run_id=str(report_id),
            timestamp=started_at,
            target_id=target_id if target_id is not None else target_name,
            target_name=target_name,
            target_type=target_type,
            intent=intent,
            schedule_name=schedule_name,
            status=derived_status,
            inspectors_used=[
                InspectorRun(
                    name=ir.name,
                    version=ir.version,
                    status=ir.status,
                    duration_seconds=ir.duration_seconds,
                    finding_count=len(ir.findings),
                )
                for ir in inspector_results
            ],
            token_usage=token_usage if token_usage is not None else TokenUsage(),
            duration_seconds=(finished_at - started_at).total_seconds(),
        )

        return cls(
            report_id=report_id,
            schema_version="1.1",
            intent=intent,
            target_name=target_name,
            inspector_results=inspector_results,
            findings=flattened_findings,
            started_at=started_at,
            finished_at=finished_at,
            metadata=metadata if metadata is not None else {},
            meta=meta,
        )

    @classmethod
    def from_fleet_results(
        cls,
        inspector_results: list[InspectorResult],
        *,
        schedule_name: str,
        intent: str | None = None,
        started_at: datetime,
        finished_at: datetime,
        token_usage: TokenUsage | None = None,
        status: ReportStatus | None = None,
    ) -> Report:
        """Assemble one fleet (multi-target) Report from `InspectorResult`s
        spanning multiple targets — the deterministic-inspection-mode path.

        Each `InspectorResult` carries its own `target_name`. The assembly
        is deterministic and **independent of caller-supplied target order**:

        - `Report.target_name` is a fleet label derived by sorting the
          distinct participating target names into canonical order and
          joining them, so the same target set yields the same label
          regardless of input order (satisfies `min_length=1`).
        - `meta.target_id` is a fleet id derived from the **sorted** set of
          target ids plus `schedule_name`, forced into a `fleet:`-prefixed
          namespace disjoint from any bare per-target `target_id`. This
          keeps a single-member fleet (`targets:[x]`) from colliding with
          the same host's agent-mode per-target report (`target_id == "x"`)
          in `ReportStore`, which would let `compute_diff` cross fleet and
          per-target baselines.
        - Each flattened finding is stamped with its source
          `InspectorResult.target_name` **and** filled with identity fields
          (`id` / `inspector_name` / `inspector_version`) exactly as
          `from_inspector_results` does — identity fields must not stay
          None (downstream proposal C dedups on
          `(target_name, inspector_name, message, severity)`).
        - `meta.inspectors_used` keeps one `InspectorRun` per
          `(target, inspector)` with each status preserved verbatim
          (`requires_unmet` / `timeout` / ... are never folded or dropped).
        - `status`: used as-is when supplied; when `None`, derived via the
          deterministic truth table that treats `requires_unmet` as `ok`
          before applying the existing `_derive_report_status` semantics.
        """
        if not inspector_results:
            raise ValueError("from_fleet_results requires at least one InspectorResult")

        flattened_findings: list[Finding] = []
        for ir in inspector_results:
            for finding in ir.findings:
                flattened_findings.append(
                    finding.model_copy(
                        update={
                            "inspector_name": ir.name,
                            "inspector_version": ir.version,
                            "id": compute_finding_id(ir.name, ir.version, finding.message),
                            "target_name": ir.target_name,
                        }
                    )
                )

        sorted_targets = sorted({ir.target_name for ir in inspector_results})
        fleet_target_name = ",".join(sorted_targets)
        # `meta.target_id` is the fleet's `ReportStore` key. A bare comma-join of
        # targets + schedule_name would alias different fleets when a target name
        # or the schedule contains a comma (e.g. targets=[a,b] + sched="c,d" vs
        # targets=[a,b,c] + sched="d" both → "fleet:a,b,c,d"), silently overwriting
        # one fleet's history with another's. Hash the **structured**
        # (count, sorted targets, schedule) over a NUL separator so the key is
        # unambiguous and collision-resistant for any name content; the `fleet:`
        # prefix keeps it disjoint from any bare per-target `target_id`.
        fleet_target_id = (
            "fleet:"
            + hashlib.sha256(
                "\x00".join([str(len(sorted_targets)), *sorted_targets, schedule_name]).encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        report_id = uuid4()
        derived_status = (
            status if status is not None else _derive_fleet_report_status(inspector_results)
        )
        meta = ReportMeta(
            run_id=str(report_id),
            timestamp=started_at,
            target_id=fleet_target_id,
            target_name=fleet_target_name,
            target_type="fleet",
            intent=intent,
            schedule_name=schedule_name,
            status=derived_status,
            inspectors_used=[
                InspectorRun(
                    name=ir.name,
                    version=ir.version,
                    status=ir.status,
                    duration_seconds=ir.duration_seconds,
                    finding_count=len(ir.findings),
                )
                for ir in inspector_results
            ],
            token_usage=token_usage if token_usage is not None else TokenUsage(),
            duration_seconds=(finished_at - started_at).total_seconds(),
        )

        return cls(
            report_id=report_id,
            schema_version="1.1",
            intent=intent,
            target_name=fleet_target_name,
            inspector_results=inspector_results,
            findings=flattened_findings,
            started_at=started_at,
            finished_at=finished_at,
            metadata={},
            meta=meta,
        )


def _derive_status_from_statuses(statuses: list[InspectorStatus]) -> ReportStatus:
    """The single ARCHITECTURE §9 Failure-Semantics truth table over a list of
    inspector statuses (the one source of truth for both the per-target and the
    fleet derivation — keep it here so a §9 change can never drift between two
    hand-maintained copies).

    All `ok` → `ok`. Non-ok statuses that are *only* `timeout` with at least one
    `ok` → `ok` (§9: a partial/single inspector timeout does not degrade —
    "ok unless all timed out"). Any `target_unreachable` / `exception` /
    `requires_unmet`, or *all* `timeout`, → `partial`.
    """
    if all(s == "ok" for s in statuses):
        return ReportStatus.OK

    non_ok = [s for s in statuses if s != "ok"]
    if all(s == "timeout" for s in non_ok) and any(s == "ok" for s in statuses):
        return ReportStatus.OK

    return ReportStatus.PARTIAL


def _derive_report_status(inspector_results: list[InspectorResult]) -> ReportStatus:
    """Per-target derivation: the §9 truth table over the raw inspector statuses."""
    return _derive_status_from_statuses([ir.status for ir in inspector_results])


def _derive_fleet_report_status(inspector_results: list[InspectorResult]) -> ReportStatus:
    """Fleet (deterministic-mode) derivation: `requires_unmet` is an *expected*
    skip (a fixed health set on a heterogeneous fleet routinely hits services
    absent on some hosts), so it is re-mapped to `ok` **before** the shared §9
    truth table — it must never degrade the report. Real degradation (all
    `timeout` / any `target_unreachable` / `exception`) still degrades; this is
    deliberately not an unconditional `ok`.
    """
    return _derive_status_from_statuses(
        ["ok" if ir.status == "requires_unmet" else ir.status for ir in inspector_results]
    )
