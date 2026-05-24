"""Tests for ToolSpec per spec §需求:ToolSpec 数据模型必须包含完整 policy 元数据.

Covers all spec scenarios:
- Field completeness (defaults).
- `extra` fields rejected.
- Frozen (immutable).
- `input_schema` must be a `BaseModel` subclass.
- `sensitive_output` default is `None` (not `False`).
- `name` must match snake_case regex; rejects kebab-case / uppercase / digit-prefix.
- `version` rejects empty string but accepts arbitrary non-empty opaque strings.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from hostlens.tools.base import ToolContext, ToolSpec


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


async def _handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    return _Out()


def _minimal_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        name="x",
        version="1.0.0",
        input_schema=_In,
        output_schema=_Out,
        handler=_handler,
        agent_description="ad",
        mcp_description="md",
        cli_help=None,
        surfaces={"agent"},
        side_effects="read",
    )
    base.update(overrides)
    return base


def test_minimal_spec_uses_documented_defaults() -> None:
    spec = ToolSpec(**_minimal_kwargs())  # type: ignore[arg-type]
    assert spec.sensitive_output is None
    assert spec.requires_approval is False
    assert spec.permissions == set()
    assert spec.target_constraints is None
    assert spec.timeout is None
    assert spec.tags == set()


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        ToolSpec(**_minimal_kwargs(unknown_field="x"))  # type: ignore[arg-type]
    assert "Extra inputs are not permitted" in str(ei.value) or "extra" in str(ei.value).lower()


def test_spec_is_frozen() -> None:
    spec = ToolSpec(**_minimal_kwargs())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        spec.name = "y"  # type: ignore[misc]


def test_input_schema_must_be_basemodel_subclass() -> None:
    with pytest.raises(ValidationError) as ei:
        ToolSpec(**_minimal_kwargs(input_schema=dict))  # type: ignore[arg-type]
    assert "BaseModel" in str(ei.value)

    with pytest.raises(ValidationError):
        ToolSpec(**_minimal_kwargs(input_schema="MyInput"))  # type: ignore[arg-type]


def test_output_schema_must_be_basemodel_subclass() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(**_minimal_kwargs(output_schema=dict))  # type: ignore[arg-type]


def test_sensitive_output_default_is_none_not_false() -> None:
    spec = ToolSpec(**_minimal_kwargs())  # type: ignore[arg-type]
    assert spec.sensitive_output is None
    # Explicitly checking the distinction:
    assert spec.sensitive_output is not False


def test_name_must_match_snake_case_regex_rejects_kebab() -> None:
    with pytest.raises(ValidationError) as ei:
        ToolSpec(**_minimal_kwargs(name="run-inspector"))  # type: ignore[arg-type]
    assert "pattern" in str(ei.value).lower() or "regex" in str(ei.value).lower()


def test_name_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(**_minimal_kwargs(name="RunInspector"))  # type: ignore[arg-type]


def test_name_rejects_digit_prefix() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(**_minimal_kwargs(name="1_tool"))  # type: ignore[arg-type]


def test_version_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(**_minimal_kwargs(version=""))  # type: ignore[arg-type]


def test_version_accepts_any_non_empty_opaque_string() -> None:
    for v in ["latest", "1", "0.1.0-alpha+build", "v2-rc"]:
        spec = ToolSpec(**_minimal_kwargs(version=v))  # type: ignore[arg-type]
        assert spec.version == v


def test_handler_must_be_async_function() -> None:
    def sync_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
        return _Out()

    with pytest.raises(ValidationError) as ei:
        ToolSpec(**_minimal_kwargs(handler=sync_handler))  # type: ignore[arg-type]
    assert "async" in str(ei.value).lower()


def test_spec_does_not_persist_host_specific_schema_fields() -> None:
    """spec §需求:ToolSpec 禁止持久化 host-specific JSON Schema."""
    forbidden = {"anthropic_schema", "mcp_schema", "openai_schema", "host_schema"}
    assert ToolSpec.model_fields.keys().isdisjoint(forbidden)
