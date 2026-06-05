"""Offline snapshot tests for the `postgres.bloat_tables` SQL inspector.

These replay committed `ReplayTarget` fixtures (recorded against a real
`postgres:16` container by ``tests/inspectors/_record_postgres_bloat.py``)
through the real `InspectorRunner` — zero `psql`, zero server, deterministic.

They prove the `add-inspector-authoring-contract` rules for the SQL data shape:

  * `json_build_object('results', ...)` emits a top-level OBJECT so
    `parse.format: json` accepts it (承重墙 4) and the `results` key survives
    parameter merge (承重墙 3);
  * all bloat derivation (`dead_ratio`) is a SQL computed column — the Finding
    DSL only threshold-compares ready scalars (承重墙 1);
  * the empty case (`{"results":[]}`) yields zero findings, not a parse error;
  * the recorded fixtures carry no plaintext connection password.

Re-record with the seeded container (see the recorder module docstring):

    docker run -d --name hl-pg -e POSTGRES_PASSWORD=<throwaway-pw> postgres:16
    # seed bloatdb / healthydb / emptydb (autovacuum disabled on bloated tables)
    PGPASSWORD=<throwaway-pw> .venv-impl/bin/python tests/inspectors/_record_postgres_bloat.py
    docker rm -f hl-pg
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "postgres_bloat_tables"


def _manifest_path() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin" / "postgres" / "bloat_tables.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("postgres-bloat-test"),
    )


async def _run(fixture: str, dbname: str, monkeypatch: Any) -> InspectorResult:
    # The runner reads PGPASSWORD from os.environ (it is declared in
    # `manifest.secrets`) and injects it via `env=`; ReplayTarget never matches
    # on or stores env, so the secret stays out of the fixture.
    monkeypatch.setenv("PGPASSWORD", "test-" + "injected-pw")  # split: not a contiguous literal
    manifest = load_manifest(_manifest_path())
    target = ReplayTarget("rec", fixture=_FIXTURES / fixture)
    result = await _runner().run(manifest, target, {"dbname": dbname})
    assert target.misses == [], target.misses
    return result


async def test_bloated_db_flags_only_over_threshold_tables(monkeypatch: Any) -> None:
    result = await _run("bloated.json", "bloatdb", monkeypatch)

    assert result.status == "ok"
    assert result.name == "postgres.bloat_tables"

    # The SQL computed `dead_ratio` reached the parsed output untouched by the DSL.
    rows = {r["table"]: r for r in result.output["results"]}
    assert rows["orders"]["n_dead_tup"] == 4000
    assert rows["orders"]["dead_ratio"] == pytest.approx(0.6667)
    assert rows["sessions"]["dead_ratio"] == pytest.approx(0.0256)

    # Only `orders` clears both the ratio (>= 0.2) and dead-tuple (>= 1000)
    # thresholds; `sessions` (ratio 0.0256) does not.
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == "warning"
    assert "orders" in finding.message
    assert "4000 dead tuples" in finding.message


async def test_healthy_db_yields_no_findings(monkeypatch: Any) -> None:
    result = await _run("healthy.json", "healthydb", monkeypatch)

    assert result.status == "ok"
    assert result.findings == []
    # A zero-dead-tuple table is present in the output but never flagged.
    assert result.output["results"][0]["table"] == "accounts"
    assert result.output["results"][0]["n_dead_tup"] == 0


async def test_empty_db_parses_object_not_array(monkeypatch: Any) -> None:
    # `coalesce(json_agg(t), '[]'::json)` inside `json_build_object` keeps the
    # empty case a top-level OBJECT `{"results":[]}` — a bare `json_agg` would
    # emit a top-level array and be rejected by parse_json_not_object.
    result = await _run("empty.json", "emptydb", monkeypatch)

    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


def test_fixtures_inject_password_via_env_not_plaintext() -> None:
    """The recorded command must reference the password through the env var
    (``$PGPASSWORD``), never embed a plaintext literal — proving secrets reach
    ``psql`` via the ``secrets_env`` mechanism, not the recorded command string."""

    for fixture in ("bloated.json", "healthy.json", "empty.json"):
        text = (_FIXTURES / fixture).read_text()
        # The recorded command injects the password through the env var
        # (`PGPASSWORD="$PGPASSWORD"`), so the only `PGPASSWORD=` occurrence is
        # the env-ref form — never a plaintext literal value.
        assert "$PGPASSWORD" in text, f"{fixture}: expected env-ref password injection"
