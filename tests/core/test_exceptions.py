from __future__ import annotations

import re

import pytest

import hostlens.core.exceptions as exceptions_module
from hostlens.core.exceptions import (
    ConfigError,
    HostlensError,
    InspectorError,
    TargetError,
    ToolError,
    ToolPolicyViolation,
)


def test_subclasses_inherit_from_hostlens_error() -> None:
    assert isinstance(ConfigError("x"), HostlensError)
    assert isinstance(TargetError("x"), HostlensError)
    assert isinstance(InspectorError("x"), HostlensError)


def test_hostlens_error_catches_all_subclasses() -> None:
    caught: list[type[HostlensError]] = []
    for exc_cls in (ConfigError, TargetError, InspectorError):
        try:
            raise exc_cls("boom")
        except HostlensError as e:
            caught.append(type(e))
    assert caught == [ConfigError, TargetError, InspectorError]


def test_config_error_accepts_optional_original_exception() -> None:
    """`ConfigError` exposes `original` so `load_settings()` can chain the
    underlying `pydantic.ValidationError` for callers that need raw details.
    The base message must remain accessible via `str(...)` unchanged.
    """

    cause = ValueError("underlying")
    err = ConfigError("formatted message", original=cause)
    assert err.original is cause
    assert str(err) == "formatted message"

    # Default keeps original=None so existing call sites continue to work.
    err_no_cause = ConfigError("just a message")
    assert err_no_cause.original is None


def test_module_exports_exactly_six_exception_classes_after_m2() -> None:
    """M2 (`add-tool-registry-capability-layer`) extends the M0 four-class
    invariant to six by adding ToolError and ToolPolicyViolation.
    """

    public_names = [
        name
        for name in dir(exceptions_module)
        if not name.startswith("_")
        and isinstance(getattr(exceptions_module, name), type)
        and issubclass(getattr(exceptions_module, name), BaseException)
    ]
    assert sorted(public_names) == [
        "ConfigError",
        "HostlensError",
        "InspectorError",
        "TargetError",
        "ToolError",
        "ToolPolicyViolation",
    ]
    assert len(public_names) == 6
    assert sorted(exceptions_module.__all__) == [
        "ConfigError",
        "HostlensError",
        "InspectorError",
        "TargetError",
        "ToolError",
        "ToolPolicyViolation",
    ]


# ---------------------------------------------------------------------------
# M2: ToolError / ToolPolicyViolation
# ---------------------------------------------------------------------------


def test_tool_error_inherits_from_hostlens_error() -> None:
    assert issubclass(ToolError, HostlensError)
    assert isinstance(ToolError("x"), HostlensError)


def test_tool_policy_violation_inherits_from_tool_error_and_hostlens_error() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="agent",
        violated_field="surfaces",
        reason="not_exposed_to_surface",
    )
    assert isinstance(err, ToolError)
    assert isinstance(err, HostlensError)


def test_tool_policy_violation_exposes_structured_fields() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="mcp",
        violated_field="sensitive_output",
        reason="sensitive_output_not_declared",
    )
    assert err.tool_name == "run_inspector"
    assert err.surface == "mcp"
    assert err.violated_field == "sensitive_output"
    assert err.reason == "sensitive_output_not_declared"


def test_tool_policy_violation_str_contains_all_four_fields() -> None:
    err = ToolPolicyViolation(
        tool_name="list_targets",
        surface="agent",
        violated_field="side_effects",
        reason="side_effects_not_permitted",
    )
    text = str(err)
    assert "list_targets" in text
    assert "agent" in text
    assert "side_effects" in text
    assert "side_effects_not_permitted" in text


def test_tool_policy_violation_rejects_free_text_reason() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="surfaces",
            reason="custom free text with /Users/alice/secrets",  # type: ignore[arg-type]
        )


def test_tool_policy_violation_rejects_invalid_surface() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="openai",  # type: ignore[arg-type]
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_invalid_violated_field() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="bogus_field",  # type: ignore[arg-type]
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_invalid_tool_name_path() -> None:
    """A path-shaped `tool_name` must be rejected at construction time so it
    cannot reach `__str__` and become a prompt/log injection vector.
    """

    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="/Users/alice/secret",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_truncates_invalid_tool_name_to_32_chars() -> None:
    """The `ValueError` message must truncate attacker-controlled input to 32
    chars so the error itself cannot become a leak vector.
    """

    long_attacker_input = "A" * 100 + "_secret_token_value_in_full"
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name=long_attacker_input,
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    # Exactly 32 chars must appear; 33 must NOT — catches regressions where
    # someone bumps the cap (e.g. to 64) and silently breaks the safety budget.
    assert "A" * 32 in message
    assert "A" * 33 not in message
    assert "_secret_token_value_in_full" not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_truncates_invalid_surface_to_32_chars() -> None:
    """surface ValueError must apply the same 32-char truncation as tool_name."""

    long_attacker_input = "/Users/alice/" + "B" * 80 + "_token"
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface=long_attacker_input,  # type: ignore[arg-type]
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    # Exact-prefix assertion: must show first 32 chars, must NOT show 33 chars
    # (catches regressions where someone bumps truncation to 64 chars).
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message
    assert "_token" not in message


def test_tool_policy_violation_truncates_invalid_violated_field_to_32_chars() -> None:
    """violated_field ValueError must apply the same 32-char truncation."""

    long_attacker_input = "Bearer sk-" + "C" * 100
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field=long_attacker_input,  # type: ignore[arg-type]
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_truncates_invalid_reason_to_32_chars() -> None:
    """reason ValueError must apply the same 32-char truncation."""

    long_attacker_input = "admin@10.0.0.5 " + "D" * 100
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="surfaces",
            reason=long_attacker_input,  # type: ignore[arg-type]
        )
    message = str(exc_info.value)
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_rejects_uppercase_tool_name() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="RunInspector",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_kebab_tool_name() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="run-inspector",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_accepts_valid_snake_case() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="agent",
        violated_field="surfaces",
        reason="not_exposed_to_surface",
    )
    assert err.tool_name == "run_inspector"


def test_tool_policy_violation_repr_never_leaks_sensitive_substrings() -> None:
    """Cycle through every legal reason value and assert __str__ never leaks
    the canonical sensitive substring set (paths / IPv4 / Bearer tokens).
    """

    legal_reasons = [
        "not_exposed_to_surface",
        "side_effects_not_permitted",
        "approval_flow_not_supported_in_m2",
        "sensitive_output_not_declared",
        "missing_required_permission",
        "target_constraint_violated",
    ]
    sensitive_substrings = ["/Users/", "/home/", "Bearer "]
    ipv4_pattern = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
    for reason in legal_reasons:
        err = ToolPolicyViolation(
            tool_name="run_inspector",
            surface="agent",
            violated_field="surfaces",
            reason=reason,  # type: ignore[arg-type]
        )
        text = str(err)
        for sub in sensitive_substrings:
            assert sub not in text
        assert not ipv4_pattern.search(text)
