"""Unit tests for ``guard_record_targets`` (tests/support, group D §4.6).

Covers spec §需求:record 模式必须由 fixture 强制在装配层拒绝真实 target — the
helper-layer classification: ssh/docker/k8s always real; bare local real; a
local target tagged ``cassette-synthetic`` synthetic; ``allow_real=True``
bypasses the gate. Real registries are assembled via
``build_registry_from_config`` so the ``TargetEntry.tags`` contract is
exercised end-to-end.
"""

from __future__ import annotations

import pytest

from hostlens.core.config import Settings
from hostlens.targets.config import LocalEntry, SSHEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config

from .cassette_recording import guard_record_targets


def _registry(config: TargetsConfig) -> object:
    return build_registry_from_config(config, Settings())


def test_ssh_target_rejected_without_leaking_host() -> None:
    registry = _registry(
        TargetsConfig(
            version="1",
            targets=[
                SSHEntry(
                    name="prod-db",
                    type="ssh",
                    host="secret-host.internal",
                    user="rootuser",
                )
            ],
        )
    )

    with pytest.raises(RuntimeError) as exc_info:
        guard_record_targets(registry, allow_real=False)  # type: ignore[arg-type]

    message = str(exc_info.value)
    assert "HOSTLENS_ALLOW_REAL_TARGET_RECORD=1" in message
    assert "secret-host.internal" not in message
    assert "rootuser" not in message


def test_synthetic_local_allowed() -> None:
    registry = _registry(
        TargetsConfig(
            version="1",
            targets=[LocalEntry(name="synthetic", type="local", tags=["cassette-synthetic"])],
        )
    )

    # Does not raise.
    guard_record_targets(registry, allow_real=False)  # type: ignore[arg-type]


def test_bare_local_rejected() -> None:
    registry = _registry(
        TargetsConfig(
            version="1",
            targets=[LocalEntry(name="bare", type="local")],
        )
    )

    with pytest.raises(RuntimeError, match="real target"):
        guard_record_targets(registry, allow_real=False)  # type: ignore[arg-type]


def test_allow_real_bypasses_gate() -> None:
    registry = _registry(
        TargetsConfig(
            version="1",
            targets=[SSHEntry(name="prod", type="ssh", host="h.internal", user="u")],
        )
    )

    # Does not raise when explicitly allowed.
    guard_record_targets(registry, allow_real=True)  # type: ignore[arg-type]
