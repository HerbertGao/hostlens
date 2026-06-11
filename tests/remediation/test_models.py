"""Executable contract checks for `RemediationStep` / `RemediationPlan`.

Each test maps to a `remediation-plan-schema` spec scenario. The suite is the
deterministic strong-anchor for the P1a contract: every validation invariant,
ValidationError trigger, fail-closed tightening, and the JSON round-trip /
duplicate-key behaviour is pinned here so downstream P1b/P2 can rely on it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.remediation.models import RemediationPlan, RemediationStep


def _step(**overrides: object) -> RemediationStep:
    base: dict[str, object] = {
        "description": "清理轮转日志",
        "precheck_cmd": "test -d /var/log",
        "forward_cmd": "find /var/log -name '*.gz' -delete",
        "rollback_cmd": "echo cannot-undo",
        "verify_cmd": "true",
        "risk_level": "low",
    }
    base.update(overrides)
    return RemediationStep(**base)  # type: ignore[arg-type]


def _plan(**overrides: object) -> RemediationPlan:
    base: dict[str, object] = {
        "finding_id": "disk-var-log-full",
        "target_name": "prod-web-01",
        "rationale": "/var 使用率 94%",
        "steps": [_step()],
        "estimated_duration_seconds": 5,
    }
    base.update(overrides)
    return RemediationPlan(**base)  # type: ignore[arg-type]


# --- RemediationStep: fields & command constraints -------------------------


def test_valid_low_step_roundtrips_field_values() -> None:
    step = _step(risk_level="low")
    assert step.risk_level == "low"
    assert step.forward_cmd == "find /var/log -name '*.gz' -delete"


def test_precheck_rollback_none_allowed_when_invariants_hold() -> None:
    # low + precheck=None + rollback non-None
    assert _step(risk_level="low", precheck_cmd=None).precheck_cmd is None
    # high + precheck set + rollback=None
    assert _step(risk_level="high", precheck_cmd="x", rollback_cmd=None).rollback_cmd is None


@pytest.mark.parametrize("field", ["forward_cmd", "verify_cmd"])
@pytest.mark.parametrize("bad", ["", "   ", "\t", "​", "﻿"])
def test_command_fields_reject_empty_and_blank(field: str, bad: str) -> None:
    # "" trips min_length (string_too_short); whitespace / invisible-only trips
    # the blank-check validator (command_must_not_be_blank).
    with pytest.raises(ValidationError) as exc:
        _step(**{field: bad})
    if bad != "":
        assert "command_must_not_be_blank" in str(exc.value)


@pytest.mark.parametrize("field", ["precheck_cmd", "rollback_cmd"])
@pytest.mark.parametrize("bad", ["", "   ", "\t", "​", "﻿"])
def test_optional_command_fields_reject_empty_and_blank(field: str, bad: str) -> None:
    # risk_level=high keeps invariants satisfiable while we probe the field itself
    overrides: dict[str, object] = {
        "risk_level": "high",
        "precheck_cmd": "ok",
        "rollback_cmd": "ok",
    }
    overrides[field] = bad
    with pytest.raises(ValidationError) as exc:
        _step(**overrides)
    if bad != "":
        assert "command_must_not_be_blank" in str(exc.value)


def test_command_field_preserves_legitimate_surrounding_whitespace() -> None:
    step = _step(forward_cmd="  echo hi  ")
    assert step.forward_cmd == "  echo hi  "


def test_step_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        _step(bogus="x")


@pytest.mark.parametrize("bad", ["HIGH", "critical", "High", "", "low "])
def test_step_rejects_illegal_risk_level(bad: str) -> None:
    with pytest.raises(ValidationError):
        _step(risk_level=bad)


@pytest.mark.parametrize("bad", [None, 123])
def test_description_must_be_str(bad: object) -> None:
    with pytest.raises(ValidationError):
        _step(description=bad)


def test_description_empty_string_allowed() -> None:
    assert _step(description="").description == ""


# --- cross-field invariants + canonical tokens -----------------------------


def test_high_missing_precheck_rejected_with_token() -> None:
    with pytest.raises(ValidationError) as exc:
        _step(risk_level="high", precheck_cmd=None)
    assert "high_requires_precheck" in str(exc.value)


def test_high_with_precheck_ok_rollback_either() -> None:
    assert _step(risk_level="high", precheck_cmd="p", rollback_cmd=None) is not None
    assert _step(risk_level="high", precheck_cmd="p", rollback_cmd="r") is not None


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_non_high_may_omit_precheck(risk: str) -> None:
    assert _step(risk_level=risk, precheck_cmd=None, rollback_cmd="r").precheck_cmd is None


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_rollback_none_non_high_rejected_with_token(risk: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _step(risk_level=risk, precheck_cmd=None, rollback_cmd=None)
    assert "rollback_none_requires_high" in str(exc.value)


def test_errors_type_is_value_error_not_token() -> None:
    # token lives only in the message string, never in errors()[i]["type"]
    with pytest.raises(ValidationError) as exc:
        _step(risk_level="high", precheck_cmd=None)
    assert exc.value.errors()[0]["type"] == "value_error"


def test_field_error_short_circuits_model_validator() -> None:
    # forward_cmd="" (field error) coexists with high+precheck=None (invariant):
    # only the field-level error surfaces; the invariant token does NOT appear.
    with pytest.raises(ValidationError) as exc:
        _step(risk_level="high", precheck_cmd=None, forward_cmd="")
    msg = str(exc.value)
    assert "high_requires_precheck" not in msg
    assert exc.value.errors()[0]["type"] == "string_too_short"


# --- required optional fields (no silent default) --------------------------


@pytest.mark.parametrize("missing", ["precheck_cmd", "rollback_cmd", "forward_cmd"])
def test_missing_key_rejected_not_defaulted(missing: str) -> None:
    fields = {
        "description": "d",
        "precheck_cmd": "p",
        "forward_cmd": "f",
        "rollback_cmd": "r",
        "verify_cmd": "v",
        "risk_level": "low",
    }
    del fields[missing]
    with pytest.raises(ValidationError) as exc:
        RemediationStep(**fields)  # type: ignore[arg-type]
    assert exc.value.errors()[0]["type"] == "missing"


# --- RemediationPlan: binding fields & metadata ----------------------------


def test_valid_plan_preserves_step_order() -> None:
    s1, s2 = _step(description="1"), _step(description="2")
    plan = _plan(steps=[s1, s2])
    assert [s.description for s in plan.steps] == ["1", "2"]


@pytest.mark.parametrize("field", ["finding_id", "target_name"])
@pytest.mark.parametrize("bad", ["", "   ", "\t", "​", "﻿"])
def test_binding_fields_reject_blank_equivalent(field: str, bad: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _plan(**{field: bad})
    if bad != "":
        assert "binding_field_must_not_be_blank" in str(exc.value)


def test_binding_field_with_visible_content_allowed() -> None:
    assert _plan(finding_id=" disk-full ").finding_id == " disk-full "


@pytest.mark.parametrize("bad", [None, 123])
def test_rationale_must_be_str(bad: object) -> None:
    with pytest.raises(ValidationError):
        _plan(rationale=bad)


def test_plan_rejects_empty_steps() -> None:
    with pytest.raises(ValidationError):
        _plan(steps=[])


def test_plan_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        _plan(bogus="x")


@pytest.mark.parametrize("bad", [-1, 5.0, "5", True, False])
def test_duration_strict_int_rejects_coercions_and_negative(bad: object) -> None:
    with pytest.raises(ValidationError):
        _plan(estimated_duration_seconds=bad)


def test_duration_accepts_nonneg_int() -> None:
    assert _plan(estimated_duration_seconds=0).estimated_duration_seconds == 0


# --- nested dict coercion path (P2 load path) ------------------------------


def test_steps_as_dict_valid() -> None:
    plan = RemediationPlan.model_validate(
        {
            "finding_id": "f",
            "target_name": "t",
            "rationale": "r",
            "estimated_duration_seconds": 1,
            "steps": [
                {
                    "description": "d",
                    "precheck_cmd": None,
                    "forward_cmd": "f",
                    "rollback_cmd": "r",
                    "verify_cmd": "v",
                    "risk_level": "low",
                }
            ],
        }
    )
    assert plan.steps[0].forward_cmd == "f"


def test_steps_dict_extra_field_propagates_forbid() -> None:
    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(
            {
                "finding_id": "f",
                "target_name": "t",
                "rationale": "r",
                "estimated_duration_seconds": 1,
                "steps": [
                    {
                        "description": "d",
                        "precheck_cmd": None,
                        "forward_cmd": "f",
                        "rollback_cmd": "r",
                        "verify_cmd": "v",
                        "risk_level": "low",
                        "bogus": "x",
                    }
                ],
            }
        )


def test_steps_dict_high_missing_precheck_propagates_invariant() -> None:
    with pytest.raises(ValidationError) as exc:
        RemediationPlan.model_validate(
            {
                "finding_id": "f",
                "target_name": "t",
                "rationale": "r",
                "estimated_duration_seconds": 1,
                "steps": [
                    {
                        "description": "d",
                        "precheck_cmd": None,
                        "forward_cmd": "f",
                        "rollback_cmd": None,
                        "verify_cmd": "v",
                        "risk_level": "high",
                    }
                ],
            }
        )
    assert "high_requires_precheck" in str(exc.value)


# --- JSON round-trip + duplicate-key loader --------------------------------


def test_json_roundtrip_with_null_rollback_high_step() -> None:
    plan = _plan(
        steps=[_step(risk_level="high", precheck_cmd="p", rollback_cmd=None)],
    )
    dumped = plan.model_dump_json()
    assert '"rollback_cmd":null' in dumped
    assert RemediationPlan.model_validate_json(dumped) == plan


def test_load_json_rejects_duplicate_keys() -> None:
    payload = (
        '{"finding_id":"f","finding_id":"g","target_name":"t","rationale":"r",'
        '"estimated_duration_seconds":1,"steps":[{"description":"d",'
        '"precheck_cmd":null,"forward_cmd":"f","rollback_cmd":"r",'
        '"verify_cmd":"v","risk_level":"low"}]}'
    )
    with pytest.raises(ValueError, match="duplicate_json_key"):
        RemediationPlan.load_json(payload)


def test_load_json_accepts_clean_payload() -> None:
    plan = _plan(steps=[_step(risk_level="high", precheck_cmd="p", rollback_cmd=None)])
    assert RemediationPlan.load_json(plan.model_dump_json()) == plan


# --- immutability -----------------------------------------------------------


def test_models_are_frozen() -> None:
    step = _step()
    with pytest.raises(ValidationError):
        step.forward_cmd = "mutated"  # type: ignore[misc]
