"""Explicit Disposable Memory artifact inspection and pruning commands."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import re
from typing import Annotated

import typer

from ouroboros.persistence.artifact_store import (
    ArtifactStore,
    ArtifactStoreError,
)

app = typer.Typer(
    name="artifacts",
    help="Fetch, replay, and conservatively prune Disposable Memory artifacts.",
    no_args_is_help=True,
)

_TTL_PATTERN = re.compile(r"^(?P<amount>[0-9]+)(?P<unit>[smhdw])$")
_TTL_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_ttl(value: str) -> timedelta:
    """Parse a compact non-negative TTL such as ``30d`` or ``12h``."""
    match = _TTL_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("ttl must be a non-negative duration such as 30d, 12h, or 90m")
    seconds = int(match.group("amount")) * _TTL_SECONDS[match.group("unit")]
    try:
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("ttl is too large") from exc


def _store(project_dir: Path) -> ArtifactStore:
    return ArtifactStore.for_project(project_dir)


@app.command("prune")
def prune(
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            help="Project containing .ouroboros/artifacts.",
            file_okay=False,
            dir_okay=True,
        ),
    ] = Path("."),
    ttl: Annotated[
        str,
        typer.Option("--ttl", help="Minimum artifact age; default 90d."),
    ] = "90d",
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write tombstones and delete listed bodies."),
    ] = False,
    allow_replay_tombstone: Annotated[
        bool,
        typer.Option(
            "--allow-replay-tombstone",
            help="Allow pruning inactive contracts still inside replay retention.",
        ),
    ] = False,
) -> None:
    """List prune candidates by default; deletion requires ``--apply``."""
    try:
        report = _store(project_dir).prune(
            ttl=parse_ttl(ttl),
            apply=apply,
            allow_replay_tombstone=allow_replay_tombstone,
        )
    except (ArtifactStoreError, OSError, ValueError) as exc:
        typer.echo(f"Artifact prune failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not report.candidates:
        typer.echo("No artifact bodies are eligible for pruning.")
        return
    verb = "removed" if apply else "would remove"
    for candidate in report.candidates:
        typer.echo(
            f"{verb} {candidate.contract_id} {candidate.body_bytes}B reason={candidate.reason}"
        )
    if apply:
        typer.echo(
            f"Removed {len(report.removed_contract_ids)} artifact body(s), "
            f"{report.removed_bytes} byte(s)."
        )
    else:
        typer.echo("Dry run only. Re-run with --apply to tombstone and delete these bodies.")


def _print_contract_body(project_dir: Path, contract_id: str, *, replay: bool) -> None:
    try:
        store = _store(project_dir)
        fetched = store.replay(contract_id) if replay else store.fetch(contract_id)
    except (ArtifactStoreError, OSError, ValueError) as exc:
        typer.echo(f"Artifact read failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(fetched.body, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("fetch")
def fetch(
    contract_id: Annotated[str, typer.Argument(help="Contract id the artifact is bound to.")],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", file_okay=False, dir_okay=True),
    ] = Path("."),
) -> None:
    """Explicitly fetch one artifact body."""
    _print_contract_body(project_dir, contract_id, replay=False)


@app.command("replay")
def replay(
    contract_id: Annotated[str, typer.Argument(help="Contract id to replay from storage.")],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", file_okay=False, dir_okay=True),
    ] = Path("."),
) -> None:
    """Replay by reading the stored body; this command never re-executes work."""
    _print_contract_body(project_dir, contract_id, replay=True)


__all__ = ["app", "parse_ttl"]
