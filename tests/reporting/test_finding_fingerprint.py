"""Tests for the `compute_finding_id` fingerprint helper.

Covers spec §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹:

- 相同 (name, version, message) 异 severity → 同 id (severity 不参与指纹)
- 不同 message → 异 id
- 不同 inspector_version → 异 id
- None 参数被拒绝 (禁止产出 "None\\x00..." 指纹)
"""

from __future__ import annotations

import hashlib

import pytest

from hostlens.reporting.models import compute_finding_id


def test_severity_agnostic_same_inputs_same_id() -> None:
    # severity is not a parameter — the fingerprint is computed from
    # (name, version, message) only, so a finding keeps a stable id
    # across runs even when its severity changes.
    a = compute_finding_id("insp.x", "1.0", "disk 95%")
    b = compute_finding_id("insp.x", "1.0", "disk 95%")
    assert a == b


def test_different_message_different_id() -> None:
    a = compute_finding_id("insp.x", "1.0", "disk 95%")
    b = compute_finding_id("insp.x", "1.0", "disk 96%")
    assert a != b


def test_different_version_different_id() -> None:
    a = compute_finding_id("insp.x", "1.0", "disk 95%")
    b = compute_finding_id("insp.x", "1.1", "disk 95%")
    assert a != b


def test_different_name_different_id() -> None:
    a = compute_finding_id("insp.x", "1.0", "disk 95%")
    b = compute_finding_id("insp.y", "1.0", "disk 95%")
    assert a != b


def test_fingerprint_is_first_16_of_sha256() -> None:
    expected = hashlib.sha256(b"insp.x\x001.0\x00disk 95%").hexdigest()[:16]
    assert compute_finding_id("insp.x", "1.0", "disk 95%") == expected
    assert len(compute_finding_id("insp.x", "1.0", "disk 95%")) == 16


def test_none_inspector_name_raises() -> None:
    with pytest.raises(ValueError):
        compute_finding_id(None, "1.0", "x")  # type: ignore[arg-type]


def test_none_inspector_version_raises() -> None:
    with pytest.raises(ValueError):
        compute_finding_id("insp.x", None, "x")  # type: ignore[arg-type]


def test_target_name_not_in_fingerprint() -> None:
    # spec §场景:target_name 不改变 finding id — the helper takes no
    # target_name argument, so the same (name, version, message) keeps a
    # stable id no matter which target a finding originates from. This is
    # what lets per-target regression diff anchor the same check across
    # targets on the same id instead of a spurious resolved+added pair.
    a = compute_finding_id("insp.x", "1.0", "disk 95%")
    b = compute_finding_id("insp.x", "1.0", "disk 95%")
    assert a == b
