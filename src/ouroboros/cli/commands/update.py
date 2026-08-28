"""Self-update command for Ouroboros.

Native CLI counterpart of the `ooo update` skill (skills/update/SKILL.md):
  1. Version check     (installed vs latest on PyPI, pre-release aware)
  2. Package upgrade   (same manager receipt and environment as the running CLI)
  3. Runtime refresh   (Claude Code plugin + `ouroboros setup --non-interactive`)

Never combines the [claude] and [mcp] extras: the Claude Agent SDK embeds
MCP 1.x while the protocol server requires MCP 2, so supported MCP hosts
launch their own isolated `ouroboros-ai[mcp]` process via uvx/pipx run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tomllib
from typing import Annotated, Literal
import urllib.request

import typer

from ouroboros import __version__
from ouroboros.backends import get_backend_capability, resolve_runtime_backend_name
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_info,
    print_success,
    print_warning,
)
from ouroboros.cli.omp_config import configure_omp_tool_call_timeout
from ouroboros.config.loader import load_config
from ouroboros.config.models import get_config_dir
from ouroboros.core.errors import ConfigError

app = typer.Typer(
    name="update",
    help="Update Ouroboros to the latest version.",
)

PYPI_JSON_URL = "https://pypi.org/pypi/ouroboros-ai/json"
PACKAGE_NAME = "ouroboros-ai"

_RUNTIME_CLI_IDENTITIES: dict[str, tuple[str, str, str]] = {
    "claude": ("OUROBOROS_CLI_PATH", "cli_path", "claude"),
    "codex": ("OUROBOROS_CODEX_CLI_PATH", "codex_cli_path", "codex"),
    "opencode": ("OUROBOROS_OPENCODE_CLI_PATH", "opencode_cli_path", "opencode"),
    "hermes": ("OUROBOROS_HERMES_CLI_PATH", "hermes_cli_path", "hermes"),
    "gemini": ("OUROBOROS_GEMINI_CLI_PATH", "gemini_cli_path", "gemini"),
    "kiro": ("OUROBOROS_KIRO_CLI_PATH", "kiro_cli_path", "kiro-cli"),
    "copilot": ("OUROBOROS_COPILOT_CLI_PATH", "copilot_cli_path", "copilot"),
    "goose": ("OUROBOROS_GOOSE_CLI_PATH", "goose_cli_path", "goose"),
    "pi": ("OUROBOROS_PI_CLI_PATH", "pi_cli_path", "pi"),
    "omp": ("OUROBOROS_OMP_CLI_PATH", "omp_cli_path", "omp"),
    "gjc": ("OUROBOROS_GJC_CLI_PATH", "gjc_cli_path", "gjc"),
    "antigravity": ("OUROBOROS_ANTIGRAVITY_CLI_PATH", "antigravity_cli_path", "agy"),
    "grok": ("OUROBOROS_GROK_CLI_PATH", "grok_cli_path", "grok"),
    "zcode": ("OUROBOROS_ZCODE_CLI_PATH", "zcode_cli_path", "zcode"),
}

_HOST_PLUGIN_RUNTIMES = ("claude", "codex")


def _is_cli_less_runtime_backend(backend: str) -> bool:
    """Return whether ``backend`` is a registry runtime with no CLI to refresh.

    The ``host`` dispatch runtime spawns nothing — the calling MCP host model
    executes the work — so update's executable refresh has no identity to
    resolve for it. Such a backend is still a valid, supported selection;
    rejecting it here would make ``OUROBOROS_AGENT_RUNTIME=host`` break
    ``ooo update`` for no operational reason.
    """
    capability = get_backend_capability(backend)
    return capability is not None and capability.supports_runtime and capability.cli_name is None


# ── Version helpers ──────────────────────────────────────────────

_FALLBACK_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_kind>a|b|rc)(?P<pre_num>\d+))?"
    r"(?:\.dev(?P<dev_num>\d+))?"
)


def _fallback_version_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """PEP 440-ish sort key for when `packaging` is not installed.

    Handles the shapes this project actually publishes (X.Y.Z, X.Y.Za1,
    X.Y.Zb2, X.Y.Zrc1, X.Y.Z.devN): dev < a < b < rc < final within a
    release. Unparseable versions sort lowest.
    """
    match = _FALLBACK_VERSION_RE.match(version)
    if match is None:
        return ((), -1, 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    pre_kind = match.group("pre_kind")
    if pre_kind is not None:
        phase = {"a": 1, "b": 2, "rc": 3}[pre_kind]
        number = int(match.group("pre_num") or 0)
    elif match.group("dev_num") is not None:
        phase, number = 0, int(match.group("dev_num"))
    else:
        phase, number = 4, 0
    return (release, phase, number)


def _compare_versions(a: str, b: str) -> int:
    """Return -1, 0, or 1 comparing version `a` to `b`."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            va, vb = Version(a), Version(b)
            return (va > vb) - (va < vb)
        except InvalidVersion:
            pass
    except ImportError:
        pass
    ka, kb = _fallback_version_key(a), _fallback_version_key(b)
    return (ka > kb) - (ka < kb)


def _is_prerelease(version: str) -> bool:
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(version).is_prerelease
        except InvalidVersion:
            return False
    except ImportError:
        match = _FALLBACK_VERSION_RE.match(version)
        if match is None:
            return False
        return match.group("pre_kind") is not None or match.group("dev_num") is not None


def _latest_pypi_version(include_prereleases: bool, timeout: float = 10.0) -> str | None:
    """Query PyPI for the newest ouroboros-ai version, or None if unreachable."""
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=timeout, context=context) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    # Build both channels from installable release files. `info.version` is
    # merely PyPI metadata and can itself name a fully-yanked release.
    candidates = [
        version
        for version, files in data.get("releases", {}).items()
        if isinstance(files, list)
        and any(isinstance(file, dict) and not file.get("yanked", False) for file in files)
        and (include_prereleases or not _is_prerelease(version))
    ]
    if not candidates:
        return None
    latest = candidates[0]
    for candidate in candidates[1:]:
        if _compare_versions(candidate, latest) > 0:
            latest = candidate
    return latest


# ── Installer detection and upgrade ──────────────────────────────


class InstallationIdentityError(RuntimeError):
    """Raised when the running installation cannot be updated safely."""


@dataclass(frozen=True)
class InstallationIdentity:
    """Receipt-backed identity of the running Ouroboros installation."""

    manager: Literal["uv", "pipx"]
    tool_name: str
    environment: Path
    profile: str
    console_path: Path
    manager_binary: str
    manager_home: Path


@dataclass(frozen=True)
class RuntimeRefreshTopology:
    """Configured runtime identity that an unattended update must preserve."""

    runtime_backend: str | None = None
    opencode_mode: Literal["plugin", "subprocess"] | None = None
    runtime_executable: str | None = None
    runtime_executable_env_key: str | None = None


def _normalise_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _resolve_environment_console(
    prefix: Path,
    *,
    windows: bool | None = None,
    pathext: str | None = None,
) -> Path | None:
    """Resolve only a console script owned by *prefix*, including Windows launchers."""
    environment = prefix.resolve()
    is_windows = os.name == "nt" if windows is None else windows
    script_dir = environment / ("Scripts" if is_windows else "bin")
    suffixes = [""]
    if is_windows:
        raw_extensions = pathext if pathext is not None else os.environ.get("PATHEXT", "")
        raw_extensions = raw_extensions or ".COM;.EXE;.BAT;.CMD"
        extensions: list[str] = []
        for extension in raw_extensions.split(";"):
            extension = extension.strip()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = f".{extension}"
            for variant in (extension.lower(), extension, extension.upper()):
                if variant not in extensions:
                    extensions.append(variant)
        suffixes = [*extensions, ""]
    for name in ("ouroboros", "ooo"):
        for suffix in suffixes:
            candidate = script_dir / f"{name}{suffix}"
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved.is_relative_to(environment):
                    return resolved
    return None


def _format_uv_requirement(requirement: object) -> str | None:
    if not isinstance(requirement, dict):
        return None
    name = requirement.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    extras = requirement.get("extras", [])
    if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
        return None
    label = name.strip()
    if extras:
        label += f"[{','.join(sorted(extras))}]"
    specifier = requirement.get("specifier")
    if specifier is not None:
        if not isinstance(specifier, str):
            return None
        label += specifier
    return label


_UV_REGISTRY_REQUIREMENT_FIELDS = frozenset(
    {
        "name",
        "extras",
        "groups",
        "marker",
        "specifier",
        "index",
        "conflict",
    }
)


def _uv_profile(receipt_path: Path) -> str:
    try:
        with receipt_path.open("rb") as receipt_file:
            data = tomllib.load(receipt_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallationIdentityError(f"cannot read uv receipt {receipt_path}: {exc}") from exc
    requirements = data.get("tool", {}).get("requirements")
    if not isinstance(requirements, list):
        raise InstallationIdentityError(f"uv receipt {receipt_path} has no requirements list")
    rendered: list[tuple[dict[object, object], str]] = []
    for requirement in requirements:
        if isinstance(requirement, dict):
            unsupported_fields = sorted(
                str(field) for field in requirement if field not in _UV_REGISTRY_REQUIREMENT_FIELDS
            )
            if unsupported_fields:
                fields = ", ".join(unsupported_fields)
                raise InstallationIdentityError(
                    f"uv receipt {receipt_path} requirement is not a PyPI registry source "
                    f"(unsupported fields: {fields})"
                )
        label = _format_uv_requirement(requirement)
        if not isinstance(requirement, dict) or label is None:
            raise InstallationIdentityError(
                f"uv receipt {receipt_path} has an unsupported requirement"
            )
        rendered.append((requirement, label))
    main = [
        label
        for requirement, label in rendered
        if _normalise_distribution_name(str(requirement.get("name", ""))) == PACKAGE_NAME
    ]
    if len(main) != 1:
        raise InstallationIdentityError(
            f"uv receipt {receipt_path} does not identify exactly one {PACKAGE_NAME} requirement"
        )
    additions = [label for _, label in rendered if label != main[0]]
    return main[0] if not additions else f"{main[0]} (with {', '.join(additions)})"


_PYPI_OUROBOROS_SPEC = re.compile(
    r"^ouroboros[-_.]ai(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
    r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>).+)?$",
    re.IGNORECASE,
)


def _pipx_profile(receipt_path: Path) -> str:
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallationIdentityError(f"cannot read pipx receipt {receipt_path}: {exc}") from exc
    main = data.get("main_package")
    if not isinstance(main, dict):
        raise InstallationIdentityError(f"pipx receipt {receipt_path} has no main package")
    package = main.get("package")
    package_or_url = main.get("package_or_url")
    if (
        not isinstance(package, str)
        or _normalise_distribution_name(package) != PACKAGE_NAME
        or not isinstance(package_or_url, str)
        or _PYPI_OUROBOROS_SPEC.fullmatch(package_or_url.strip()) is None
    ):
        raise InstallationIdentityError(
            f"pipx receipt {receipt_path} does not identify a PyPI {PACKAGE_NAME} profile"
        )
    injected = data.get("injected_packages", {})
    if not isinstance(injected, dict):
        raise InstallationIdentityError(
            f"pipx receipt {receipt_path} has an invalid injected-packages record"
        )
    injected_specs: list[str] = []
    for details in injected.values():
        if not isinstance(details, dict):
            raise InstallationIdentityError(
                f"pipx receipt {receipt_path} has an invalid injected package"
            )
        spec = details.get("package_or_url")
        if not isinstance(spec, str) or not spec.strip() or "\n" in spec or "\r" in spec:
            raise InstallationIdentityError(
                f"pipx receipt {receipt_path} has an invalid injected package spec"
            )
        injected_specs.append(spec.strip())
    profile = package_or_url.strip()
    if injected_specs:
        profile += f" (with injected {', '.join(sorted(injected_specs))})"
    return profile


def _detect_installation_identity(prefix: Path | None = None) -> InstallationIdentity:
    """Prove manager, environment, profile, and console from local receipts.

    No global manager listing or directory-name heuristic is accepted. A
    direct pip installation cannot prove which extras were requested, so it
    fails closed instead of silently replacing the profile with another.
    """
    environment = (prefix or Path(sys.prefix)).resolve()
    console_path = _resolve_environment_console(environment)
    if console_path is None:
        raise InstallationIdentityError(
            f"no Ouroboros console script exists inside the running environment {environment}"
        )

    uv_receipt = environment / "uv-receipt.toml"
    pipx_receipt = environment / "pipx_metadata.json"
    receipts = [path for path in (uv_receipt, pipx_receipt) if path.is_file()]
    if len(receipts) > 1:
        raise InstallationIdentityError(
            f"both uv and pipx receipts exist in {environment}; ownership is ambiguous"
        )
    if uv_receipt.is_file():
        binary = shutil.which("uv")
        if binary is None:
            raise InstallationIdentityError(
                "uv owns this environment but the uv executable is unavailable"
            )
        return InstallationIdentity(
            manager="uv",
            tool_name=environment.name,
            environment=environment,
            profile=_uv_profile(uv_receipt),
            console_path=console_path,
            manager_binary=binary,
            manager_home=environment.parent,
        )
    if pipx_receipt.is_file():
        binary = shutil.which("pipx")
        if binary is None:
            raise InstallationIdentityError(
                "pipx owns this environment but the pipx executable is unavailable"
            )
        if environment.parent.name != "venvs":
            raise InstallationIdentityError(
                f"cannot derive PIPX_HOME safely from non-standard environment {environment}"
            )
        return InstallationIdentity(
            manager="pipx",
            tool_name=environment.name,
            environment=environment,
            profile=_pipx_profile(pipx_receipt),
            console_path=console_path,
            manager_binary=binary,
            manager_home=environment.parent.parent,
        )
    raise InstallationIdentityError(
        "the running environment has no uv or pipx receipt; direct pip installs do not "
        "record requested extras, so the installed profile cannot be preserved automatically"
    )


def _upgrade_command(identity: InstallationIdentity, prerelease: bool) -> list[str]:
    """Build a receipt-replaying upgrade command for *identity*."""
    if identity.manager == "uv":
        command = [identity.manager_binary, "tool", "upgrade"]
        if prerelease:
            command.extend(["--prerelease", "allow"])
        return [*command, identity.tool_name]
    command = [identity.manager_binary, "upgrade", "--include-injected"]
    if prerelease:
        command.append("--pip-args=--pre")
    return [*command, identity.tool_name]


def _upgrade_environment(identity: InstallationIdentity) -> dict[str, str]:
    """Pin the manager invocation to the identity's proven environment root."""
    if identity.manager == "uv":
        return {"UV_TOOL_DIR": str(identity.manager_home)}
    return {"PIPX_HOME": str(identity.manager_home)}


def _run_step(
    command: list[str],
    *,
    description: str,
    dry_run: bool,
    timeout: float = 600.0,
    env_overrides: Mapping[str, str] | None = None,
) -> bool:
    """Run one update step, streaming its output. Returns True on success."""
    if dry_run:
        prefix = ""
        if env_overrides:
            prefix = " ".join(f"{key}={value}" for key, value in env_overrides.items()) + " "
        print_info(f"[dry-run] Would run: {prefix}{' '.join(command)}")
        return True
    try:
        command_env = None
        if env_overrides:
            command_env = {**os.environ, **env_overrides}
        result = subprocess.run(command, timeout=timeout, env=command_env)
    except (OSError, subprocess.SubprocessError):
        print_warning(f"{description} failed — could not run {command[0]!r}.")
        return False
    if result.returncode != 0:
        print_warning(f"{description} exited with code {result.returncode}.")
        return False
    print_success(description)
    return True


# ── Runtime integration refresh ──────────────────────────────────


def _refresh_claude_plugin(dry_run: bool, claude_executable: str | None) -> bool | None:
    """Refresh the Claude Code plugin. Returns None when the claude CLI is absent."""
    if claude_executable is None:
        print_info("claude CLI not found — skipping Claude Code plugin refresh.")
        return None
    # Best effort: a missing marketplace entry must not block the install step.
    _run_step(
        [claude_executable, "plugin", "marketplace", "update", "ouroboros"],
        description="Refreshed ouroboros marketplace",
        dry_run=dry_run,
    )
    installed = _run_step(
        [claude_executable, "plugin", "install", "ouroboros@ouroboros"],
        description="Installed Claude Code plugin",
        dry_run=dry_run,
    )
    if not installed:
        return False
    # `install` is a no-op for an already-installed plugin. The explicit
    # update is what advances an existing installation to the marketplace's
    # refreshed version.
    return _run_step(
        [claude_executable, "plugin", "update", "ouroboros@ouroboros"],
        description="Updated Claude Code plugin",
        dry_run=dry_run,
    )


def _refresh_codex_plugin(dry_run: bool, codex_executable: str | None) -> bool | None:
    """Refresh the Codex marketplace snapshot that owns the installed plugin."""
    if codex_executable is None:
        print_info("codex CLI not found — skipping Codex plugin refresh.")
        return None
    return _run_step(
        [codex_executable, "plugin", "marketplace", "upgrade", "ouroboros"],
        description="Refreshed Codex Ouroboros marketplace",
        dry_run=dry_run,
    )


def _refresh_all_runtime_artifacts(
    dry_run: bool,
    identity: InstallationIdentity,
) -> bool:
    """Refresh installed host artifacts without changing the selected runtime."""
    return _run_step(
        [str(identity.console_path), "setup", "refresh"],
        description="Refreshed installed runtime artifacts",
        dry_run=dry_run,
    )


def _refresh_runtime_config(
    runtime: str,
    dry_run: bool,
    identity: InstallationIdentity,
    *,
    opencode_mode: Literal["plugin", "subprocess"] | None = None,
    runtime_executable: str | None = None,
    runtime_executable_env_key: str | None = None,
) -> bool:
    """Re-run setup through the proven installation's refreshed console script."""
    command = [str(identity.console_path), "setup", "--runtime", runtime]
    if runtime in {"opencode", "opencode_cli"} and opencode_mode is not None:
        command.extend(["--opencode-mode", opencode_mode])
    if runtime in {"codex", "codex_cli"}:
        command.append("--preserve-existing-llm")
    command.append("--non-interactive")
    return _run_step(
        command,
        description=f"Refreshed {runtime} runtime config",
        dry_run=dry_run,
        env_overrides=(
            {runtime_executable_env_key: runtime_executable}
            if runtime_executable is not None and runtime_executable_env_key is not None
            else None
        ),
    )


def _canonical_runtime_backend(runtime: str) -> str:
    normalized = runtime.strip().lower()
    try:
        backend = resolve_runtime_backend_name(normalized)
    except ValueError:
        return normalized
    if backend in _RUNTIME_CLI_IDENTITIES:
        return backend

    # Worker runtimes such as codex_mcp/claude_mcp share their operator-facing
    # CLI integration with the canonical Codex/Claude setup backend. Derive
    # that relationship from capability metadata instead of another alias map.
    capability = get_backend_capability(backend)
    if capability is not None:
        for integration, (_, config_field, command_name) in _RUNTIME_CLI_IDENTITIES.items():
            if capability.cli_config_key == config_field and capability.cli_name == command_name:
                return integration
    return backend


def _validate_runtime_option(runtime: str) -> str:
    """Normalize supported runtime values before update performs any I/O."""
    normalized = runtime.strip().lower()
    if normalized in {"all", "auto", "none"}:
        return normalized
    backend = _canonical_runtime_backend(normalized)
    if backend not in _RUNTIME_CLI_IDENTITIES and not _is_cli_less_runtime_backend(backend):
        supported = ", ".join(("all", "auto", "none", *_RUNTIME_CLI_IDENTITIES))
        raise typer.BadParameter(
            f"unsupported runtime {runtime!r}; choose one of: {supported}",
            param_hint="--runtime",
        )
    return backend


def _runtime_executable_identity(
    runtime: str,
    config: object | None,
) -> tuple[str | None, str | None]:
    """Resolve env > config > PATH executable identity for one runtime."""
    backend = _canonical_runtime_backend(runtime)
    identity = _RUNTIME_CLI_IDENTITIES.get(backend)
    if identity is None:
        return None, None
    env_key, config_field, command_name = identity
    env_value = os.environ.get(env_key, "").strip()
    configured_value: str | None = None
    configured_key: str | None = None
    if env_value:
        configured_value = env_value
        configured_key = env_key
    elif config is not None:
        orchestrator = getattr(config, "orchestrator", None)
        raw_value = getattr(orchestrator, config_field, None)
        if isinstance(raw_value, str) and raw_value.strip():
            configured_value = raw_value.strip()
            configured_key = f"orchestrator.{config_field}"

    candidate = (
        str(Path(configured_value).expanduser()) if configured_value is not None else command_name
    )
    resolved = shutil.which(candidate)
    if resolved is None:
        if configured_value is not None:
            raise ConfigError(
                f"Configured {backend} executable is not runnable: {candidate}",
                config_key=configured_key,
            )
        return None, None
    return str(Path(resolved).absolute()), env_key


def _configured_runtime_topology(requested_runtime: str = "auto") -> RuntimeRefreshTopology:
    """Resolve backend, mode, and exact runtime executable without identity drift."""
    config_path = get_config_dir() / "config.yaml"
    config_existed = config_path.exists()
    config = None
    try:
        config = load_config(config_path)
    except ConfigError:
        # A genuinely unconfigured installation may still use PATH discovery.
        # An existing malformed/invalid config is different: treating it as
        # absent could authorize a different runtime before package mutation.
        # Check both sides of the load to fail closed across create/delete races.
        if not config_existed and not config_path.exists():
            config = None
        else:
            raise

    configured_backend = config.orchestrator.runtime_backend if config is not None else None
    env_backend = (
        os.environ.get("OUROBOROS_AGENT_RUNTIME", "").strip()
        or os.environ.get("OUROBOROS_RUNTIME", "").strip()
    )
    if requested_runtime != "auto":
        runtime_backend = _canonical_runtime_backend(requested_runtime)
    elif env_backend:
        runtime_backend = _canonical_runtime_backend(env_backend)
        if runtime_backend not in _RUNTIME_CLI_IDENTITIES and not _is_cli_less_runtime_backend(
            runtime_backend
        ):
            raise ConfigError(
                f"Configured runtime backend is unsupported: {env_backend}",
                config_key="OUROBOROS_AGENT_RUNTIME",
            )
    else:
        runtime_backend = configured_backend

    runtime_executable: str | None = None
    runtime_executable_env_key: str | None = None
    if runtime_backend is not None:
        runtime_executable, runtime_executable_env_key = _runtime_executable_identity(
            runtime_backend,
            config,
        )
    elif requested_runtime == "auto":
        # Preserve the historic unconfigured auto order, but include supported
        # executable overrides that intentionally live outside PATH.
        for candidate_backend in ("claude", "codex"):
            candidate, env_key = _runtime_executable_identity(candidate_backend, None)
            if candidate is not None:
                runtime_backend = candidate_backend
                runtime_executable = candidate
                runtime_executable_env_key = env_key
                break

    opencode_mode = config.orchestrator.opencode_mode if config is not None else None
    # Legacy OpenCode configs predate the explicit mode field. Their effective
    # topology is non-plugin/subprocess because the dispatch gate requires an
    # explicit ``plugin`` value; preserve that behavior during refresh.
    if runtime_backend == "opencode" and opencode_mode is None:
        opencode_mode = "subprocess"
    return RuntimeRefreshTopology(
        runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
        runtime_executable=runtime_executable,
        runtime_executable_env_key=runtime_executable_env_key,
    )


def _resolve_runtime(runtime: str, *, configured_backend: str | None = None) -> str:
    """Resolve --runtime auto while preserving an existing configured backend."""
    if runtime != "auto":
        return runtime
    if configured_backend is not None:
        return configured_backend
    if shutil.which("claude") is not None:
        return "claude"
    if shutil.which("codex") is not None:
        return "codex"
    return "none"


def _installed_version(identity: InstallationIdentity) -> str | None:
    """Read the version from the refreshed binary (post-upgrade, fresh process)."""
    try:
        result = subprocess.run(
            [str(identity.console_path), "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"\d+\.\d+\.\d+[0-9a-zA-Z.+-]*", result.stdout)
    return match.group(0) if match else None


# ── CLI Command ──────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def update(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Only report installed vs latest version — change nothing.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip confirmation prompt.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the commands that would run without executing them.",
        ),
    ] = False,
    prerelease: Annotated[
        bool | None,
        typer.Option(
            "--prerelease/--no-prerelease",
            help="Include pre-releases (default: only when a pre-release is installed).",
        ),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            "-r",
            help="Runtime integration to refresh after upgrading "
            "(all refreshes Claude and Codex without changing the configured backend; "
            "auto preserves the configured backend; none skips refresh).",
            callback=_validate_runtime_option,
        ),
    ] = "auto",
) -> None:
    """Update Ouroboros to the latest version.

    Replays the running uv/pipx installation's own receipt, preserving its
    environment and optional-dependency profile. Then refreshes the selected
    runtime through that same environment's console script.

    [dim]Examples:[/dim]
    [dim]    ouroboros update              # interactive[/dim]
    [dim]    ouroboros update --check      # version check only[/dim]
    [dim]    ouroboros update -y           # no prompts (for scripts)[/dim]
    [dim]    ouroboros update --dry-run    # preview the commands[/dim]
    [dim]    ouroboros update -r all       # refresh Claude and Codex integrations[/dim]
    """
    console.print("\n[bold cyan]Ouroboros Update[/bold cyan]\n")

    current = __version__
    include_prereleases = _is_prerelease(current) if prerelease is None else prerelease
    latest = _latest_pypi_version(include_prereleases=include_prereleases)
    if latest is None:
        print_warning("Could not reach PyPI to check the latest version.")
        raise typer.Exit(1)

    console.print(f"Installed: [cyan]v{current}[/cyan]")
    console.print(f"Latest:    [cyan]v{latest}[/cyan]")

    if _compare_versions(current, latest) >= 0:
        if not check and not configure_omp_tool_call_timeout(dry_run=dry_run):
            print_warning(
                "Could not set OMP MCP tool timeout; run: "
                "omp config set extensionHandlers.toolCallTimeoutMs 60000"
            )
        console.print(f"\n[green]Ouroboros is up to date (v{current}).[/green]\n")
        raise typer.Exit()

    console.print(f"\nUpdate available: [yellow]v{current}[/yellow] → [green]v{latest}[/green]")
    console.print(f"[dim]Changes: https://github.com/Q00/ouroboros/releases/tag/v{latest}[/dim]\n")

    if check:
        raise typer.Exit()

    configured_topology = RuntimeRefreshTopology()
    configured_host_topologies: dict[str, RuntimeRefreshTopology] = {}
    if runtime != "none":
        try:
            if runtime == "all":
                configured_host_topologies = {
                    host: _configured_runtime_topology(host) for host in _HOST_PLUGIN_RUNTIMES
                }
            else:
                configured_topology = _configured_runtime_topology(runtime)
        except ConfigError as exc:
            print_warning(f"Cannot update safely: runtime configuration is invalid: {exc}")
            console.print("[dim]No changes were made.[/dim]\n")
            raise typer.Exit(1) from exc

    try:
        identity = _detect_installation_identity()
    except InstallationIdentityError as exc:
        print_warning(f"Cannot update safely: {exc}.")
        console.print(
            "[dim]No changes were made. Reinstall with your original manager and exact "
            "ouroboros-ai[...] profile, then use `ouroboros update` for future updates.[/dim]\n"
        )
        raise typer.Exit(1) from exc
    console.print(f"Installer:   [cyan]{identity.manager}[/cyan]")
    console.print(f"Environment: [cyan]{identity.environment}[/cyan]")
    console.print(f"Profile:     [cyan]{identity.profile}[/cyan]\n")

    if not yes and not dry_run:
        if not typer.confirm(f"Update to v{latest}?", default=True):
            print_info("Cancelled.")
            raise typer.Exit()

    if not _run_step(
        _upgrade_command(identity, include_prereleases),
        description=f"Upgraded {identity.profile} via {identity.manager}",
        dry_run=dry_run,
        env_overrides=_upgrade_environment(identity),
    ):
        console.print(
            "\n[bold yellow]Update failed — package upgrade did not complete.[/bold yellow]\n"
        )
        raise typer.Exit(1)

    installed: str | None = None
    if not dry_run:
        installed = _installed_version(identity)
        if installed is None:
            print_warning(
                "Package upgrade returned success, but the refreshed installation "
                "version could not be verified."
            )
            console.print("[bold yellow]Runtime integration was not refreshed.[/bold yellow]\n")
            raise typer.Exit(1)
        if _compare_versions(installed, latest) < 0:
            print_warning(
                f"Package upgrade returned success, but the refreshed installation is "
                f"v{installed} (expected at least v{latest})."
            )
            console.print("[bold yellow]Runtime integration was not refreshed.[/bold yellow]\n")
            raise typer.Exit(1)

    failed: list[str] = []
    resolved_runtime = _resolve_runtime(
        runtime,
        configured_backend=configured_topology.runtime_backend,
    )
    opencode_mode: Literal["plugin", "subprocess"] | None = (
        configured_topology.opencode_mode
        if resolved_runtime in {"opencode", "opencode_cli"}
        else None
    )

    if resolved_runtime == "none":
        print_info("Runtime refresh skipped — package upgrade only.")
    elif resolved_runtime == "all":
        claude_refreshed = _refresh_claude_plugin(
            dry_run,
            configured_host_topologies["claude"].runtime_executable,
        )
        if claude_refreshed is False:
            failed.append("Claude Code plugin refresh")

        codex_refreshed = _refresh_codex_plugin(
            dry_run,
            configured_host_topologies["codex"].runtime_executable,
        )
        if codex_refreshed is False:
            failed.append("Codex plugin refresh")

        if not _refresh_all_runtime_artifacts(dry_run, identity):
            failed.append("installed runtime artifact refresh")
    elif resolved_runtime == "claude":
        plugin_refreshed = _refresh_claude_plugin(
            dry_run,
            configured_topology.runtime_executable,
        )
        if plugin_refreshed is None:
            # claude CLI absent: a notice, not a failure — setup would also
            # fail without claude on PATH, so skip the config refresh too.
            print_info(
                "Skipping claude runtime config refresh — install the claude CLI "
                "and run `ouroboros setup --runtime claude` later."
            )
        else:
            if plugin_refreshed is False:
                failed.append("Claude Code plugin refresh")
            if not _refresh_runtime_config(
                "claude",
                dry_run,
                identity,
                runtime_executable=configured_topology.runtime_executable,
                runtime_executable_env_key=(configured_topology.runtime_executable_env_key),
            ):
                failed.append("claude runtime config refresh")
    else:
        # Codex and the other setup-supported runtimes: setup re-installs
        # the packaged rules/skills, so no separate plugin step is needed.
        if not _refresh_runtime_config(
            resolved_runtime,
            dry_run,
            identity,
            opencode_mode=opencode_mode,
            runtime_executable=configured_topology.runtime_executable,
            runtime_executable_env_key=configured_topology.runtime_executable_env_key,
        ):
            failed.append(f"{resolved_runtime} runtime config refresh")

    if not configure_omp_tool_call_timeout(dry_run=dry_run):
        print_warning(
            "Could not set OMP MCP tool timeout; run: "
            "omp config set extensionHandlers.toolCallTimeoutMs 60000"
        )

    console.print()
    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/yellow]\n")
        raise typer.Exit()

    assert installed is not None  # Verified before any runtime integration mutation.
    if failed:
        console.print("[bold yellow]Ouroboros partially updated.[/bold yellow]")
        console.print("[yellow]Could not complete:[/yellow]")
        for step in failed:
            console.print(f"  [yellow]![/yellow] {step}")
        console.print()
    else:
        console.print(f"[bold green]Updated to v{installed}.[/bold green]")
    if resolved_runtime == "all":
        refreshed_hosts = [
            host
            for host, topology in configured_host_topologies.items()
            if topology.runtime_executable is not None
        ]
        if refreshed_hosts:
            joined_hosts = " and ".join(refreshed_hosts)
            console.print(f"[dim]Restart active {joined_hosts} sessions to apply the update.[/dim]")
        console.print("[dim]Claude may use /reload-plugins; Codex sessions must restart.[/dim]")
    elif resolved_runtime == "claude":
        console.print("[dim]Restart your Claude Code session to apply the update.[/dim]")
        console.print("[dim]If the CLAUDE.md block content changed, regenerate it: ooo setup[/dim]")
    elif resolved_runtime != "none":
        console.print(f"[dim]Restart your {resolved_runtime} session to apply the update.[/dim]")
    console.print()
    if failed:
        raise typer.Exit(1)
