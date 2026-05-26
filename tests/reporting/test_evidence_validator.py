"""Matrix tests for `Evidence` model-level kind ↔ field validator.

Covers all four `kind` values x (legal field set / missing required /
forbidden field present), per spec
§需求:`Evidence` Pydantic 模型必须按 kind ↔ 字段子集映射强制约束.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.reporting.models import Evidence

# -- command_output ---------------------------------------------------------


def test_command_output_minimal_legal() -> None:
    e = Evidence(kind="command_output", command="echo hi", stdout="hi\n")
    assert e.kind == "command_output"
    assert e.command == "echo hi"


def test_command_output_full_legal() -> None:
    e = Evidence(
        kind="command_output",
        command="ls -l",
        stdout="total 0\n",
        stderr="",
        exit_code=0,
        truncated=True,
    )
    assert e.exit_code == 0
    assert e.truncated is True


def test_command_output_missing_command_rejected() -> None:
    with pytest.raises(ValidationError, match="command"):
        Evidence(kind="command_output", stdout="hi\n")


def test_command_output_missing_stdout_rejected() -> None:
    with pytest.raises(ValidationError, match="stdout"):
        Evidence(kind="command_output", command="echo hi")


def test_command_output_forbids_path() -> None:
    with pytest.raises(ValidationError, match="path"):
        Evidence(kind="command_output", command="echo hi", stdout="hi\n", path="/etc/hosts")


def test_command_output_forbids_metric_name() -> None:
    with pytest.raises(ValidationError, match="metric_name"):
        Evidence(
            kind="command_output",
            command="echo hi",
            stdout="hi\n",
            metric_name="cpu",
        )


# -- file_excerpt -----------------------------------------------------------


def test_file_excerpt_legal() -> None:
    e = Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="127.0.0.1 localhost\n")
    assert e.path == "/etc/hosts"


def test_file_excerpt_missing_path_rejected() -> None:
    with pytest.raises(ValidationError, match="path"):
        Evidence(kind="file_excerpt", excerpt="x")


def test_file_excerpt_missing_excerpt_rejected() -> None:
    with pytest.raises(ValidationError, match="excerpt"):
        Evidence(kind="file_excerpt", path="/etc/hosts")


def test_file_excerpt_forbids_command() -> None:
    with pytest.raises(ValidationError, match="command"):
        Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="x", command="cat /etc/hosts")


def test_file_excerpt_forbids_stdout() -> None:
    with pytest.raises(ValidationError, match="stdout"):
        Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="x", stdout="x")


# -- metric -----------------------------------------------------------------


def test_metric_float_value_legal() -> None:
    e = Evidence(kind="metric", metric_name="load_1min", metric_value=0.42)
    assert e.metric_value == 0.42


def test_metric_str_value_legal() -> None:
    e = Evidence(kind="metric", metric_name="load_1min", metric_value="unavailable")
    assert e.metric_value == "unavailable"


def test_metric_missing_name_rejected() -> None:
    with pytest.raises(ValidationError, match="metric_name"):
        Evidence(kind="metric", metric_value=1.0)


def test_metric_forbids_command() -> None:
    with pytest.raises(ValidationError, match="command"):
        Evidence(kind="metric", metric_name="x", metric_value=1.0, command="echo")


def test_metric_forbids_data() -> None:
    with pytest.raises(ValidationError, match="data"):
        Evidence(kind="metric", metric_name="x", metric_value=1.0, data={"a": "b"})


# -- structured -------------------------------------------------------------


def test_structured_legal() -> None:
    e = Evidence(kind="structured", data={"k": "v"})
    assert e.data == {"k": "v"}


def test_structured_truncated_legal() -> None:
    e = Evidence(kind="structured", data={}, truncated=True)
    assert e.truncated is True


def test_structured_missing_data_rejected() -> None:
    with pytest.raises(ValidationError):
        Evidence(kind="structured")


def test_structured_forbids_path() -> None:
    with pytest.raises(ValidationError, match="path"):
        Evidence(kind="structured", data={}, path="/etc/hosts")


# -- generic ----------------------------------------------------------------


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Evidence(kind="trace", data={})  # type: ignore[arg-type]


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            kind="command_output",
            command="x",
            stdout="y",
            weird_field="z",  # type: ignore[call-arg]
        )


def test_evidence_is_frozen() -> None:
    e = Evidence(kind="command_output", command="x", stdout="y")
    with pytest.raises((ValidationError, TypeError)):
        e.kind = "structured"  # type: ignore[misc]


def test_truncated_works_for_all_kinds() -> None:
    Evidence(kind="command_output", command="x", stdout="y", truncated=True)
    Evidence(kind="file_excerpt", path="/x", excerpt="y", truncated=True)
    Evidence(kind="metric", metric_name="x", metric_value=1.0, truncated=True)
    Evidence(kind="structured", data={}, truncated=True)
