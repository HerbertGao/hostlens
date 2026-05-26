"""Tests for `Finding` Pydantic model.

Covers spec §需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.reporting.models import Evidence, Finding


def test_minimal_finding() -> None:
    f = Finding(severity="info", message="ok")
    assert f.severity == "info"
    assert f.message == "ok"
    assert f.evidence == []
    assert f.tags == []


def test_finding_with_evidence() -> None:
    f = Finding(
        severity="critical",
        message="db down",
        evidence=[
            Evidence(
                kind="command_output",
                command="ping db",
                stdout="",
                stderr="timeout",
                exit_code=1,
            )
        ],
    )
    assert f.evidence[0].kind == "command_output"
    assert f.evidence[0].exit_code == 1


def test_finding_with_tags() -> None:
    f = Finding(severity="warning", message="cpu high", tags=["cpu", "perf"])
    assert f.tags == ["cpu", "perf"]


def test_finding_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity="info",
            message="x",
            evidence=[],
            tags=[],
            extra="y",  # type: ignore[call-arg]
        )


def test_finding_rejects_dict_evidence() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity="info",
            message="x",
            evidence={"key": "value"},  # type: ignore[arg-type]
        )


def test_finding_rejects_non_evidence_list_element() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity="info",
            message="x",
            evidence=["not an evidence"],  # type: ignore[list-item]
        )


def test_finding_rejects_non_string_tags() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity="info",
            message="x",
            tags=[123, None],  # type: ignore[list-item]
        )


def test_finding_rejects_empty_message() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="")


def test_finding_is_frozen() -> None:
    f = Finding(severity="info", message="x")
    with pytest.raises((ValidationError, TypeError)):
        f.severity = "critical"  # type: ignore[misc]


def test_finding_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="debug", message="x")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Tag pattern: spec §需求:`Finding` Pydantic 模型 (`^[a-z][a-z0-9_-]*$`)
# --------------------------------------------------------------------------- #


def test_finding_accepts_valid_tags() -> None:
    f = Finding(
        severity="info",
        message="x",
        tags=["cpu", "perf", "linux-os", "tag_with_underscore"],
    )
    assert f.tags == ["cpu", "perf", "linux-os", "tag_with_underscore"]


def test_finding_rejects_uppercase_tag() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", tags=["BadTag"])


def test_finding_rejects_empty_tag() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", tags=[""])


def test_finding_rejects_tag_starting_with_digit() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", tags=["1bad"])


def test_finding_rejects_tag_with_space() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", tags=["bad tag"])
