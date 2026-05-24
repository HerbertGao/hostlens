"""Tests for `hostlens.tools.schemas.*` covering field-set invariants,
`kind` enum strictness, `extra="forbid"` rejection, and the absence of
forbidden substrings in `ListTargetsOutput.model_dump_json`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.tools.schemas.list_targets import (
    ListTargetsOutput,
    TargetSummary,
)


def test_target_summary_field_set_is_exactly_seven_entries() -> None:
    expected = {
        "name",
        "kind",
        "display_name",
        "description",
        "capabilities",
        "tags",
        "enabled",
    }
    assert set(TargetSummary.model_fields.keys()) == expected
    assert len(TargetSummary.model_fields) == 7


def test_target_summary_kind_rejects_kubernetes_and_accepts_k8s() -> None:
    # "k8s" is valid.
    TargetSummary(
        name="t",
        kind="k8s",
        display_name=None,
        description=None,
        capabilities=[],
        tags=[],
        enabled=True,
    )
    # "kubernetes" is invalid — Literal forbids it.
    with pytest.raises(ValidationError):
        TargetSummary(
            name="t",
            kind="kubernetes",  # type: ignore[arg-type]
            display_name=None,
            description=None,
            capabilities=[],
            tags=[],
            enabled=True,
        )


def test_target_summary_extra_field_is_rejected() -> None:
    """`extra="forbid"` blocks any forbidden field-name from leaking in,
    even one that happens to be on the spec's 15-name blocklist
    (ssh_key_path, password, host, ...). The model_config does the work
    structurally — the test only needs to prove one forbidden name fails
    because `extra="forbid"` applies uniformly.
    """
    with pytest.raises(ValidationError) as ei:
        TargetSummary(
            name="t",
            kind="ssh",
            display_name=None,
            description=None,
            capabilities=[],
            tags=[],
            enabled=True,
            ssh_key_path="/Users/alice/.ssh/id_rsa",  # type: ignore[call-arg]
        )
    assert "extra" in str(ei.value).lower()


def test_list_targets_output_json_excludes_forbidden_substrings() -> None:
    """The spec lists 15+ forbidden field-name strings. Once the schema
    is `extra="forbid"` and the field set is locked to the 7 allowed
    keys, no JSON dump can contain those field names by structural
    invariant — but we assert it programmatically here as a regression
    fence (a future field-name typo would surface immediately).
    """
    out = ListTargetsOutput(
        targets=[
            TargetSummary(
                name="prod-web",
                kind="ssh",
                display_name=None,
                description=None,
                capabilities=["shell"],
                tags=["web", "prod"],
                enabled=True,
            )
        ]
    )
    json_text = out.model_dump_json()

    forbidden = [
        "password",
        "token",
        "private_key",
        "ssh_key_path",
        "connection_string",
        "dsn",
        "url",
        "host",
        "hostname",
        "ip_address",
        "port",
        "username",
        "env",
        "secret_ref",
        "raw_config",
    ]
    for needle in forbidden:
        assert needle not in json_text, (
            f"forbidden substring {needle!r} leaked into JSON: {json_text}"
        )
