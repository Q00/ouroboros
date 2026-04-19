"""Mechanical detect command for Ouroboros.

Runs a single AI call to inspect a project and author
``.ouroboros/mechanical.toml``. Stage 1 (lint/build/test/static) then reads
that file deterministically instead of guessing from hardcoded language
presets.

Usage:
    ouroboros detect                  # detect in current directory
    ouroboros detect /path/to/repo    # detect in explicit directory
    ouroboros detect --force          # overwrite an existing mechanical.toml
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.evaluation.detector import (
    ensure_mechanical_toml,
    has_mechanical_toml,
    toml_path,
)
from ouroboros.providers.factory import create_llm_adapter

app = typer.Typer(
    name="detect",
    help="Generate .ouroboros/mechanical.toml via one AI call.",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def detect(
    path: Annotated[
        Path | None,
        typer.Argument(help="Project directory to inspect. Defaults to cwd."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Re-detect and overwrite an existing .ouroboros/mechanical.toml.",
        ),
    ] = False,
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="LLM backend override (claude, codex, gemini, opencode, litellm).",
        ),
    ] = None,
) -> None:
    """Inspect the project and write mechanical.toml with validated commands."""
    working_dir = (path or Path.cwd()).resolve()
    if not working_dir.is_dir():
        print_error(f"Not a directory: {working_dir}")
        raise typer.Exit(code=2)

    target = toml_path(working_dir)
    if has_mechanical_toml(working_dir) and not force:
        print_info(f"Already present: {target} (use --force to re-detect)")
        raise typer.Exit(code=0)

    try:
        adapter = create_llm_adapter(backend=backend, max_turns=1)
    except Exception as exc:  # noqa: BLE001 — surface any factory failure to user
        print_error(f"Could not initialize LLM adapter: {exc}")
        raise typer.Exit(code=1) from exc

    existed_before = has_mechanical_toml(working_dir)
    ok = asyncio.run(ensure_mechanical_toml(working_dir, adapter, backend=backend, force=force))
    if not ok:
        if existed_before and force:
            print_warning(
                "Detector could not propose any verifiable commands. "
                f"Existing {target} was left untouched."
            )
        else:
            print_warning(
                "Detector could not propose any verifiable commands. "
                "Stage 1 will skip gracefully until mechanical.toml is authored."
            )
        raise typer.Exit(code=1)

    print_success(f"Wrote {target}")
    print_info("Edit this file to customize Stage 1 commands.")
