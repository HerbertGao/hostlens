"""Hostlens CLI entrypoint.

`pyproject.toml` registers `hostlens = "hostlens.cli:app"`, so the `app`
object below is what `pip install -e .` exposes as the `hostlens` shell
command. M0 ships a single subcommand, `doctor`.
"""

from __future__ import annotations

import typer

from hostlens.cli.doctor import run_doctor

__all__ = ["app"]


app = typer.Typer(
    name="hostlens",
    help="Hostlens CLI — LLM-driven server inspection agent.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode.

    Without an explicit callback, a Typer app with exactly one registered
    `@app.command` collapses into single-command mode and the subcommand
    name disappears from `--help`. This callback keeps `doctor` addressable
    as `hostlens doctor` (which the cli-foundation spec requires).
    """


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich table.",
    ),
) -> None:
    """Check local environment health (Python version, env vars, config dir)."""

    exit_code = run_doctor(json_output=json_output)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
