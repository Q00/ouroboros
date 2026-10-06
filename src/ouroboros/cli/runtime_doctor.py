"""Minimal public app for bounded MCP runtime diagnostics."""

from __future__ import annotations

import json
import unicodedata

from rich.console import Console
from rich.markup import escape
import typer

from ouroboros.mcp.machine_runtime import RuntimeSnapshot, collect_runtime_snapshot
from ouroboros.mcp.registry_paths import diagnostic_mcp_pid_registry_dir

app = typer.Typer(add_completion=False)


def _terminal_safe_path(value: str) -> str:
    """Escape terminal control and Unicode formatting characters in a path."""
    rendered: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            codepoint = ord(character)
            if codepoint <= 0xFF:
                rendered.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                rendered.append(f"\\u{codepoint:04x}")
            else:
                rendered.append(f"\\U{codepoint:08x}")
        else:
            rendered.append(character)
    return escape("".join(rendered))


def render_runtime_snapshot(snapshot: RuntimeSnapshot, *, as_json: bool) -> None:
    """Render bounded runtime facts as JSON or terminal-safe human output."""
    if as_json:
        print(json.dumps(snapshot.to_dict(), indent=2))
        return

    console = Console(soft_wrap=True)
    console.print("[bold]Ouroboros MCP Runtime Facts[/bold]")
    path = snapshot.path
    console.print(
        f"  PATH: {escape(path.status)}; entries {path.entries_seen}/{path.entries_limit}; "
        f"truncated={path.truncated}"
    )
    if path.reason is not None:
        console.print(f"  PATH unavailable reason: {escape(path.reason)}")
    for candidate in path.candidates:
        console.print(
            f"  PATH candidate: {escape(candidate.name)} -> {_terminal_safe_path(candidate.path)} "
            f"(executable={candidate.executable})"
        )
    for name, paths in path.collisions.items():
        collision_paths = ", ".join(_terminal_safe_path(value) for value in paths)
        console.print(f"  PATH collision: {escape(name)} -> {collision_paths}")
    for probe in snapshot.loopback:
        port = "-" if probe.port is None else str(probe.port)
        reason = "" if probe.reason is None else f", {probe.reason}"
        console.print(f"  loopback {probe.family}: {probe.status} (port {port}{reason})")
    registry = snapshot.registry
    console.print(
        f"  registry: {escape(registry.status)} at {escape(registry.directory)}; "
        f"entries {registry.entries_seen}/{registry.entries_limit}; "
        f"records={len(registry.records)}; truncated={registry.truncated}"
    )
    if registry.reason is not None:
        console.print(f"  registry unavailable reason: {escape(registry.reason)}")
    for record in registry.records:
        console.print(
            f"  registry record: pid={record.pid}, name={escape(record.name)}, "
            f"size={record.size}, mtime_ns={record.mtime_ns}, "
            f"identity_verified={record.identity_verified}, liveness={escape(record.liveness)}"
        )


@app.command("doctor-runtime")
def doctor_runtime(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Show bounded, read-only runtime facts for local MCP diagnostics."""
    try:
        registry_dir = diagnostic_mcp_pid_registry_dir()
    except (KeyError, OSError):
        registry_dir = None
    snapshot = collect_runtime_snapshot(registry_dir=registry_dir)
    render_runtime_snapshot(snapshot, as_json=as_json)


__all__ = ["app", "doctor_runtime", "render_runtime_snapshot"]
