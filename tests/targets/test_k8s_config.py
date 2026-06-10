"""Tests for ``K8sEntry`` config layer — schema + non-secret placeholder rejection.

Spec: ``openspec/changes/add-kubernetes-target/specs/execution-target/spec.md``
§场景:type k8s 路由到 K8sEntry / §场景:TargetEntry k8s 字段集严格 /
§场景:k8s 非 secret 字段占位被拒. No kubernetes cluster required — this module
only exercises Pydantic schema + the loader's field-name placeholder allowlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.targets.config import (
    K8sEntry,
    LocalEntry,
    load_targets_config,
)


def _write_config(tmp_path: Path, doc: object) -> Path:
    path = tmp_path / "targets.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


# ---------------------------------------------------------------------------
# Schema: routing + defaults
# ---------------------------------------------------------------------------


def test_type_k8s_routes_to_k8s_entry(tmp_path: Path) -> None:
    """Spec §场景:type k8s 路由到 K8sEntry — defaults assertions."""

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [{"name": "web-pod", "type": "k8s", "pod": "my-app"}],
        },
    )
    config = load_targets_config(path)
    [entry] = config.targets
    assert isinstance(entry, K8sEntry)
    assert entry.type == "k8s"
    assert entry.pod == "my-app"
    assert entry.namespace == "default"
    assert entry.container is None
    assert entry.kubeconfig is None
    assert entry.context is None


# ---------------------------------------------------------------------------
# Schema: field set strictness
# ---------------------------------------------------------------------------


def test_k8s_specific_field_set_is_exactly_five_fields() -> None:
    """Spec §场景:TargetEntry k8s 字段集严格 — exactly 5 k8s-specific fields."""

    k8s_specific = set(K8sEntry.model_fields.keys()) - set(LocalEntry.model_fields.keys())
    assert k8s_specific == {"pod", "namespace", "container", "kubeconfig", "context"}


def test_pod_missing_raises_validation_error() -> None:
    """Spec §场景:TargetEntry k8s 字段集严格 — pod is required."""

    with pytest.raises(ValidationError):
        K8sEntry(name="web-pod", type="k8s")  # type: ignore[call-arg]


def test_pod_empty_string_raises_validation_error() -> None:
    """Spec §场景:TargetEntry k8s 字段集严格 — pod min_length=1."""

    with pytest.raises(ValidationError):
        K8sEntry(name="web-pod", type="k8s", pod="")


def test_k8s_entry_extra_field_forbidden() -> None:
    """Spec §场景:TargetEntry k8s 字段集严格 — extra=forbid."""

    with pytest.raises(ValidationError):
        K8sEntry(
            name="web-pod",
            type="k8s",
            pod="my-app",
            image="alpine",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Placeholder rejection (field-name allowlist, before model_validate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "placeholder"),
    [
        ("pod", "${POD_VAR}"),
        ("namespace", "${NS_VAR}"),
        ("container", "${C}"),
        ("kubeconfig", "${KC}"),
        ("context", "${CTX}"),
    ],
)
def test_k8s_non_secret_field_placeholder_rejected(
    tmp_path: Path, field: str, placeholder: str
) -> None:
    """Spec §场景:k8s 非 secret 字段占位被拒 — all 5 fields are non-secret."""

    target: dict[str, object] = {"name": "web-pod", "type": "k8s", "pod": "my-app"}
    target[field] = placeholder
    path = _write_config(tmp_path, {"version": "1", "targets": [target]})
    with pytest.raises(ConfigError) as excinfo:
        load_targets_config(path)
    assert excinfo.value.kind == "env_placeholder_not_allowed_here"
