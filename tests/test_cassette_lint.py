"""Tests for ``scripts/cassette_lint.py``.

The script is the secret-leak guard for committed cassettes (spec
§需求:Backend 实现必须脱敏所有敏感字段). We exercise:

- scan mode happy-path on the real cassettes
- scan mode reject for each sensitive pattern in spec §14.4 (a-d)
- ``--check-schema-drift`` warning vs. hard-fail behavior (§14.4 e-f)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_SCRIPT = REPO_ROOT / "scripts" / "cassette_lint.py"


VALID_RESPONSE = {
    "id": "msg_test_01",
    "model": "claude-opus-4-7",
    "role": "assistant",
    "content": [{"type": "tool_use", "id": "toolu_01", "name": "list_inspectors", "input": {}}],
    "stop_reason": "tool_use",
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    },
}


def _run_lint(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the lint script via the same Python interpreter as the tests."""

    return subprocess.run(
        [sys.executable, str(LINT_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_cassette(tmp_path: Path, record: dict[str, object]) -> Path:
    """Materialize a single-line cassette file with one JSON record."""

    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    cassette = cassette_dir / "synthetic.jsonl"
    cassette.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return cassette_dir


def test_existing_cassettes_pass_scan_mode() -> None:
    """The cassettes committed under ``tests/fixtures/cassettes/`` must be clean."""

    result = _run_lint([])
    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"


def test_scan_rejects_anthropic_api_key(tmp_path: Path) -> None:
    fake_key = (
        "sk-" + "ant-" + "leakvalue123456789"
    )  # pragma: allowlist secret — fake fixture, not a real key
    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "api_key": fake_key,
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_scan_rejects_user_home_path(tmp_path: Path) -> None:
    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "snippet": "/Users/alice/.ssh/id_rsa",
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_scan_rejects_ipv4_address(tmp_path: Path) -> None:
    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "host": "10.0.0.5",
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_scan_rejects_hostname_or_fqdn(tmp_path: Path) -> None:
    """A dotted hostname / FQDN inside a cassette body must trip the lint.

    Inspector output (e.g. ``"prod-db.internal.example.com is unreachable"``)
    can leak hostnames into recorded responses; the scan rule blocks the
    commit before the secret reaches git.
    """

    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "snippet": "prod-db.internal.example.com is unreachable",
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_scan_rejects_company_suffix_hostname(tmp_path: Path) -> None:
    """Internal corporate ``*.company`` FQDNs must also trip the lint.

    Codex R2 review caught the original suffix list was too narrow and
    missed common internal domains like ``api.service.company``.
    """

    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "snippet": "api.service.company is unreachable",
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_scan_rejects_lan_suffix_hostname(tmp_path: Path) -> None:
    """``*.lan`` style internal hostnames must also trip the lint."""

    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "snippet": "node.cluster.lan timeout",
        },
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1
    assert "sensitive substring detected" in result.stderr


def test_drift_warns_without_failing(tmp_path: Path) -> None:
    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "tools_schema_hash": "abc",
        },
    )
    result = _run_lint(
        [
            "--cassette-dir",
            str(cassette_dir),
            "--check-schema-drift",
            "--current-tools-hash",
            "xyz",
        ]
    )
    assert result.returncode == 0
    assert "WARNING: tools_schema_hash drift" in result.stdout


def test_drift_requires_current_hash_flag(tmp_path: Path) -> None:
    cassette_dir = _write_cassette(
        tmp_path,
        {
            "request": {"model": "claude-opus-4-7", "messages": [], "tools_count": 0},
            "response": VALID_RESPONSE,
            "tools_schema_hash": "abc",
        },
    )
    result = _run_lint(
        [
            "--cassette-dir",
            str(cassette_dir),
            "--check-schema-drift",
        ]
    )
    assert result.returncode == 2
    assert "--current-tools-hash required" in result.stderr


def test_cassette_file_exists() -> None:
    """§14.2 — confirm the demo cassette is present in the repo."""

    cassette = REPO_ROOT / "tests" / "fixtures" / "cassettes" / "list_inspectors_demo.jsonl"
    assert cassette.is_file()
