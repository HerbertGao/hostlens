"""Tests for ``scripts/cassette_lint.py`` duplicate request-key detection.

Covers spec §需求:`cassette_lint.py` secret-scan 必须检测同文件内重复 request-key:
``RecordingBackend`` overwrites and never emits duplicates, but a hand-written
or mis-edited cassette can repeat a request-key within one file, which makes
``PlaybackBackend`` silently serve only the first matching record. The lint
must reject that (exit 1) while leaving genuine multi-turn scenarios (distinct
keys per turn) untouched.
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
    return subprocess.run(
        [sys.executable, str(LINT_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_cassette(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    cassette = cassette_dir / "synthetic.jsonl"
    cassette.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return cassette_dir


def test_scan_rejects_duplicate_request_key(tmp_path: Path) -> None:
    """Two records with identical canonical ``request`` → exit 1."""

    request = {"model": "claude-opus-4-7", "messages": [], "tools_count": 0}
    cassette_dir = _write_cassette(
        tmp_path,
        [
            {"request": request, "response": VALID_RESPONSE},
            {"request": request, "response": VALID_RESPONSE},
        ],
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "duplicate request-key" in result.stderr
    assert "synthetic.jsonl" in result.stderr


def test_scan_passes_distinct_multi_turn_keys(tmp_path: Path) -> None:
    """A normal multi-turn scenario (messages grow per turn) → exit 0."""

    cassette_dir = _write_cassette(
        tmp_path,
        [
            {
                "request": {
                    "model": "claude-opus-4-7",
                    "messages": [{"role": "user", "content": "check health"}],
                    "tools_count": 1,
                },
                "response": VALID_RESPONSE,
            },
            {
                "request": {
                    "model": "claude-opus-4-7",
                    "messages": [
                        {"role": "user", "content": "check health"},
                        {"role": "assistant", "content": "running"},
                    ],
                    "tools_count": 1,
                },
                "response": VALID_RESPONSE,
            },
        ],
    )
    result = _run_lint(["--cassette-dir", str(cassette_dir)])
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
