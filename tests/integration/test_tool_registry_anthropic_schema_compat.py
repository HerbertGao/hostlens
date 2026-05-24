"""Verify ToolsAdapter projections are valid Anthropic-compatible JSON Schemas.

The Anthropic Messages API `tool_use` protocol requires each tool's
`input_schema` to be a valid JSON Schema (Draft 2020-12). Pydantic v2's
`model_json_schema()` should produce conformant output, but we keep a
dedicated integration test so that any future regression — either in
Pydantic or in our ToolSpec field definitions — fails loudly here
before it leaks into a real LLM call.

Uses the `jsonschema` dev dependency (declared in pyproject's
`[project.optional-dependencies].dev`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from jsonschema import Draft202012Validator, ValidationError

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools import ToolContext, ToolRegistry


def test_all_projected_input_schemas_pass_draft_2020_12(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    adapter = ToolsAdapter(tool_registry, tool_context_factory)
    projections = adapter.list_for_agent()

    assert len(projections) == 3  # M2 batch: run_inspector / list_inspectors / list_targets

    for entry in projections:
        schema = entry["input_schema"]
        # `check_schema` raises `jsonschema.SchemaError` if the schema
        # itself is invalid against Draft 2020-12 metaschema.
        Draft202012Validator.check_schema(schema)


def test_run_inspector_schema_rejects_missing_required_fields(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    """`RunInspectorInput.target_name` / `inspector_name` are required.

    Verify the projected JSON Schema actually enforces this via
    `jsonschema` validation (sanity check that Pydantic emitted
    `required: [...]` correctly).
    """
    adapter = ToolsAdapter(tool_registry, tool_context_factory)
    schema = next(
        entry["input_schema"]
        for entry in adapter.list_for_agent()
        if entry["name"] == "run_inspector"
    )

    validator = Draft202012Validator(schema)

    # Both required → instance is valid.
    validator.validate({"target_name": "t1", "inspector_name": "i1"})

    # Missing inspector_name → schema rejects.
    with pytest.raises(ValidationError):
        validator.validate({"target_name": "t1"})

    # Wrong type for target_name → schema rejects.
    with pytest.raises(ValidationError):
        validator.validate({"target_name": 42, "inspector_name": "i1"})


def test_list_inspectors_schema_accepts_optional_filters(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    """`ListInspectorsInput.tag` / `target_kind` are both optional.

    Empty `{}` must validate; both filters provided must also validate.
    """
    adapter = ToolsAdapter(tool_registry, tool_context_factory)
    schema = next(
        entry["input_schema"]
        for entry in adapter.list_for_agent()
        if entry["name"] == "list_inspectors"
    )

    validator = Draft202012Validator(schema)

    # Empty payload — no required fields.
    validator.validate({})
    # Both filters supplied.
    validator.validate({"tag": "linux", "target_kind": "ssh"})


def test_list_targets_schema_accepts_default_payload(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    """`ListTargetsInput.include_disabled` defaults to False.

    Empty `{}` and `{"include_disabled": true}` must both validate;
    a wrong type must reject.
    """
    adapter = ToolsAdapter(tool_registry, tool_context_factory)
    schema = next(
        entry["input_schema"]
        for entry in adapter.list_for_agent()
        if entry["name"] == "list_targets"
    )

    validator = Draft202012Validator(schema)

    validator.validate({})
    validator.validate({"include_disabled": True})

    with pytest.raises(ValidationError):
        validator.validate({"include_disabled": "yes"})
