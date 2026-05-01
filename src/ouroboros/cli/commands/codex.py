"""Codex CLI integration helper commands."""

from __future__ import annotations

from pathlib import Path

import typer

from ouroboros.cli.formatters.panels import print_error, print_success
from ouroboros.codex import install_codex_artifacts

app = typer.Typer(
    name="codex",
    help="Manage Ouroboros Codex CLI integration artifacts.",
    no_args_is_help=True,
)


@app.callback()
def codex() -> None:
    """Manage Ouroboros Codex CLI integration artifacts."""


@app.command("refresh")
def refresh() -> None:
    """Refresh Codex rules and skills without changing MCP or Ouroboros config."""
    codex_dir = Path.home() / ".codex"
    try:
        result = install_codex_artifacts(codex_dir=codex_dir, prune=False)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    print_success(f"Installed Codex rules → {result.rules_path}")
    print_success(f"Installed {len(result.skill_paths)} Codex skills → {codex_dir / 'skills'}")
