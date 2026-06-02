"""Tests for the per-run `FindingStore` (task 2.1).

(a) seed + append + resolve, and that each instance is independent (per-run,
    not module-global).
(b) the severity-collision case: two findings with the same
    (inspector, version, message) but different severities get the **same**
    real id; the store must give them two distinct labels that both resolve to
    that one id without overwriting each other.
"""

from __future__ import annotations

from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.tools.finding_store import FindingStore


def _stamped(message: str, severity: str = "warning") -> Finding:
    fid = compute_finding_id("linux.load", "1.0.0", message)
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        message=message,
        id=fid,
        inspector_name="linux.load",
        inspector_version="1.0.0",
    )


def test_seed_assigns_sequential_labels() -> None:
    store = FindingStore()
    a, b = _stamped("alpha"), _stamped("beta")
    labels = store.seed([a, b])
    assert labels == ["F1", "F2"]
    assert store.resolve_label("F1") == a.id
    assert store.resolve_label("F2") == b.id


def test_append_allocates_new_unique_label() -> None:
    store = FindingStore()
    store.seed([_stamped("alpha")])
    new = _stamped("gamma")
    label = store.append(new)
    assert label == "F2"
    assert store.resolve_label("F2") == new.id


def test_resolve_unknown_label_returns_none() -> None:
    store = FindingStore()
    store.seed([_stamped("alpha")])
    assert store.resolve_label("F9") is None
    assert store.contains("F1") is True
    assert store.contains("F9") is False


def test_snapshot_is_label_ordered_full_set() -> None:
    store = FindingStore()
    a, b = _stamped("alpha"), _stamped("beta")
    store.seed([a])
    store.append(b)
    snap = store.snapshot()
    assert [f.message for f in snap] == ["alpha", "beta"]


def test_instances_are_independent_not_module_global() -> None:
    store_one = FindingStore()
    store_two = FindingStore()
    store_one.seed([_stamped("alpha")])
    # A second, independently-constructed store shares no state with the first.
    assert store_two.contains("F1") is False
    assert store_one.contains("F1") is True
    # And a fresh store starts labelling from F1 again.
    label = store_two.seed([_stamped("delta")])
    assert label == ["F1"]


def test_severity_collision_two_labels_one_real_id_no_overwrite() -> None:
    """compute_finding_id excludes severity, so same (inspector, version,
    message) with different severities collides on a single real id. The store
    must keep both as distinct labels that both resolve to that id."""
    warning = _stamped("disk pressure", severity="warning")
    critical = _stamped("disk pressure", severity="critical")
    # Precondition: the two findings genuinely share one real id.
    assert warning.id == critical.id

    store = FindingStore()
    labels = store.seed([warning, critical])
    assert labels == ["F1", "F2"]

    # Both labels resolve to the same real id — neither overwrote the other.
    assert store.resolve_label("F1") == warning.id
    assert store.resolve_label("F2") == critical.id
    assert store.resolve_label("F1") == store.resolve_label("F2")

    # Both stamped findings survive in the snapshot (distinct objects).
    snap = store.snapshot()
    assert len(snap) == 2
    assert {f.severity for f in snap} == {"warning", "critical"}
