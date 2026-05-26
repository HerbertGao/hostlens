"""Verify ``import hostlens.reporting`` is side-effect free.

Spec: ``openspec/changes/add-report-data-model/specs/report-data-model/spec.md``
+ ``src/hostlens/reporting/__init__.py`` docstring promise: "no IO, no
registry assembly, no ``model_rebuild`` calls".

This guards a subtle invariant: every entrypoint that imports
``hostlens.reporting`` (CLI, MCP server, tests, future Notifier) must
not pay an IO or registry-warmup cost. If anyone adds an ``open(...)``
or ``yaml.safe_load(...)`` to ``reporting/__init__.py`` or any module it
eagerly imports, this test fails.

Implementation: drop the cached ``hostlens.reporting`` (and submodules)
from ``sys.modules`` then re-import while ``builtins.open`` is patched
to raise. ``.py`` source loads do **not** go through ``builtins.open``
(they use the import-system ``SourceFileLoader``), so this assertion is
specific to user-level file IO.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def _reset_reporting_modules() -> Iterator[None]:
    """Drop hostlens.reporting + submodules from sys.modules around the test."""

    prefixes = (
        "hostlens.reporting",
        "hostlens.reporting.models",
        "hostlens.reporting._redact",
        "hostlens.reporting.render_markdown",
        "hostlens.reporting.render_json",
    )
    saved = {name: sys.modules.pop(name, None) for name in prefixes}
    try:
        yield
    finally:
        # Restore originals so other tests see the cached modules.
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod


def test_import_hostlens_reporting_does_not_open_files(
    _reset_reporting_modules: None,
) -> None:
    """Importing the package must not trigger ``builtins.open``.

    Source loading uses ``importlib``'s ``SourceFileLoader`` (NOT
    ``builtins.open``), so patching ``open`` to raise is safe — only
    user-level IO would fail.
    """

    original_open = builtins.open

    def _raise_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"builtins.open called during `import hostlens.reporting`: {args!r}")

    with patch.object(builtins, "open", _raise_open):
        mod = importlib.import_module("hostlens.reporting")

    # Sanity: re-import returns a real module with the documented exports.
    assert mod.__name__ == "hostlens.reporting"
    assert hasattr(mod, "Report")
    assert hasattr(mod, "render_markdown")
    assert hasattr(mod, "render_json")

    # Restoration sanity (with-block exits already restored, but keep
    # an explicit check to make the contract obvious in the test body).
    assert builtins.open is original_open
