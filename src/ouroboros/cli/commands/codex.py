"""Codex CLI integration helper commands."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error, print_success, print_warning
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


@app.command("doctor")
def doctor(
    codex_dir: Annotated[
        Path | None,
        typer.Option(
            "--codex-dir",
            help="Codex configuration directory to inspect. Defaults to ~/.codex.",
        ),
    ] = None,
) -> None:
    """Verify installed Codex artifacts can route ``ooo auto`` to Ouroboros."""
    resolved_codex_dir = codex_dir or Path.home() / ".codex"
    failures = _check_auto_dispatch_surface(resolved_codex_dir)

    if failures:
        print_error(
            "Codex ooo auto dispatch: BROKEN\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n\nRun `ouroboros codex refresh` and ensure the `ouroboros` MCP server is enabled.",
            title="Codex Doctor",
        )
        raise typer.Exit(1)

    print_success(
        "Codex ooo auto dispatch: OK\n"
        "- rule maps `ooo auto` to `ouroboros_auto`\n"
        "- auto skill declares MCP dispatch through `ouroboros_auto`\n"
        "- Codex config contains an `ouroboros` MCP server entry",
        title="Codex Doctor",
    )


def _check_auto_dispatch_surface(codex_dir: Path) -> list[str]:
    """Return configuration failures that can silently bypass ``ooo auto`` dispatch."""
    failures: list[str] = []

    rules_path = codex_dir / "rules" / "ouroboros.md"
    if not rules_path.is_file():
        failures.append(f"missing Codex rules file: {rules_path}")
    else:
        rules = rules_path.read_text(encoding="utf-8")
        if "`ooo auto" not in rules or "ouroboros_auto" not in rules:
            failures.append("Codex rules do not map `ooo auto` to `ouroboros_auto`")
        if "manual" not in rules.lower() or "unavailable" not in rules.lower():
            failures.append("Codex rules do not describe fail-closed behavior for `ooo auto`")

    skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
    if not skill_path.is_file():
        failures.append(f"missing auto skill file: {skill_path}")
    else:
        skill = skill_path.read_text(encoding="utf-8")
        if "mcp_tool: ouroboros_auto" not in skill:
            failures.append("auto skill does not declare `mcp_tool: ouroboros_auto`")
        if "manual" not in skill.lower() or "unavailable" not in skill.lower():
            failures.append("auto skill does not forbid manual fallback when dispatch is unavailable")

    config_path = codex_dir / "config.toml"
    if not config_path.is_file():
        failures.append(f"missing Codex config file: {config_path}")
        return failures

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        failures.append(f"Codex config is not valid TOML: {exc}")
        return failures

    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        failures.append("Codex config does not contain an [mcp_servers] table")
        return failures

    ouroboros_entry = mcp_servers.get("ouroboros")
    if not isinstance(ouroboros_entry, dict):
        failures.append("Codex config does not contain [mcp_servers.ouroboros]")
        return failures

    command = str(ouroboros_entry.get("command", ""))
    args = ouroboros_entry.get("args")
    args_text = " ".join(str(arg) for arg in args) if isinstance(args, list) else ""
    if not command:
        failures.append("[mcp_servers.ouroboros] is missing `command`")
    if "ouroboros" not in f"{command} {args_text}" or "mcp" not in args_text:
        failures.append("[mcp_servers.ouroboros] does not appear to start `ouroboros mcp serve`")

    if failures:
        print_warning(
            "Detected a Codex surface where `ooo auto` may be interpreted as normal text.",
            title="Codex Doctor",
        )

    return failures
