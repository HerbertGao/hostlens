"""Field-set + frozen + no-defaults contract for ``BackendCapabilities``.

The dataclass is intentionally rigid (frozen + all 7 fields required, no
defaults) so a backend implementation cannot accidentally inherit a default
declaration for a capability it does not actually support. Tests below pin
this contract.
"""

from __future__ import annotations

import dataclasses

import pytest

from hostlens.agent.backend import BackendCapabilities


def test_fields_count_is_exactly_seven_with_expected_names() -> None:
    """The field set is locked to exactly seven (``按需扩展`` discipline).

    Drift here is a spec-level event — both the dataclass declaration and
    every backend ``capabilities = BackendCapabilities(...)`` site must be
    updated together.
    """

    fields = dataclasses.fields(BackendCapabilities)
    assert len(fields) == 7
    assert [f.name for f in fields] == [
        "prompt_caching",
        "tool_use",
        "structured_output",
        "parallel_tool_use",
        "extended_thinking",
        "vision",
        "streaming",
    ]


def test_no_field_has_a_default_value() -> None:
    """Every field must be explicitly passed at construction time.

    Defaults would silently let a backend declare ``streaming=False``
    without thinking about it; the no-defaults rule keeps capability
    declarations a conscious decision per backend.
    """

    for field in dataclasses.fields(BackendCapabilities):
        assert field.default is dataclasses.MISSING, f"{field.name} must not have a default value"
        assert field.default_factory is dataclasses.MISSING, (
            f"{field.name} must not have a default_factory"
        )


def test_construction_without_arguments_raises_type_error() -> None:
    """Calling ``BackendCapabilities()`` MUST raise — all fields required."""

    with pytest.raises(TypeError):
        BackendCapabilities()  # type: ignore[call-arg]


def test_instances_are_frozen() -> None:
    """Frozen dataclass — direct attribute mutation MUST raise.

    Capability declarations are part of a backend's static contract; allowing
    mutation would let a test mutate a backend's declared capabilities at
    runtime and hide a real capability-gate bug.
    """

    caps = BackendCapabilities(
        prompt_caching=True,
        tool_use=True,
        structured_output=True,
        parallel_tool_use=True,
        extended_thinking=False,
        vision=True,
        streaming=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.prompt_caching = False  # type: ignore[misc]
