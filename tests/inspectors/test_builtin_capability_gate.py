"""Class-lock for the lazy-capability preflight trap (Authoring Contract rule 9).

`InspectorRunner` preflight checks `requires_capabilities` (step 2) **before**
any `exec` / binary probe (step 5). But `docker_cli` and `systemd` are added to
`LocalTarget` / `SSHTarget` only **lazily** — after the first `exec`, via
`_probe_capabilities`. So a builtin that gates on a lazily-probed capability
fails preflight with `requires_unmet` on a perfectly capable host and never
runs (and snapshot tests miss it, because the recorder warms the probe first).

The statically-present, preflight-safe capabilities are exactly the ones a
freshly constructed target already holds:
  - `LocalTarget`: {shell, file_read}
  - `SSHTarget`:   {ssh, shell, file_read}
i.e. the union {shell, file_read, ssh}. Every other enum value
(`docker_cli`, `systemd`) is lazily probed and MUST be gated via
`requires_binaries:` instead (rule 9), never via `requires_capabilities:`.

This test scans every builtin manifest and fails if any declares a
non-static (lazily-probed) capability in `requires_capabilities` — locking
docker, systemd, and any future lazy-capability inspector against the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.inspectors.loader import load_manifest

# Capabilities present at target construction (before any exec). Keep in sync
# with LocalTarget.__init__ / SSHTarget.__init__; anything outside this set is
# lazily probed (see LocalTarget._probe_capabilities) and is unsafe to require.
_STATICALLY_PRESENT_CAPABILITIES = {"shell", "file_read", "ssh"}

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"

_BUILTIN_MANIFESTS = sorted(p for p in _BUILTIN_DIR.rglob("*.yaml") if p.name != "hook.py")


@pytest.mark.parametrize("manifest_path", _BUILTIN_MANIFESTS, ids=lambda p: p.stem)
def test_builtin_requires_only_static_capabilities(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    declared = set(manifest.requires_capabilities)
    lazily_probed = declared - _STATICALLY_PRESENT_CAPABILITIES
    assert not lazily_probed, (
        f"{manifest.name} requires lazily-probed capabilities {sorted(lazily_probed)} "
        f"in requires_capabilities; preflight checks capabilities before any exec, so "
        f"this fails on a capable host with requires_unmet. Gate on requires_binaries "
        f"instead (Authoring Contract rule 9)."
    )


def test_at_least_one_manifest_scanned() -> None:
    # Guard against a glob that silently matches nothing (vacuous parametrize).
    assert len(_BUILTIN_MANIFESTS) >= 12, _BUILTIN_MANIFESTS
