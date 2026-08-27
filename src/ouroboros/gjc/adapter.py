"""GJC CLI probing, MCP registration, and endpoint validation.

This adapter owns GJC host interactions. It may invoke the GJC CLI and the
registered MCP endpoint, but it delegates durable config ownership checks and
compare-and-swap removal to :mod:`ouroboros.gjc.mcp`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import json
import subprocess

from ouroboros.gjc.mcp import (
    MCP_SHARING,
    MCP_TIMEOUT,
    gjc_mcp_entry_config,
    gjc_mcp_entry_generation,
    gjc_mcp_registration_lock,
    is_active_gjc_mcp_entry,
    is_setup_managed_gjc_mcp_entry,
    persisted_gjc_mcp_entry,
    remove_persisted_gjc_mcp_server_locked,
)
from ouroboros.gjc.paths import gjc_mcp_bridge_config_path


def _print_error(message: str) -> None:
    from ouroboros.cli.formatters.panels import print_error

    print_error(message)


def _print_info(message: str) -> None:
    from ouroboros.cli.formatters.panels import print_info

    print_info(message)


def _print_success(message: str) -> None:
    from ouroboros.cli.formatters.panels import print_success

    print_success(message)


def _print_warning(message: str) -> None:
    from ouroboros.cli.formatters.panels import print_warning

    print_warning(message)


async def _probe_endpoint(command: str, args: Sequence[str]) -> None:
    from ouroboros.cli.stdio_mcp_probe import probe_stdio_mcp_tool

    await probe_stdio_mcp_tool(
        command,
        tuple(args),
        {"OUROBOROS_MCP_CONFIG": str(gjc_mcp_bridge_config_path())},
        tool_name="ouroboros_query_events",
        tool_arguments={"limit": 1},
    )


_GJC_MCP_HELP_MARKERS = ("--sharing=<value>", "ordinary standalone sessions")


def gjc_native_mcp_autoload_support(
    gjc_path: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool | None:
    """Probe whether this GJC host autoloads ordinary MCP registrations."""
    run_command = run_command or subprocess.run
    try:
        result = run_command(
            [gjc_path, "mcp", "add", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print_warning(f"Could not inspect the GJC MCP activation contract: {exc}")
        return None
    if result.returncode != 0:
        _print_warning(
            f"Could not inspect the GJC MCP activation contract: {result.stderr.strip()}"
        )
        return None
    help_text = f"{result.stdout}\n{result.stderr}"
    return all(marker in help_text for marker in _GJC_MCP_HELP_MARKERS)


def _listed_gjc_mcp_entry(
    gjc_path: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[bool, dict[str, object] | None]:
    """Read GJC's Ouroboros MCP entry without conflating absence with failure."""
    try:
        listed = run_command(
            [gjc_path, "mcp", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print_warning(f"Could not inspect GJC MCP registrations: {exc}")
        return False, None
    if listed.returncode != 0:
        _print_warning(f"Could not inspect GJC MCP registrations: {listed.stderr.strip()}")
        return False, None
    try:
        payload = json.loads(listed.stdout)
    except json.JSONDecodeError:
        _print_warning("GJC MCP list returned malformed JSON; leaving registrations untouched.")
        return False, None
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, list):
        _print_warning("GJC MCP list returned an invalid server collection.")
        return False, None
    return True, next(
        (
            entry
            for entry in servers
            if isinstance(entry, dict) and entry.get("name") == "ouroboros"
        ),
        None,
    )


def verify_gjc_mcp_endpoint(command: str, args: Sequence[str]) -> bool:
    """Initialize the exact endpoint and call one read-only Ouroboros tool."""
    try:
        asyncio.run(_probe_endpoint(command, args))
    except Exception as exc:
        _print_warning(f"GJC Ouroboros MCP endpoint health check failed: {exc}")
        return False
    return True


def register_gjc_mcp_server(
    gjc_path: str,
    *,
    detected: dict[str, object] | None = None,
    detect_mcp_entry: Callable[..., dict[str, object] | None] | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    assume_registration_lock: bool = False,
) -> bool:
    """Register and validate the isolated Ouroboros MCP server through GJC."""
    run_command = run_command or subprocess.run
    if detected is None and detect_mcp_entry is not None:
        detected = detect_mcp_entry(package_spec="ouroboros-ai[mcp]")
    if detected is None:
        _print_error(
            "GJC setup requires an isolated MCP 2 launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False
    if assume_registration_lock:
        return _register_gjc_mcp_server_locked(
            gjc_path,
            detected=detected,
            run_command=run_command,
        )
    try:
        with gjc_mcp_registration_lock():
            return _register_gjc_mcp_server_locked(
                gjc_path,
                detected=detected,
                run_command=run_command,
            )
    except OSError as exc:
        _print_warning(f"Could not serialize the GJC MCP registration operation: {exc}")
        return False


def _register_gjc_mcp_server_locked(
    gjc_path: str,
    *,
    detected: dict[str, object],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    """Validate or add the registration; the caller holds the registration lock."""
    listed_ok, existing = _listed_gjc_mcp_entry(gjc_path, run_command)
    if not listed_ok:
        return False
    if existing is not None:
        persisted = persisted_gjc_mcp_entry()
        if not (
            is_setup_managed_gjc_mcp_entry(existing, allow_redacted_env=True)
            and is_setup_managed_gjc_mcp_entry(persisted)
        ):
            _print_error(
                "GJC already has an MCP server named 'ouroboros' that is not the "
                "complete setup-owned registration. Preserved it, but native "
                "Ouroboros activation cannot be verified."
            )
            return False
        if not is_active_gjc_mcp_entry(existing):
            _print_error(
                "The existing Ouroboros MCP server is not autoloaded by GJC; "
                "preserved it and kept the legacy route intact."
            )
            return False
        config = gjc_mcp_entry_config(existing)
        command = config.get("command") if config else None
        args = config.get("args") if config else None
        if not (
            isinstance(command, str)
            and isinstance(args, list)
            and all(isinstance(arg, str) for arg in args)
            and verify_gjc_mcp_endpoint(command, args)
        ):
            _print_error("The existing Ouroboros MCP endpoint failed execution validation.")
            return False
        _print_info("Ouroboros MCP server in GJC is active and execution-validated.")
        return True

    command = detected.get("command")
    raw_args = detected.get("args")
    if (
        not isinstance(command, str)
        or not isinstance(raw_args, list)
        or not all(isinstance(arg, str) for arg in raw_args)
    ):
        _print_warning("Detected Ouroboros MCP launcher is invalid; GJC registration skipped.")
        return False
    return _add_gjc_mcp_server_locked(
        gjc_path,
        command=command,
        raw_args=raw_args,
        run_command=run_command,
    )


def _add_gjc_mcp_server_locked(
    gjc_path: str,
    *,
    command: str,
    raw_args: list[str],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    """Add and validate a registration while holding the registration lock."""
    pre_add_generation = gjc_mcp_entry_generation(persisted_gjc_mcp_entry())

    def _cleanup_failed_add() -> None:
        observed = persisted_gjc_mcp_entry()
        observed_generation = gjc_mcp_entry_generation(observed)
        if observed_generation is None or observed_generation == pre_add_generation:
            return
        if is_setup_managed_gjc_mcp_entry(observed):
            remove_persisted_gjc_mcp_server_locked(expected_entry_generation=observed_generation)

    server_args = [*raw_args, "--runtime", "gjc"]
    add_command = [
        gjc_path,
        "mcp",
        "add",
        "ouroboros",
        "--command",
        command,
        *(f"--arg={arg}" for arg in server_args),
        f"--env=OUROBOROS_MCP_CONFIG={gjc_mcp_bridge_config_path()}",
        "--sharing",
        MCP_SHARING,
        "--timeout",
        str(MCP_TIMEOUT),
        "--json",
    ]
    try:
        added = run_command(
            add_command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _cleanup_failed_add()
        _print_warning(f"Could not register Ouroboros MCP server in GJC: {exc}")
        return False
    if added.returncode != 0:
        _cleanup_failed_add()
        _print_warning(f"Could not register Ouroboros MCP server in GJC: {added.stderr.strip()}")
        return False
    try:
        add_payload = json.loads(added.stdout)
    except json.JSONDecodeError:
        _cleanup_failed_add()
        _print_warning("GJC MCP add returned malformed JSON; activation cannot be owned safely.")
        return False

    persisted = persisted_gjc_mcp_entry()
    persisted_generation = gjc_mcp_entry_generation(persisted)
    created_here = (
        is_setup_managed_gjc_mcp_entry(persisted) and persisted_generation != pre_add_generation
    )
    receipt_matches_request = (
        isinstance(add_payload, dict)
        and add_payload.get("action") == "add"
        and add_payload.get("name") == "ouroboros"
    )
    if not receipt_matches_request:
        if created_here:
            remove_persisted_gjc_mcp_server_locked(expected_entry_generation=persisted_generation)
        _print_warning("GJC did not report adding the requested Ouroboros MCP registration.")
        return False

    validated_ok, validated = _listed_gjc_mcp_entry(gjc_path, run_command)
    config = gjc_mcp_entry_config(validated)
    validated_command = config.get("command") if config else None
    validated_args = config.get("args") if config else None
    activation_valid = (
        validated_ok
        and is_setup_managed_gjc_mcp_entry(validated, allow_redacted_env=True)
        and is_setup_managed_gjc_mcp_entry(persisted)
        and is_active_gjc_mcp_entry(validated)
        and isinstance(validated_command, str)
        and isinstance(validated_args, list)
        and all(isinstance(arg, str) for arg in validated_args)
        and verify_gjc_mcp_endpoint(validated_command, validated_args)
    )
    if not activation_valid:
        if created_here and not remove_persisted_gjc_mcp_server_locked(
            expected_entry_generation=persisted_generation
        ):
            _print_warning("Could not remove the unvalidated GJC MCP registration.")
        return False
    _print_success("Registered and execution-validated Ouroboros MCP server in GJC.")
    return True


__all__ = [
    "gjc_native_mcp_autoload_support",
    "register_gjc_mcp_server",
    "verify_gjc_mcp_endpoint",
]
