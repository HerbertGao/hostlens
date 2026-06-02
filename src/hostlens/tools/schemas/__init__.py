"""Pydantic input/output schemas for the M2 first-batch ToolSpecs.

Each ToolSpec keeps its `input_schema` / `output_schema` in a dedicated
sub-module so reviewers can read the contract without scrolling through
handler code:

- `run_inspector` — `RunInspectorInput` / `RunInspectorOutput`
- `list_inspectors` — `ListInspectorsInput` / `ListInspectorsOutput`
- `list_targets` — `ListTargetsInput` / `ListTargetsOutput` (+ the
  `scrub_inventory_string` redaction helper enforced by
  `list_targets_handler`).
- `correlate_findings` — `CorrelateFindingsInput` / `CorrelateFindingsOutput`
  (the Diagnostician's structured-output channel for one root-cause hypothesis).
- `request_more_inspection` — `RequestMoreInspectionInput` /
  `RequestMoreInspectionOutput` (+ `LabeledFinding`; the Diagnostician's
  re-run-one-inspector channel exposing `status` + id + ordinal label).
"""
