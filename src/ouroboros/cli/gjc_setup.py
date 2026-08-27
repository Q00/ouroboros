"""GJC runtime activation orchestration shared by setup, refresh, and uninstall.

This module narrates progress, invokes the GJC CLI, and sequences the
ownership-safe transaction. All path resolution, artifact rendering, and
ownership judgment lives in :mod:`ouroboros.gjc`; the generic filesystem
transaction seams (atomic writes, path snapshots) are owned by
``ouroboros setup`` and injected through :class:`GjcSetupHost`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import yaml

from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.core.fs_ownership import UnownedArtifactError, publish_owned_file
from ouroboros.gjc import (
    MCP_BRIDGE_CONFIG_CONTENT,
    MCP_SHARING,
    MCP_TIMEOUT,
    gjc_agent_dir,
    gjc_bridge_path,
    gjc_instruction_path,
    gjc_mcp_bridge_config_path,
    gjc_mcp_entry_config,
    gjc_mcp_entry_generation,
    gjc_mcp_registration_lock,
    gjc_ooo_bridge_source_text,
    install_gjc_skills,
    is_active_gjc_mcp_entry,
    is_setup_managed_gjc_bridge,
    is_setup_managed_gjc_mcp_bridge_config,
    is_setup_managed_gjc_mcp_entry,
    persisted_gjc_mcp_entry,
    remove_persisted_gjc_mcp_server,
    remove_persisted_gjc_mcp_server_locked,
    setup_owned_gjc_skill_paths,
)

_GJC_MCP_HELP_MARKERS = ("--sharing=<value>", "ordinary standalone sessions")

_PathSnapshots = tuple[tuple[Path, Any], ...]


@dataclass(frozen=True, slots=True)
class GjcSetupHost:
    """Setup-owned collaborators the GJC activation transaction runs against."""

    atomic_write_text: Callable[..., object]
    snapshot_path: Callable[..., object]
    restore_path_snapshot: Callable[..., None]
    detect_mcp_entry: Callable[..., dict[str, object] | None]
    bridge_dispatch_entry: Callable[[], tuple[str, list[str]]]


def install_gjc_skills_step() -> bool:
    """Project packaged Ouroboros skills into GJC's native user registry."""
    try:
        result = install_gjc_skills(agent_dir=gjc_agent_dir(), prune=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print_warning(f"Could not install GJC Ouroboros skills: {exc}")
        return False
    print_success(f"Installed {len(result.skill_paths)} Ouroboros skills → {result.target_root}")
    return True


def install_gjc_mcp_bridge_config() -> bool:
    """Install the isolated config only when setup owns the target path."""
    path = gjc_mcp_bridge_config_path()
    try:
        publish_owned_file(
            path,
            MCP_BRIDGE_CONFIG_CONTENT,
            is_owned=is_setup_managed_gjc_mcp_bridge_config,
            mode=0o600,
            trusted_ancestor=gjc_agent_dir(),
        )
    except UnownedArtifactError:
        print_error(
            f"Preserved user-managed GJC MCP bridge config at {path}; "
            "native activation requires an isolated setup-owned configuration."
        )
        return False
    except OSError as exc:
        print_warning(f"Could not install GJC MCP bridge config: {exc}")
        return False
    return True


def install_gjc_instruction_step() -> bool:
    """Install the always-applied routing guide without replacing operator files."""
    from ouroboros.runtime_instruction_artifacts import install_gjc_instruction_artifact

    try:
        artifact = install_gjc_instruction_artifact()
    except OSError as exc:
        print_warning(f"Could not install gjc instruction artifact: {exc}")
        return False
    print_success(f"Installed Gjc instruction guide → {artifact.path}")
    return True


def gjc_native_mcp_autoload_support(
    gjc_path: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> bool | None:
    """Probe native autoload: true/false for known contracts, None on failure."""
    try:
        result = run_command(
            [gjc_path, "mcp", "add", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print_warning(f"Could not inspect the GJC MCP activation contract: {exc}")
        return None
    if result.returncode != 0:
        print_warning(f"Could not inspect the GJC MCP activation contract: {result.stderr.strip()}")
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
        print_warning(f"Could not inspect GJC MCP registrations: {exc}")
        return False, None
    if listed.returncode != 0:
        print_warning(f"Could not inspect GJC MCP registrations: {listed.stderr.strip()}")
        return False, None
    try:
        payload = json.loads(listed.stdout)
    except json.JSONDecodeError:
        print_warning("GJC MCP list returned malformed JSON; leaving registrations untouched.")
        return False, None
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, list):
        print_warning("GJC MCP list returned an invalid server collection.")
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
    import asyncio

    from ouroboros.cli.stdio_mcp_probe import probe_stdio_mcp_tool

    try:
        asyncio.run(
            probe_stdio_mcp_tool(
                command,
                tuple(args),
                {"OUROBOROS_MCP_CONFIG": str(gjc_mcp_bridge_config_path())},
                tool_name="ouroboros_query_events",
                tool_arguments={"limit": 1},
            )
        )
    except Exception as exc:
        print_warning(f"GJC Ouroboros MCP endpoint health check failed: {exc}")
        return False
    return True


def register_gjc_mcp_server(
    gjc_path: str,
    *,
    detected: dict[str, object] | None = None,
    detect_mcp_entry: Callable[..., dict[str, object] | None] | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    registration_state: dict[str, object] | None = None,
    assume_registration_lock: bool = False,
) -> bool:
    """Register and validate the isolated Ouroboros MCP server through GJC.

    The complete validate/add/cleanup sequence runs under the cross-process
    registration lock. A caller that must extend the transaction past the
    registration itself (for example through legacy-route retirement) holds
    the lock and passes ``assume_registration_lock=True``.
    """
    run_command = run_command or subprocess.run
    if registration_state is not None:
        registration_state.update(created=False, changed=False)
    if detected is None and detect_mcp_entry is not None:
        detected = detect_mcp_entry(package_spec="ouroboros-ai[mcp]")
    if detected is None:
        print_error(
            "GJC setup requires an isolated MCP 2 launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False
    if assume_registration_lock:
        return _register_gjc_mcp_server_locked(
            gjc_path,
            detected=detected,
            run_command=run_command,
            registration_state=registration_state,
        )
    try:
        with gjc_mcp_registration_lock():
            return _register_gjc_mcp_server_locked(
                gjc_path,
                detected=detected,
                run_command=run_command,
                registration_state=registration_state,
            )
    except OSError as exc:
        print_warning(f"Could not serialize the GJC MCP registration transaction: {exc}")
        return False


def _register_gjc_mcp_server_locked(
    gjc_path: str,
    *,
    detected: dict[str, object],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    registration_state: dict[str, object] | None,
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
            print_error(
                "GJC already has an MCP server named 'ouroboros' that is not the "
                "complete setup-owned registration. Preserved it, but native "
                "Ouroboros activation cannot be verified."
            )
            return False
        if not is_active_gjc_mcp_entry(existing):
            print_error(
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
            print_error("The existing Ouroboros MCP endpoint failed execution validation.")
            return False
        print_info("Ouroboros MCP server in GJC is active and execution-validated.")
        return True
    command = detected.get("command")
    raw_args = detected.get("args")
    if (
        not isinstance(command, str)
        or not isinstance(raw_args, list)
        or not all(isinstance(arg, str) for arg in raw_args)
    ):
        print_warning("Detected Ouroboros MCP launcher is invalid; GJC registration skipped.")
        return False
    return _add_gjc_mcp_server_locked(
        gjc_path,
        command=command,
        raw_args=raw_args,
        run_command=run_command,
        registration_state=registration_state,
    )


def _add_gjc_mcp_server_locked(
    gjc_path: str,
    *,
    command: str,
    raw_args: list[str],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    registration_state: dict[str, object] | None,
) -> bool:
    """Add and validate a new registration; runs under the registration lock.

    The lock serializes every Ouroboros add/validate/cleanup transaction, so
    a setup-shaped durable entry observed during this window either predates
    the add or was written during this invocation's ``gjc mcp add`` window —
    never by a concurrent Ouroboros invocation. Non-Ouroboros writers do not
    take the lock, so cleanup never trusts a shape judgment alone: it removes
    only an entry that (a) differs from the exact pre-add generation, (b) is
    setup-shaped, and (c) still equals — under the lock, via a whole-file
    compare-and-swap — the exact generation sampled for the cleanup decision.
    A byte-identical setup-shaped registration raced in by a non-cooperating
    writer inside the add window is indistinguishable from this invocation's
    own write by construction; the generation binding confines the cleanup to
    exactly that observed generation and nothing later.
    """
    pre_add_generation = gjc_mcp_entry_generation(persisted_gjc_mcp_entry())

    def _cleanup_failed_add() -> None:
        observed = persisted_gjc_mcp_entry()
        observed_generation = gjc_mcp_entry_generation(observed)
        if observed_generation is None or observed_generation == pre_add_generation:
            return  # nothing appeared or changed during this invocation's add
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
        print_warning(f"Could not register Ouroboros MCP server in GJC: {exc}")
        return False
    if added.returncode != 0:
        _cleanup_failed_add()
        print_warning(f"Could not register Ouroboros MCP server in GJC: {added.stderr.strip()}")
        return False
    try:
        add_payload = json.loads(added.stdout)
    except json.JSONDecodeError:
        _cleanup_failed_add()
        print_warning("GJC MCP add returned malformed JSON; activation cannot be owned safely.")
        return False

    persisted = persisted_gjc_mcp_entry()
    persisted_generation = gjc_mcp_entry_generation(persisted)
    persisted_by_setup = is_setup_managed_gjc_mcp_entry(persisted)
    created_here = persisted_by_setup and persisted_generation != pre_add_generation
    if created_here and registration_state is not None:
        registration_state.update(created=True, changed=True)
        registration_state["entry_generation"] = persisted_generation
    receipt_matches_request = (
        isinstance(add_payload, dict)
        and add_payload.get("action") == "add"
        and add_payload.get("name") == "ouroboros"
    )
    if not receipt_matches_request:
        if created_here and remove_persisted_gjc_mcp_server_locked(
            expected_entry_generation=persisted_generation
        ):
            if registration_state is not None:
                registration_state.update(created=False, changed=False)
                registration_state.pop("entry_generation", None)
        print_warning("GJC did not report adding the requested Ouroboros MCP registration.")
        return False

    validated_ok, validated = _listed_gjc_mcp_entry(gjc_path, run_command)
    config = gjc_mcp_entry_config(validated)
    command = config.get("command") if config else None
    args = config.get("args") if config else None
    activation_valid = (
        validated_ok
        and is_setup_managed_gjc_mcp_entry(validated, allow_redacted_env=True)
        and persisted_by_setup
        and is_active_gjc_mcp_entry(validated)
        and isinstance(command, str)
        and isinstance(args, list)
        and all(isinstance(arg, str) for arg in args)
        and verify_gjc_mcp_endpoint(command, args)
    )
    if not activation_valid:
        if created_here:
            if remove_persisted_gjc_mcp_server_locked(
                expected_entry_generation=persisted_generation
            ):
                if registration_state is not None:
                    registration_state.update(created=False, changed=False)
                    registration_state.pop("entry_generation", None)
            else:
                print_warning("Could not roll back the unvalidated GJC MCP registration.")
        return False
    print_success("Registered and execution-validated Ouroboros MCP server in GJC.")
    return True


def install_gjc_compatibility_bridge(content: str) -> bool:
    """Install the owned bridge when the host cannot autoload native MCP entries."""
    bridge = gjc_bridge_path()
    try:
        publish_owned_file(
            bridge,
            content,
            is_owned=is_setup_managed_gjc_bridge,
            trusted_ancestor=gjc_agent_dir(),
        )
    except UnownedArtifactError:
        print_error(f"Preserved custom GJC extension at {bridge}; compatibility activation failed.")
        return False
    except OSError as exc:
        print_warning(f"Could not install GJC compatibility bridge: {exc}")
        return False
    print_info("Installed GJC compatibility bridge; this host does not expose native MCP autoload.")
    return True


def remove_legacy_gjc_bridge() -> bool:
    """Remove the obsolete setup-owned input bridge without touching custom files."""
    from ouroboros.core.fs_ownership import claim_and_remove_owned, recover_owned_claims

    bridge = gjc_bridge_path()
    with suppress(OSError):
        recover_owned_claims(
            bridge, is_owned=is_setup_managed_gjc_bridge, trusted_ancestor=gjc_agent_dir()
        )
    if not os.path.lexists(bridge):
        return True
    try:
        removed = claim_and_remove_owned(
            bridge,
            is_owned=is_setup_managed_gjc_bridge,
            trusted_ancestor=gjc_agent_dir(),
        )
    except OSError as exc:
        print_warning(f"Could not remove legacy GJC bridge: {exc}")
        return False
    if not removed:
        print_info(f"Preserved custom GJC extension at {bridge}")
        return True
    try:
        bridge.parent.rmdir()
    except OSError:
        pass
    print_info("Removed obsolete GJC input bridge; native skills now own ooo routing.")
    return True


def install_gjc_runtime_artifacts(
    gjc_path: str,
    *,
    host: GjcSetupHost,
    registration_state: dict[str, object] | None = None,
) -> bool:
    """Activate one complete GJC frontdoor before retiring any prior route."""
    agent_dir = gjc_agent_dir()
    from ouroboros.core.fs_ownership import close_rollback_archive, prepare_rollback_archive

    rollback_archive = prepare_rollback_archive(agent_dir)
    state = registration_state if registration_state is not None else {}
    paths = (
        *setup_owned_gjc_skill_paths(agent_dir=agent_dir),
        gjc_instruction_path(),
        gjc_mcp_bridge_config_path(),
        gjc_bridge_path(),
    )
    snapshots: _PathSnapshots | None = None
    expected: _PathSnapshots = ()
    succeeded = False
    try:
        snapshots = _snapshot_gjc_paths(paths, host)
        expected = snapshots
        native_support = gjc_native_mcp_autoload_support(gjc_path, run_command=subprocess.run)
        if native_support is not None and not native_support:
            command, args = host.bridge_dispatch_entry()
            succeeded = install_gjc_compatibility_bridge(gjc_ooo_bridge_source_text(command, args))
            expected = _snapshot_gjc_paths(paths, host)
        elif native_support:
            installed = (
                install_gjc_mcp_bridge_config()
                and install_gjc_skills_step()
                and install_gjc_instruction_step()
            )
            expected = _snapshot_gjc_paths(paths, host)
            if installed:
                # Registration, the final durable-state validation, and legacy
                # retirement form one serialized transaction: a concurrent
                # uninstall cannot remove the MCP registration between the
                # validation and the retirement of the compatibility route.
                try:
                    with gjc_mcp_registration_lock():
                        succeeded = register_gjc_mcp_server(
                            gjc_path,
                            detect_mcp_entry=host.detect_mcp_entry,
                            registration_state=state,
                            assume_registration_lock=True,
                        )
                        if succeeded and not is_setup_managed_gjc_mcp_entry(
                            persisted_gjc_mcp_entry()
                        ):
                            print_warning(
                                "The GJC MCP registration disappeared before the legacy "
                                "route was retired; kept the compatibility bridge."
                            )
                            succeeded = False
                        if succeeded:
                            succeeded = remove_legacy_gjc_bridge()
                except OSError as exc:
                    print_warning(f"Could not serialize the GJC MCP activation transaction: {exc}")
                    succeeded = False
            if succeeded:
                expected = _snapshot_gjc_paths(paths, host)
    except OSError as exc:
        print_warning(f"Could not install GJC runtime artifacts: {exc}")
        succeeded = False

    if succeeded:
        close_rollback_archive(rollback_archive)
        return True
    if snapshots is not None:
        rollback_gjc_activation(
            snapshots,
            expected,
            restore_path_snapshot=host.restore_path_snapshot,
            snapshot_path=host.snapshot_path,
            registration_state=state,
            rollback_archive=rollback_archive,
        )
        for directory in (
            agent_dir / "skills",
            agent_dir / "rules",
            agent_dir / "ouroboros",
            agent_dir / "extensions" / "ouroboros-ooo-bridge",
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    close_rollback_archive(rollback_archive)
    return False


def _snapshot_gjc_paths(paths: Sequence[Path], host: GjcSetupHost) -> _PathSnapshots:
    return tuple((path, host.snapshot_path(path, follow_links=False)) for path in paths)


def setup_gjc_runtime(gjc_path: str, *, host: GjcSetupHost) -> bool:
    """Configure GJC and roll back only unchanged setup-owned generations."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    paths = (
        config_path,
        config_dir / "credentials.yaml",
        *setup_owned_gjc_skill_paths(agent_dir=gjc_agent_dir()),
        gjc_instruction_path(),
        gjc_mcp_bridge_config_path(),
        gjc_bridge_path(),
    )
    registration_state: dict[str, object] = {}
    snapshots: _PathSnapshots = ()
    expected: _PathSnapshots = ()
    try:
        snapshots = _snapshot_gjc_paths(paths, host)
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        else:
            create_default_config(config_dir)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        expected = _snapshot_gjc_paths(paths, host)
        config_generation = dict(expected)[config_path]
        if not isinstance(config, dict):
            print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting GJC setup.")
            _restore_gjc_paths(snapshots, expected, host.restore_path_snapshot, host.snapshot_path)
            return False

        orchestrator = config.get("orchestrator")
        if not isinstance(orchestrator, dict):
            orchestrator = {}
            config["orchestrator"] = orchestrator
        orchestrator.update(runtime_backend="gjc", gjc_cli_path=gjc_path)
        llm = config.get("llm")
        if not isinstance(llm, dict):
            llm = {}
            config["llm"] = llm
        llm["backend"] = "gjc"

        if not install_gjc_runtime_artifacts(
            gjc_path, host=host, registration_state=registration_state
        ):
            raise OSError("runtime artifact activation failed")
        current_after_activation = {
            path: host.snapshot_path(path, follow_links=False) for path in paths
        }
        current_after_activation[config_path] = config_generation
        expected = tuple(current_after_activation.items())
        host.atomic_write_text(
            config_path,
            yaml.dump(config, default_flow_style=False, sort_keys=False),
            expected_current=config_generation,
        )
    except (OSError, yaml.YAMLError) as exc:
        _restore_gjc_paths(snapshots, expected, host.restore_path_snapshot, host.snapshot_path)
        _rollback_new_gjc_mcp_registration(registration_state)
        print_error(f"GJC setup failed; restored the previous state: {exc}")
        return False

    print_success(f"Configured GJC runtime (CLI: {gjc_path})")
    print_info(f"Config saved to: {config_path}")
    return True


def _restore_gjc_paths(
    snapshots: _PathSnapshots,
    expected: _PathSnapshots,
    restore_path_snapshot: Callable[..., None],
    snapshot_path: Callable[..., object],
    rollback_archive: object | None = None,
) -> None:
    """Restore pre-transaction snapshots through the shared ownership primitives.

    There is no separate check-then-restore sequence: each path's current
    entry is claimed and verified against the exact generation this
    transaction last wrote, and the snapshot is staged beside the canonical
    path and published with a no-replace rename — an operator generation
    inserted at any point is preserved and that path's rollback is skipped.
    """
    from ouroboros.core.fs_ownership import (
        RollbackArchive,
        claim_and_archive_owned,
        claim_and_remove_owned,
        publish_owned_entry,
        recover_owned_claims,
    )

    failures: list[str] = []
    expected_by_path = dict(expected)
    if not snapshots:
        return
    missing = snapshot_path(
        snapshots[0][0].parent / f".{os.urandom(8).hex()}.gjc-missing-probe",
        follow_links=False,
    )
    for path, snapshot in reversed(snapshots):
        expected_current = expected_by_path.get(path)
        if expected_current is not None and snapshot == expected_current:
            continue  # this transaction never changed the path

        def _is_expected(claimed: Path, _expected: object = expected_current) -> bool:
            return _expected is None or snapshot_path(claimed, follow_links=False) == _expected

        def _build(staging: Path, _snapshot: object = snapshot) -> None:
            restore_path_snapshot(staging, _snapshot, restore_link_targets=False)

        try:
            recover_owned_claims(path, is_owned=_is_expected, trusted_ancestor=path.parent)
            if snapshot == missing:
                if not os.path.lexists(path):
                    continue
                if path.is_dir() and isinstance(rollback_archive, RollbackArchive):
                    removed = claim_and_archive_owned(
                        path,
                        archive=rollback_archive,
                        is_owned=_is_expected,
                        trusted_ancestor=path.parent,
                    )
                else:
                    removed = claim_and_remove_owned(
                        path, is_owned=_is_expected, trusted_ancestor=path.parent
                    )
                if not removed:
                    print_warning(f"Preserved concurrently changed GJC setup path: {path}")
                continue
            if (
                not os.path.lexists(path)
                and expected_current is not None
                and expected_current != missing
            ):
                # The generation this transaction wrote disappeared; do not
                # resurrect state into a slot another writer is managing.
                print_warning(f"Preserved concurrently changed GJC setup path: {path}")
                continue
            publish_owned_entry(
                path,
                _build,
                is_owned=_is_expected,
                trusted_ancestor=path.parent,
            )
        except UnownedArtifactError:
            print_warning(f"Preserved concurrently changed GJC setup path: {path}")
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        print_warning("GJC setup rollback was incomplete: " + "; ".join(failures))


def _rollback_new_gjc_mcp_registration(registration_state: dict[str, object]) -> None:
    if not registration_state.get("created"):
        return
    entry_generation = registration_state.get("entry_generation")
    if remove_persisted_gjc_mcp_server(
        expected_entry_generation=entry_generation if isinstance(entry_generation, str) else None
    ):
        registration_state.update(created=False, changed=False)
        registration_state.pop("entry_generation", None)
        return
    print_warning("Preserved the GJC MCP registration because it changed after setup created it.")


def rollback_gjc_activation(
    snapshots: _PathSnapshots,
    expected: _PathSnapshots,
    *,
    restore_path_snapshot: Callable[..., None],
    snapshot_path: Callable[..., object],
    registration_state: dict[str, object],
    rollback_archive: object | None = None,
) -> None:
    """Restore unchanged owned generations and remove a registration created here."""
    _restore_gjc_paths(
        snapshots,
        expected,
        restore_path_snapshot,
        snapshot_path,
        rollback_archive,
    )
    _rollback_new_gjc_mcp_registration(registration_state)
