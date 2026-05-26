"""JSON rendering boundary for `Report`.

`render(report)` is the public entry point used by `hostlens inspect
--format json`, the MCP server (M7), and any future caller that needs a
machine-readable serialisation of a `Report`. It is the JSON twin of
`hostlens.reporting.render_markdown.render` and **must** apply the same
redaction boundary so that secret patterns covered by
`hostlens.core.redact.redact_text` never leak to stdout, `--output`
files, log sinks, or notifier payloads.

The function is intentionally a two-step composition:

1. `redact_report_for_render(report)` produces a deep-copied `Report`
   with every string field listed in spec §需求:`render_markdown` /
   `render_json` 必须在渲染边界对字符串字段过 `core/redact.py` masked.
   The source `report` argument is *not* mutated — the in-memory object
   held by the runner / Agent loop keeps its raw content so reasoning
   logic still has access to the original strings.
2. `redacted.model_dump_json(indent=2, exclude_none=False)` is the SOT
   for serialisation: Pydantic v2 owns the field set and the JSON
   encoding of `UUID` / `datetime`. We never hand-roll
   `json.dumps(report.model_dump())` here — keeping the dump path
   single-sourced through Pydantic guarantees the JSON shape matches
   what `Report.model_validate(json.loads(...))` accepts.

Why `exclude_none=False`: schema consumers (MCP clients, downstream
diff tools) need the full field set to be visible in every payload.
Omitting `intent` when it is `None` would make the JSON schema fragment
depend on runtime values, which breaks naive diffing and validation.
"""

from __future__ import annotations

from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import Report

__all__ = ["render"]


def render(report: Report) -> str:
    """Return the redacted JSON serialisation of `report`.

    The returned string is valid JSON (`json.loads` succeeds), contains
    every field of the `Report` model (including `null` values), and is
    indented with 2 spaces. The source `report` is not modified.
    """
    redacted = redact_report_for_render(report)
    return redacted.model_dump_json(indent=2, exclude_none=False)
