"""Read-only Workflow IR inspection commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from ouroboros.cli.formatters.panels import print_error
from ouroboros.core.seed import Seed
from ouroboros.orchestrator.workflow_ir import validate_workflow
from ouroboros.orchestrator.workflow_ir_adapter import workflow_spec_from_seed

app = typer.Typer(
    name="workflow-ir",
    help="Inspect read-only Workflow IR projections.",
    no_args_is_help=True,
)


@app.command(name="inspect")
def inspect_seed(
    seed_file: Annotated[Path, typer.Argument(help="Seed YAML file to inspect.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Project a Seed file into Workflow IR without dispatching work."""

    try:
        seed = _load_seed(seed_file)
        spec = workflow_spec_from_seed(seed)
        validation = validate_workflow(spec)
    except Exception as exc:
        print_error(f"Workflow IR inspection failed: {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "seed_file": str(seed_file),
        "workflow_spec": spec.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    typer.echo(f"WorkflowSpec: {spec.spec_id}")
    typer.echo(f"Nodes: {len(spec.nodes)}")
    typer.echo(f"Edges: {len(spec.edges)}")
    typer.echo(f"Validation: {'ok' if validation.ok else 'failed'}")


def _load_seed(seed_file: Path) -> Seed:
    try:
        raw = yaml.safe_load(seed_file.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact yaml/os subclass varies
        msg = f"failed to read seed file: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(raw, dict):
        msg = "seed file must contain a mapping"
        raise ValueError(msg)
    return Seed.model_validate(_normalize_seed_payload(raw))


def _normalize_seed_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw)
    criteria = payload.get("acceptance_criteria")
    if isinstance(criteria, list):
        payload["acceptance_criteria"] = tuple(_normalize_criterion(item) for item in criteria)
    return payload


def _normalize_criterion(item: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("criterion"), str):
        return item["criterion"]
    if isinstance(item, str):
        return item
    msg = "acceptance_criteria entries must be strings or {criterion: <text>} mappings"
    raise ValueError(msg)


__all__ = ["app"]
