"""Setup command for Ouroboros.

Standalone setup that works in any terminal — not just inside Claude Code.
Detects available runtimes and configures Ouroboros accordingly.

Also provides brownfield repository management subcommands:
    ouroboros setup scan [ROOT]  Re-scan a root directory for repos/worktrees
    ouroboros setup list         List registered brownfield repos
    ouroboros setup default      Toggle default brownfield repos
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as datetime_time
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tomllib
from typing import Annotated, Literal

from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table
import typer
import yaml

from ouroboros.bigbang.brownfield import scan_and_register, set_default_repo
from ouroboros.cli.commands.claude_setup import (
    setup_claude as _setup_claude,
)
from ouroboros.cli.commands.claude_setup import (
    setup_claude_cli as _setup_claude_cli,
)
from ouroboros.cli.commands.claude_setup import (
    setup_claude_sdk as _setup_claude_sdk,
)
from ouroboros.cli.commands.pi_bridge import pi_ooo_bridge_source_text
from ouroboros.cli.commands.setup_atomic_restore import restore_hermes, restore_hermes_receipt
from ouroboros.cli.commands.setup_completion import print_setup_completion
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.cli.opencode_config import (
    BRIDGE_PLUGIN_FILENAME as _BRIDGE_PLUGIN_FILENAME,
)
from ouroboros.cli.opencode_config import (
    BRIDGE_PLUGIN_SUBDIR as _BRIDGE_PLUGIN_SUBDIR,
)
from ouroboros.cli.opencode_config import (
    find_opencode_config,
    opencode_config_dir,
)
from ouroboros.cli.opencode_config import (
    is_bridge_plugin_entry as _is_bridge_plugin_entry,
)
from ouroboros.cli.setup_model_config import (
    has_explicit_codex_model_override as _has_explicit_codex_model_override,
)
from ouroboros.cli.setup_model_config import (
    is_shipped_default_roster as _is_shipped_default_roster,
)
from ouroboros.cli.setup_model_config import (
    neutralize_fresh_codex_model_defaults as _neutralize_fresh_codex_model_defaults,
)
from ouroboros.cli.windows_codex_mcp import apply_windows_codex_mcp_mode, is_native_windows
from ouroboros.codex.cli_policy import resolve_codex_cli_path
from ouroboros.codex.home import resolve_codex_home
from ouroboros.codex.runtime_profile import codex_uses_profile_v2 as _shared_codex_uses_profile_v2
from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
from ouroboros.core.errors import ConfigError
from ouroboros.package_profiles import (
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    UVX_PYTHON_FLOOR,
    has_unsupported_claude_sdk_mcp_mix,
)
from ouroboros.persistence.brownfield import BrownfieldStore


class _SetupCodexCliLogger:
    """Small adapter for shared Codex CLI resolution diagnostics."""

    def warning(self, event: str, **kwargs: object) -> None:
        hint = kwargs.get("hint")
        print_warning(f"{event}: {hint}" if hint else event)

    def info(self, event: str, **kwargs: object) -> None:
        print_info(event)


def _build_uvx_mcp_args(package_spec: str) -> list[str]:
    """Return the canonical uvx args for the requested Ouroboros package spec."""
    return [
        "--isolated",
        "--python",
        UVX_PYTHON_FLOOR,
        "--from",
        package_spec,
        "ouroboros",
        "mcp",
        "serve",
    ]


def _detect_mcp_entry(*, package_spec: str = "ouroboros-ai[mcp]") -> dict[str, object] | None:
    """Build an isolated MCP 2 launcher entry.

    Direct ``ouroboros`` and ``python -m`` fallbacks are deliberately excluded:
    their environments may contain the Claude SDK's MCP 1.x dependency or no
    MCP extra at all. ``uvx --isolated`` and ``pipx run`` both create a
    package-isolated process whose ``[mcp]`` extra is known to contain MCP 2.
    ``uvx --from`` without ``--isolated`` is insufficient: uv may reuse an
    already installed Ouroboros tool whose environment contains MCP 1.x.
    Matches the contract in install.sh and skills/setup/SKILL.md.
    """
    if shutil.which("uvx"):
        return {"command": "uvx", "args": _build_uvx_mcp_args(package_spec)}
    if shutil.which("pipx"):
        return {
            "command": "pipx",
            "args": ["run", "--spec", package_spec, "ouroboros", "mcp", "serve"],
        }
    return None


type _PersistentFileState = tuple[bool, str, int]


def _capture_persistent_file(path: Path) -> _PersistentFileState:
    """Capture enough state to restore one setup-managed text file."""
    if not path.exists():
        return (False, "", 0o644)
    return (True, path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777)


def _restore_persistent_file(path: Path, state: _PersistentFileState) -> None:
    """Restore a file snapshot after a multi-file setup transaction fails."""
    existed, content, mode = state
    if existed:
        _atomic_write_text(path, content, mode=mode)
    else:
        path.unlink(missing_ok=True)


def _rollback_persistent_files(
    snapshots: tuple[tuple[Path, _PersistentFileState], ...],
) -> None:
    """Best-effort rollback for files changed during runtime activation."""
    failures: list[str] = []
    for path, state in reversed(snapshots):
        try:
            _restore_persistent_file(path, state)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        print_warning("Setup rollback was incomplete: " + "; ".join(failures))


def _commit_runtime_activation(
    *,
    runtime_name: str,
    host_path: Path,
    config_path: Path,
    config_was_missing: bool,
    runtime_content: str,
    register_host: Callable[[], bool],
    create_defaults: Callable[[Path], object],
    additional_host_paths: tuple[Path, ...] = (),
) -> bool:
    """Commit host registration and runtime files as one recoverable unit."""
    credentials_path = config_path.parent / "credentials.yaml"
    snapshots: tuple[tuple[Path, _PersistentFileState], ...] = ()
    try:
        snapshot_paths = tuple(dict.fromkeys((host_path, *additional_host_paths)))
        snapshots = tuple(
            (path, _capture_persistent_file(path))
            for path in (*snapshot_paths, config_path, credentials_path)
        )
        if not register_host():
            _rollback_persistent_files(snapshots)
            print_error(f"{runtime_name} setup aborted without changing persistent configuration.")
            return False
        if config_was_missing:
            create_defaults(config_path.parent)
        _atomic_write_text(config_path, runtime_content)
    except (OSError, ConfigError) as exc:
        _rollback_persistent_files(snapshots)
        print_error(f"{runtime_name} setup could not commit runtime configuration: {exc}")
        return False
    return True


def _ensure_claude_mcp_entry() -> None:
    """Explain the process boundary for legacy callers.

    Kept as a compatibility shim for callers from older plugin artifacts. The
    current Claude Agent SDK requires MCP 1.x, while the Ouroboros protocol
    server requires MCP 2 and cannot load that configured backend from its
    isolated process. Never mutate user-owned Claude MCP configuration here.
    """
    print_warning(
        escape(
            "Claude SDK stays on MCP 1.x. Launch the Ouroboros MCP 2 server from "
            "the separate ouroboros-ai[mcp] profile with --runtime claude-cli."
        )
    )


app = typer.Typer(
    name="setup",
    help="Set up Ouroboros for your environment.",
    invoke_without_command=True,
)


# ── Runtime detection helpers ────────────────────────────────────


def _get_current_backend() -> str | None:
    """Read the current runtime backend from config, if configured."""
    config_path = Path.home() / ".ouroboros" / "config.yaml"
    if not config_path.exists():
        return None
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
        backend = data.get("orchestrator", {}).get("runtime_backend")
        if backend == "claude_mcp":
            return "claude-cli"
        return backend
    except Exception:
        return None


def _resolve_setup_codex_cli_path(
    *,
    explicit_cli_path: str | Path | None = None,
    configured_cli_path: str | None = None,
) -> str:
    """Resolve the Codex executable setup will persist and later runtime will use."""
    return resolve_codex_cli_path(
        explicit_cli_path=explicit_cli_path,
        configured_cli_path=configured_cli_path,
        logger=_SetupCodexCliLogger(),
        log_namespace="setup.codex",
    ).cli_path


def _runtime_cli_from_env(env_key: str, command_name: str) -> str | None:
    """Resolve an explicit runtime executable before consulting PATH."""
    configured = os.environ.get(env_key, "").strip()
    if configured:
        resolved = shutil.which(str(Path(configured).expanduser()))
        return str(Path(resolved).absolute()) if resolved is not None else None
    resolved = shutil.which(command_name)
    return str(Path(resolved).absolute()) if resolved is not None else None


def _detect_runtimes() -> dict[str, str | None]:
    """Detect available runtime CLIs in PATH.

    For Gemini, Goose, and Kiro, we additionally honor explicit-path overrides
    and persisted orchestrator ``*_cli_path`` settings so users with non-PATH
    installs are still detected.
    """
    runtimes: dict[str, str | None] = {
        "claude": _runtime_cli_from_env("OUROBOROS_CLI_PATH", "claude"),
        "codex": shutil.which("codex"),
        "opencode": _runtime_cli_from_env("OUROBOROS_OPENCODE_CLI_PATH", "opencode"),
        "hermes": _runtime_cli_from_env("OUROBOROS_HERMES_CLI_PATH", "hermes"),
    }

    # Codex: mirror the shared runtime launch resolver so setup persists the
    # executable that nested execution will actually use.
    env_codex_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH", "").strip()
    if env_codex_path:
        configured = Path(
            _resolve_setup_codex_cli_path(explicit_cli_path=env_codex_path)
        ).expanduser()
        runtimes["codex"] = (
            str(configured) if configured.is_file() and os.access(configured, os.X_OK) else None
        )
    else:
        # Persisted config paths may be repaired by setup, so use them when
        # runnable but still allow PATH/App discovery when they are stale.
        try:
            from ouroboros.config import get_codex_cli_path

            codex_path = get_codex_cli_path()
        except Exception:
            codex_path = None
        if codex_path:
            configured = Path(
                _resolve_setup_codex_cli_path(configured_cli_path=codex_path)
            ).expanduser()
            if configured.is_file() and os.access(configured, os.X_OK):
                runtimes["codex"] = str(configured)

        if runtimes["codex"] is not None:
            resolved = Path(
                _resolve_setup_codex_cli_path(explicit_cli_path=runtimes["codex"])
            ).expanduser()
            # Trust `shutil.which`: it already proved PATH executability. This
            # keeps setup tests hermetic while still canonicalizing relative
            # PATH entries and wrapper fallbacks through the shared resolver.
            runtimes["codex"] = str(resolved)

        # Codex App bundles this executable but does not always add it to the
        # terminal PATH. Treat it as an available Codex runtime for App-only users.
        if runtimes["codex"] is None and _CODEX_APP_CLI_PATH.is_file():
            resolved = Path(
                _resolve_setup_codex_cli_path(explicit_cli_path=_CODEX_APP_CLI_PATH)
            ).expanduser()
            if resolved.is_file() and os.access(resolved, os.X_OK):
                runtimes["codex"] = str(resolved)

    # Gemini: prefer explicit-path config (env var / config.yaml) over PATH.
    try:
        from ouroboros.config import get_gemini_cli_path

        gemini_path = get_gemini_cli_path()
    except Exception:
        gemini_path = None
    runtimes["gemini"] = gemini_path or shutil.which("gemini")

    # Goose: explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_goose_cli_path

        goose_path = get_goose_cli_path()
    except Exception:
        goose_path = None
    runtimes["goose"] = (
        goose_path if goose_path and shutil.which(goose_path) else None
    ) or shutil.which("goose")

    # Kiro: same explicit-path-first policy. Binary is ``kiro-cli``.
    # Validate the helper result defensively so a stale env/config override
    # cannot make setup persist a dead executable path.
    try:
        from ouroboros.config import get_kiro_cli_path

        kiro_path = get_kiro_cli_path()
    except Exception:
        kiro_path = None
    runtimes["kiro"] = (
        kiro_path if kiro_path and shutil.which(kiro_path) else None
    ) or shutil.which("kiro-cli")

    # Copilot: explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_copilot_cli_path

        copilot_path = get_copilot_cli_path()
    except Exception:
        copilot_path = None
    runtimes["copilot"] = (
        copilot_path if copilot_path and shutil.which(copilot_path) else None
    ) or shutil.which("copilot")

    # Pi: explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_pi_cli_path

        pi_path = get_pi_cli_path()
    except Exception:
        pi_path = None
    runtimes["pi"] = (pi_path if pi_path and shutil.which(pi_path) else None) or shutil.which("pi")

    # GJC: explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_gjc_cli_path

        gjc_path = get_gjc_cli_path()
    except Exception:
        gjc_path = None
    runtimes["gjc"] = (gjc_path if gjc_path and shutil.which(gjc_path) else None) or shutil.which(
        "gjc"
    )

    # Antigravity (agy): explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_antigravity_cli_path

        antigravity_path = get_antigravity_cli_path()
    except Exception:
        antigravity_path = None
    runtimes["antigravity"] = antigravity_path or shutil.which("agy")

    # Grok Build (grok): explicit-path config first, then PATH.
    try:
        from ouroboros.config import get_grok_cli_path

        grok_path = get_grok_cli_path()
    except Exception:
        grok_path = None
    runtimes["grok"] = grok_path or shutil.which("grok")

    # Zcode: app-bundle/config path first, then a PATH wrapper/binary.
    try:
        from ouroboros.config import get_zcode_cli_path

        zcode_path = get_zcode_cli_path()
    except Exception:
        zcode_path = None
    runtimes["zcode"] = zcode_path or shutil.which("zcode")

    return runtimes


_CODEX_MCP_SECTION_TEMPLATE = """# Ouroboros MCP hookup for Codex CLI.
# Keep Ouroboros runtime settings and per-role model overrides in
# ~/.ouroboros/config.yaml (for example: clarification.default_model,
# llm.qa_model, evaluation.semantic_model, consensus.*).
# This file is only for the Codex MCP/env registration block.

[mcp_servers.ouroboros]
{command_lines}

[mcp_servers.ouroboros.env]
OUROBOROS_AGENT_RUNTIME = "codex"
OUROBOROS_LLM_BACKEND = "codex"
"""
_CODEX_MCP_COMMENT_LINES = (
    "# Ouroboros MCP hookup for Codex CLI.",
    "# Keep Ouroboros runtime settings and per-role model overrides in",
    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
    "# This file is only for the Codex MCP/env registration block.",
)

CodexMcpMode = Literal["auto", "http", "preserve", "stdio"]
_CODEX_APP_CLI_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_CODEX_UVX_MCP_ARGS = _build_uvx_mcp_args("ouroboros-ai[mcp]")
_CODEX_LEGACY_UVX_MCP_ARGS: tuple[tuple[str, ...], ...] = (
    tuple(_CODEX_UVX_MCP_ARGS),
    (
        "--python",
        UVX_PYTHON_FLOOR,
        "--from",
        "ouroboros-ai[mcp]",
        "ouroboros",
        "mcp",
        "serve",
    ),
    ("--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"),
    ("--from", "ouroboros-ai", "ouroboros", "mcp", "serve"),
    ("ouroboros", "mcp", "serve"),
)
_CODEX_MANAGED_MCP_ENV = {
    "OUROBOROS_AGENT_RUNTIME": "codex",
    "OUROBOROS_LLM_BACKEND": "codex",
}
_CODEX_HOST_MCP_ENV = {
    "OUROBOROS_AGENT_RUNTIME": "host",
    "OUROBOROS_LLM_BACKEND": "codex",
}
_CODEX_HOST_DIRECT_MCP_ARGS = [
    "mcp",
    "serve",
    "--runtime",
    "host",
    "--llm-backend",
    "codex",
]
_CODEX_HOST_MODULE_MCP_ARGS = ["-m", "ouroboros", *_CODEX_HOST_DIRECT_MCP_ARGS]
_CODEX_DIRECT_MCP_BASE_ARGS = ("mcp", "serve")
_CODEX_DIRECT_MCP_ARGS = [
    *_CODEX_DIRECT_MCP_BASE_ARGS,
    "--runtime",
    "codex",
    "--llm-backend",
    "codex",
]
_CODEX_MODULE_MCP_ARGS = ["-m", "ouroboros", *_CODEX_DIRECT_MCP_ARGS]
_CODEX_LEGACY_DIRECT_MCP_ARGS = (
    tuple(_CODEX_DIRECT_MCP_ARGS),
    _CODEX_DIRECT_MCP_BASE_ARGS,
)
_CODEX_LEGACY_MODULE_MCP_ARGS = (
    tuple(_CODEX_MODULE_MCP_ARGS),
    ("-m", "ouroboros", *_CODEX_DIRECT_MCP_BASE_ARGS),
)
_CODEX_PROFILE_COMMENT = (
    "# Ouroboros task profile anchor. Generated by ouroboros setup; edit freely."
)

_CodexProfileSettings = dict[str, object]

_CODEX_DEFAULT_PROFILE_SECTIONS: dict[str, _CodexProfileSettings] = {
    "ouroboros-fast": {"model_reasoning_effort": "low"},
    "ouroboros-standard": {"model_reasoning_effort": "medium"},
    "ouroboros-deep": {"model_reasoning_effort": "high"},
    "ouroboros-frontier": {"model_reasoning_effort": "xhigh"},
}

_CODEX_WORKER_PROFILE_NAME = "ouroboros-worker"
_CODEX_PROFILE_V2_COMMENT = (
    "# Ouroboros Codex profile-v2 file. Generated by ouroboros setup; edit freely."
)

_CODEX_DEFAULT_LLM_PROFILES: dict[str, dict[str, object]] = {
    "fast": {
        "max_turns": 1,
        "temperature": 0.2,
        "providers": {"codex": {"reasoning_effort": "low"}},
    },
    "standard": {
        "max_turns": 3,
        "temperature": 0.3,
        "providers": {"codex": {"reasoning_effort": "medium"}},
    },
    "deep": {
        "max_turns": 5,
        "temperature": 0.4,
        "providers": {"codex": {"reasoning_effort": "high"}},
    },
    "frontier": {
        "max_turns": 8,
        "temperature": 0.4,
        "providers": {"codex": {"reasoning_effort": "xhigh"}},
    },
}

_CODEX_DEFAULT_LLM_ROLE_PROFILES: dict[str, str] = {
    "ambiguity": "deep",
    "assertion_extraction": "fast",
    "brownfield": "fast",
    "context_compression": "deep",
    "mechanical_detection": "fast",
    "question_classification": "deep",
    "qa": "frontier",
    "atomicity": "standard",
    "brownfield_explore": "frontier",
    "clarification": "frontier",
    "decomposition": "standard",
    "dependency_analysis": "standard",
    "pm_interview": "deep",
    "seed_generation": "deep",
    "consensus_advocate": "deep",
    "consensus_perspective": "deep",
    "consensus_vote": "deep",
    "double_diamond": "deep",
    "ontology_analysis": "deep",
    "pm_document": "deep",
    "reflect": "deep",
    "semantic_evaluation": "deep",
    "wonder": "frontier",
    "consensus_judge": "frontier",
    "agent_runtime": "standard",
    "agent_runtime_implementation": "standard",
    "agent_runtime_interview": "deep",
    "agent_runtime_coordinator": "standard",
    "agent_runtime_evaluation": "deep",
}


def _normalize_codex_mcp_mode(value: str) -> CodexMcpMode:
    normalized = value.lower()
    if normalized not in {"auto", "http", "preserve", "stdio"}:
        print_error("Unsupported Codex MCP mode. Use one of: auto, http, preserve, stdio.")
        raise typer.Exit(1)
    return normalized  # type: ignore[return-value]


def _codex_mcp_entry_from_toml(data: dict[str, object]) -> dict[str, object] | None:
    """Return the parsed Ouroboros Codex MCP entry, if present."""
    mcp_servers = data.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return None
    entry = mcp_servers.get("ouroboros")
    return entry if isinstance(entry, dict) else None


def _codex_mcp_entry_has_endpoint(entry: dict[str, object] | None) -> bool:
    """Return whether a Codex MCP entry has a usable command or URL."""
    if not isinstance(entry, dict):
        return False
    for key in ("command", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _is_source_tree_ouroboros_build() -> bool:
    """Return whether this module is executing from an Ouroboros source tree.

    The project name is read from the parsed document rather than matched as a
    formatted substring. TOML does not guarantee the spelling ``name = "..."``,
    so ``name="ouroboros-ai"``, single quotes, or extra whitespace all declare
    the same project while failing a literal search; conversely the literal can
    appear in a comment, a ``[tool.*]`` table, or a dependency pin without being
    the project name. Both directions change which Codex MCP command block
    setup writes (see ``_render_codex_mcp_section``).
    """
    import tomllib

    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        pyproject = parent / "pyproject.toml"
        source_package = parent / "src" / "ouroboros"
        if not pyproject.exists():
            continue
        if not current_file.is_relative_to(source_package):
            continue
        try:
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            # Unreadable or malformed metadata keeps walking the parents, as
            # before, instead of aborting setup.
            continue
        project = parsed.get("project")
        if not isinstance(project, dict):
            continue
        if project.get("name") == "ouroboros-ai" and source_package.is_dir():
            return True
    return False


def _is_dev_ouroboros_build() -> bool:
    """Return whether setup is running from a dev/editable Ouroboros build."""
    if _is_source_tree_ouroboros_build():
        return True
    try:
        version = importlib_metadata.version("ouroboros-ai")
    except importlib_metadata.PackageNotFoundError:
        return False
    return ".dev" in version or "+" in version


def _toml_string(value: str) -> str:
    """Render one TOML basic string without invalid surrogate escapes."""
    return json.dumps(value, ensure_ascii=False)


def _codex_release_mcp_launcher() -> tuple[str, list[str]] | None:
    """Resolve a launchable release MCP command for the current machine."""
    uvx_path = shutil.which("uvx")
    if uvx_path:
        return uvx_path, list(_CODEX_UVX_MCP_ARGS)

    ouroboros_path = shutil.which("ouroboros")
    if ouroboros_path:
        try:
            result = subprocess.run(
                [ouroboros_path, "mcp", "serve", "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            result = None
        if result is not None and result.returncode == 0:
            return ouroboros_path, list(_CODEX_DIRECT_MCP_ARGS)

    if (
        Path(sys.executable).is_file()
        and os.access(sys.executable, os.X_OK)
        and importlib_util.find_spec("mcp") is not None
    ):
        return sys.executable, list(_CODEX_MODULE_MCP_ARGS)
    return None


def _render_codex_mcp_section() -> str | None:
    """Render the managed Codex MCP block for the current install source.

    Release installs use ``uvx --isolated --from ouroboros-ai[mcp]`` so Codex
    can bootstrap the MCP extra without reusing a conflicting installed tool.
    Dev/git installs must instead point Codex at this
    Python environment; otherwise setup silently downgrades the MCP server back
    to the latest PyPI release and hides main-branch fixes under test.
    """
    if _is_dev_ouroboros_build():
        if (
            not Path(sys.executable).is_file()
            or not os.access(sys.executable, os.X_OK)
            or importlib_util.find_spec("mcp") is None
        ):
            return None
        command = sys.executable
        args = list(_CODEX_MODULE_MCP_ARGS)
    else:
        launcher = _codex_release_mcp_launcher()
        if launcher is None:
            return None
        command, args = launcher
    command_lines = "\n".join(
        (
            f"command = {_toml_string(command)}",
            "args = [" + ", ".join(_toml_string(arg) for arg in args) + "]",
        )
    )
    return _CODEX_MCP_SECTION_TEMPLATE.format(command_lines=command_lines)


def _has_managed_codex_mcp_comment(raw: str) -> bool:
    """Return whether the Ouroboros MCP table carries setup's managed comment."""
    lines = raw.splitlines()
    expected = list(_CODEX_MCP_COMMENT_LINES)
    for index, line in enumerate(lines):
        if not _toml_line_starts_structure(lines, index):
            continue
        if not _is_codex_ouroboros_table_header(line.strip()):
            continue
        comment_end = index
        while comment_end > 0 and not lines[comment_end - 1].strip():
            comment_end -= 1
        comment_start = comment_end - len(expected)
        if comment_start >= 0 and lines[comment_start:comment_end] == expected:
            return True
    return False


def _is_setup_managed_codex_mcp_entry(
    entry: dict[str, object], *, has_managed_comment: bool = False
) -> bool:
    """Return whether setup may safely replace this Codex MCP entry."""
    if "url" in entry:
        return False

    command = entry.get("command")
    args = entry.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return False

    env = entry.get("env")
    if env is not None and env != _CODEX_MANAGED_MCP_ENV and env != _CODEX_HOST_MCP_ENV:
        return False
    if set(entry) - {"command", "args", "env"}:
        return False

    if Path(command).name == "uvx":
        return tuple(str(arg) for arg in args) in _CODEX_LEGACY_UVX_MCP_ARGS
    if Path(command).name == "ouroboros":
        args_tuple = tuple(str(arg) for arg in args)
        if has_managed_comment:
            return args_tuple in _CODEX_LEGACY_DIRECT_MCP_ARGS
        return (
            args_tuple == _CODEX_DIRECT_MCP_BASE_ARGS
            and env == _CODEX_MANAGED_MCP_ENV
            and _command_matches_path_program(command, "ouroboros")
        )
    if not has_managed_comment:
        return False
    return tuple(str(arg) for arg in args) in (
        *_CODEX_LEGACY_MODULE_MCP_ARGS,
        tuple(_CODEX_HOST_MODULE_MCP_ARGS),
    )


def _command_matches_path_program(command: str, program: str) -> bool:
    """Return whether a configured command is the exact PATH-selected program."""
    expected = shutil.which(program)
    configured = shutil.which(command)
    if expected is None or configured is None:
        return False
    try:
        return Path(configured).resolve(strict=True) == Path(expected).resolve(strict=True)
    except OSError:
        return False


_CODEX_WORKER_PROFILE_SECTION = """# Ouroboros Agent OS runtime profile for Codex worker subprocesses.
# Activated when ~/.ouroboros/config.yaml sets `orchestrator.runtime_profile.backend_profile: worker`
# (or the OUROBOROS_RUNTIME_PROFILE=worker env var). Add per-worker Codex
# overrides below — for example a different model, sandbox, or notify hook —
# without affecting interactive `codex` sessions that share this config file.

[profiles.ouroboros-worker]
"""

_CODEX_WORKER_PROFILE_V2_CONTENT = """# Ouroboros Agent OS runtime profile for Codex worker subprocesses.
# Activated when ~/.ouroboros/config.yaml sets `orchestrator.runtime_profile.backend_profile: worker`
# or the OUROBOROS_RUNTIME_PROFILE=worker env var. Add per-worker Codex
# overrides below — for example a different model, sandbox, or notify hook —
# without affecting interactive `codex` sessions that share this config file.
"""

_CODEX_WORKER_PROFILE_COMMENT_LINES = (
    "# Ouroboros Agent OS runtime profile for Codex worker subprocesses.",
    "# Activated when ~/.ouroboros/config.yaml sets `orchestrator.runtime_profile.backend_profile: worker`",
    "# (or the OUROBOROS_RUNTIME_PROFILE=worker env var). Add per-worker Codex",
    "# overrides below — for example a different model, sandbox, or notify hook —",
    "# without affecting interactive `codex` sessions that share this config file.",
)


def _is_codex_ouroboros_worker_profile_header(line: str) -> bool:
    """Return True when the line starts the managed Codex worker profile table."""
    path = _toml_table_header_path(line)
    return (
        path is not None
        and len(path) >= 2
        and path[:2]
        == (
            "profiles",
            _CODEX_WORKER_PROFILE_NAME,
        )
    )


def _trim_managed_codex_worker_profile_comments(lines: list[str]) -> None:
    """Excise every managed worker-profile comment block from ``lines``."""
    expected = list(_CODEX_WORKER_PROFILE_COMMENT_LINES)
    block_len = len(expected)
    if block_len == 0:
        return

    index = 0
    while index <= len(lines) - block_len:
        if lines[index : index + block_len] == expected:
            end = index + block_len
            if end < len(lines) and not lines[end].strip():
                end += 1
            del lines[index:end]
        else:
            index += 1


def _upsert_codex_worker_profile_section(raw: str) -> tuple[str, bool]:
    """Refresh the managed comment block for ``[profiles.ouroboros-worker]``.

    Setup owns only the managed comment/header. Existing user-authored keys
    and subtables under ``[profiles.ouroboros-worker]`` are preserved verbatim.
    """
    section_lines = _CODEX_WORKER_PROFILE_SECTION.strip("\n").splitlines()
    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    existed_before = False
    refreshed = False

    while index < len(input_lines):
        line = input_lines[index]
        stripped = line.strip()
        starts_structure = _toml_line_starts_structure(input_lines, index)
        path = _toml_table_header_path(stripped) if starts_structure else None
        is_worker_root = path == ("profiles", _CODEX_WORKER_PROFILE_NAME)
        if is_worker_root and not refreshed:
            existed_before = True
            refreshed = True
            _trim_managed_codex_worker_profile_comments(output_lines)
            if output_lines and output_lines[-1].strip():
                output_lines.append("")
            output_lines.extend(section_lines)
            index += 1
            continue
        if starts_structure and _is_codex_ouroboros_worker_profile_header(stripped):
            existed_before = True
            output_lines.append(line)
            index += 1
            continue
        output_lines.append(line)
        index += 1

    if not refreshed:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.extend(section_lines)

    return "\n".join(output_lines).rstrip() + "\n", existed_before


def _toml_table_header_body(line: str) -> str | None:
    """Return the body of a TOML table header, accepting trailing comments."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    is_array_table = stripped.startswith("[[")
    body_start = 2 if is_array_table else 1

    quote: str | None = None
    escaped = False
    for index, char in enumerate(stripped[body_start:], start=body_start):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char != "]":
            continue
        suffix_start = index + 1
        if is_array_table:
            if suffix_start >= len(stripped) or stripped[suffix_start] != "]":
                continue
            suffix_start += 1
        suffix = stripped[suffix_start:].strip()
        if suffix and not suffix.startswith("#"):
            return None
        body = stripped[body_start:index].strip()
        return body or None
    return None


def _toml_table_header_path(line: str) -> tuple[str, ...] | None:
    """Return the parsed dotted TOML path for a table header line."""
    body = _toml_table_header_body(line)
    if body is None:
        return None

    marker = "__ouroboros_table_header_marker__"
    try:
        parsed = tomllib.loads(f"[{body}]\n{marker} = true\n")
    except tomllib.TOMLDecodeError:
        return None

    path: list[str] = []

    def _walk(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get(marker) is True:
            return True
        for key, value in node.items():
            if key == marker:
                continue
            path.append(str(key))
            if _walk(value):
                return True
            path.pop()
        return False

    return tuple(path) if _walk(parsed) else None


def _is_toml_table_header(line: str) -> bool:
    return _toml_table_header_path(line) is not None


def _is_codex_ouroboros_table_header(line: str) -> bool:
    """Return True when the line starts the managed Codex MCP table."""
    path = _toml_table_header_path(line)
    return path is not None and len(path) >= 2 and path[:2] == ("mcp_servers", "ouroboros")


def _trim_managed_codex_comments(lines: list[str]) -> None:
    """Remove the managed Codex comment block immediately before a table."""
    while lines and not lines[-1].strip():
        lines.pop()

    comment_index = len(lines)
    for expected in reversed(_CODEX_MCP_COMMENT_LINES):
        if comment_index == 0 or lines[comment_index - 1] != expected:
            break
        comment_index -= 1
    else:
        del lines[comment_index:]
        return

    comment_end = len(lines)
    comment_start = comment_end
    while comment_start > 0 and lines[comment_start - 1].strip().startswith("#"):
        comment_start -= 1
    if comment_start < comment_end and lines[comment_start].strip().startswith(
        "# Ouroboros MCP hookup"
    ):
        del lines[comment_start:comment_end]


def _toml_parent_table_assignment_key(line: str, parent_path: tuple[str, ...]) -> str | None:
    """Return the key assigned in a parent table line, if parseable."""
    path = _toml_assignment_path(line, parent_path=parent_path)
    if path is None or len(path) != len(parent_path) + 1 or path[: len(parent_path)] != parent_path:
        return None
    return path[-1]


def _toml_assignment_path(
    line: str, *, parent_path: tuple[str, ...] = ()
) -> tuple[str, ...] | None:
    """Return the TOML key path assigned by one line, if parseable."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None

    quote: str | None = None
    escaped = False
    equals_index: int | None = None
    for index, char in enumerate(stripped):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "=":
            equals_index = index
            break
    if equals_index is None:
        return None

    key = stripped[:equals_index].strip()
    if not key:
        return None
    try:
        path = _toml_table_path_from_body(key)
    except tomllib.TOMLDecodeError:
        return None
    if path is None:
        return None
    return parent_path + path


def _toml_assignment_span_end(
    lines: list[str],
    start_index: int,
    *,
    parent_path: tuple[str, ...] = (),
) -> int:
    """Return the exclusive end index for a valid TOML assignment span."""
    table_header = ""
    if parent_path:
        table_header = "[" + ".".join(_render_toml_key(part) for part in parent_path) + "]\n"
    for end_index in range(start_index + 1, len(lines) + 1):
        snippet = "\n".join(lines[start_index:end_index]).strip()
        if not snippet:
            continue
        try:
            tomllib.loads(f"{table_header}{snippet}\n")
        except tomllib.TOMLDecodeError:
            continue
        return end_index
    return start_index + 1


def _toml_fragment_is_complete(lines: list[str]) -> bool:
    """Return whether a TOML fragment is parseable as a complete document."""
    snippet = "\n".join(lines).strip()
    if not snippet:
        return True
    try:
        tomllib.loads(f"{snippet}\n")
    except tomllib.TOMLDecodeError:
        return False
    return True


def _toml_line_starts_structure(lines: list[str], index: int) -> bool:
    """Return whether *index* begins outside any multiline TOML value."""
    return _toml_fragment_is_complete(lines[:index])


def _toml_table_boundary_is_real(lines: list[str], section_start: int, boundary_index: int) -> bool:
    """Return whether a header-looking line is outside multiline TOML values."""
    return _toml_fragment_is_complete(lines[section_start:boundary_index])


def _remove_key_from_root_inline_table_assignment(
    lines: list[str],
    start_index: int,
    *,
    table_name: str,
    remove_keys: set[str],
) -> tuple[list[str], int] | None:
    """Remove keys from a root inline table assignment while preserving siblings."""
    end_index = _toml_assignment_span_end(lines, start_index)
    snippet = "\n".join(lines[start_index:end_index]).strip()
    try:
        parsed = tomllib.loads(f"{snippet}\n")
    except tomllib.TOMLDecodeError:
        return None
    table = parsed.get(table_name)
    if not isinstance(table, dict) or not any(key in table for key in remove_keys):
        return None

    remaining = {str(key): value for key, value in table.items() if str(key) not in remove_keys}
    replacement: list[str] = []
    if remaining:
        replacement.append(f"[{table_name}]")
        replacement.extend(
            f"{_render_toml_key(key)} = {_render_toml_value(value)}"
            for key, value in remaining.items()
        )
    return replacement, end_index


def _toml_table_path_from_body(body: str) -> tuple[str, ...] | None:
    """Parse a TOML dotted key/table body into a path tuple."""
    marker = "__ouroboros_table_header_marker__"
    parsed = tomllib.loads(f"[{body}]\n{marker} = true\n")

    path: list[str] = []

    def _walk(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get(marker) is True:
            return True
        for key, value in node.items():
            if key == marker:
                continue
            path.append(str(key))
            if _walk(value):
                return True
            path.pop()
        return False

    return tuple(path) if _walk(parsed) else None


def _remove_codex_mcp_section(raw: str) -> tuple[str, bool]:
    """Remove an existing Ouroboros Codex MCP entry from dotted or inline TOML."""
    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    existed_before = False
    current_table_path: tuple[str, ...] | None = None

    while index < len(input_lines):
        line = input_lines[index]
        stripped = line.strip()
        starts_structure = _toml_line_starts_structure(input_lines, index)
        path = _toml_table_header_path(stripped) if starts_structure else None
        if path is not None:
            current_table_path = path

        if starts_structure and _is_codex_ouroboros_table_header(stripped):
            existed_before = True
            preserved_comments: list[str] = []
            _trim_managed_codex_comments(output_lines)
            section_start = index
            index += 1
            while index < len(input_lines):
                next_stripped = input_lines[index].strip()
                is_table_header = _is_toml_table_header(next_stripped)
                if is_table_header:
                    if not _toml_table_boundary_is_real(input_lines, section_start, index):
                        index += 1
                        continue
                    if not _is_codex_ouroboros_table_header(next_stripped):
                        break
                if next_stripped.startswith("#"):
                    preserved_comments.append(input_lines[index])
                index += 1
            if preserved_comments:
                if output_lines and output_lines[-1].strip():
                    output_lines.append("")
                output_lines.extend(preserved_comments)
            continue

        assignment_path = (
            _toml_assignment_path(stripped, parent_path=current_table_path or ())
            if starts_structure
            else None
        )
        if assignment_path == ("mcp_servers",):
            rewritten = _remove_key_from_root_inline_table_assignment(
                input_lines,
                index,
                table_name="mcp_servers",
                remove_keys={"ouroboros"},
            )
            if rewritten is not None:
                existed_before = True
                replacement, index = rewritten
                output_lines.extend(replacement)
                continue
        if assignment_path is not None and assignment_path[:2] == ("mcp_servers", "ouroboros"):
            existed_before = True
            index = _toml_assignment_span_end(
                input_lines,
                index,
                parent_path=current_table_path or (),
            )
            continue

        output_lines.append(line)
        index += 1

    cleaned = "\n".join(output_lines).rstrip()
    return (cleaned + ("\n" if cleaned else "")), existed_before


def _upsert_codex_mcp_section(raw: str, section: str) -> tuple[str, bool]:
    """Insert or replace the managed Codex MCP block.

    Returns:
        Tuple of (updated_contents, existed_before).
    """
    section_lines = section.strip("\n").splitlines()
    cleaned_raw, existed_before = _remove_codex_mcp_section(raw)
    output_lines = cleaned_raw.splitlines()
    if output_lines and output_lines[-1].strip():
        output_lines.append("")
    output_lines.extend(section_lines)

    return "\n".join(output_lines).rstrip() + "\n", existed_before


def _codex_uses_profile_v2(codex_path: str | None = None) -> bool:
    """Return whether ``codex --profile`` expects ``<name>.config.toml`` files."""
    return _shared_codex_uses_profile_v2(codex_path, run_command=subprocess.run) is True


def _register_codex_mcp_server(
    *,
    mode: CodexMcpMode = "auto",
    expected_snapshots: dict[Path, _PathSnapshot] | None = None,
) -> bool:
    """Register the Ouroboros MCP/env hookup in ~/.codex/config.toml."""
    import tomllib

    if mode == "preserve":
        codex_config = resolve_codex_home() / "config.toml"
        if codex_config.exists():
            try:
                parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError:
                print_error(f"Could not parse {codex_config} — Codex setup not saved.")
                return False
            if not _codex_mcp_entry_has_endpoint(_codex_mcp_entry_from_toml(parsed)):
                print_error(
                    "Preserved Codex MCP config does not define a usable Ouroboros "
                    "command or URL; Codex setup not saved."
                )
                return False
        else:
            print_error(f"{codex_config} does not exist — Codex setup not saved.")
            return False
        print_info("Preserved Codex MCP config.")
        return True

    if is_native_windows():
        handled, success, section = apply_windows_codex_mcp_mode(
            mode,
            codex_config=resolve_codex_home() / "config.toml",
            launcher=(
                (sys.executable, list(_CODEX_MODULE_MCP_ARGS))
                if _is_dev_ouroboros_build()
                else _codex_release_mcp_launcher()
            )
            if mode == "http"
            else None,
            print_info=print_info,
            print_error=print_error,
        )
        if handled and (not success or section is None):
            return success
        rendered_section = section if handled else _render_codex_mcp_section()
    else:
        if mode == "http":
            print_error("--mcp-mode http is only for native Windows Codex Desktop.")
            return False
        rendered_section = _render_codex_mcp_section()
    if rendered_section is None:
        print_error(
            "Could not find a launchable Ouroboros MCP command. Install uv, or install "
            "Ouroboros with the \\[mcp] extra, then rerun setup."
        )
        return False

    codex_config = resolve_codex_home() / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    codex_config_snapshot = _snapshot_path(codex_config)

    if codex_config.exists():
        raw = codex_config.read_text(encoding="utf-8")
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            print_error(f"Could not parse {codex_config} — Codex setup not saved.")
            return False

        entry = _codex_mcp_entry_from_toml(parsed)
        has_managed_comment = _has_managed_codex_mcp_comment(raw)
        if (
            entry is not None
            and not _codex_mcp_entry_has_endpoint(entry)
            and mode not in {"stdio", "http"}
        ):
            print_error(
                "Existing Codex Ouroboros MCP config has no usable command or URL; "
                "Codex setup not saved. Use an explicit replacement mode."
            )
            return False
        if (
            mode == "auto"
            and entry is not None
            and not _is_setup_managed_codex_mcp_entry(
                entry, has_managed_comment=has_managed_comment
            )
        ):
            print_info(
                "Preserved existing user-managed Ouroboros MCP config in "
                f"{codex_config}. Use --mcp-mode stdio to replace it."
            )
            return True

        updated_raw, existed_before = _upsert_codex_mcp_section(raw, rendered_section)
        if updated_raw == raw:
            print_info("Codex MCP server already up to date.")
            return True
        try:
            tomllib.loads(updated_raw)
        except tomllib.TOMLDecodeError as exc:
            print_error(
                f"Could not update {codex_config} — MCP registration would create invalid TOML."
            )
            print_info(str(exc))
            return False

        written_snapshot = _atomic_write_text_if_current_matches(
            codex_config,
            updated_raw,
            codex_config_snapshot,
        )
        if expected_snapshots is not None:
            expected_snapshots[codex_config] = written_snapshot
        if existed_before:
            print_success(f"Updated Ouroboros MCP server in {codex_config}")
        else:
            print_success(f"Registered Ouroboros MCP server in {codex_config}")
    else:
        written_snapshot = _atomic_write_text_if_current_matches(
            codex_config,
            rendered_section.lstrip("\n"),
            codex_config_snapshot,
        )
        if expected_snapshots is not None:
            expected_snapshots[codex_config] = written_snapshot
        print_success(f"Registered Ouroboros MCP server in {codex_config}")
    return True


def _render_codex_profile_section(name: str, settings: _CodexProfileSettings) -> str:
    """Render a sparse Codex profile table used as an Ouroboros anchor."""
    lines = [_CODEX_PROFILE_COMMENT, f"[profiles.{name}]"]
    for key, value in settings.items():
        if not isinstance(value, dict):
            lines.append(f"{_render_toml_key(key)} = {_render_toml_value(value)}")
    return "\n".join(lines)


def _render_codex_profile_v2_file(settings: _CodexProfileSettings) -> str:
    """Render a sparse Codex profile-v2 file."""
    lines = [_CODEX_PROFILE_V2_COMMENT, *_render_toml_mapping(settings)]
    return "\n".join(lines).rstrip() + "\n"


def _render_toml_key(key: str) -> str:
    """Render a TOML key without changing dotted/custom key semantics."""
    if key and all(char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in key):
        return key
    return _toml_string(key)


def _render_toml_value(value: object) -> str:
    """Render a parsed TOML scalar/list value back to TOML without type loss."""
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, datetime | date | datetime_time):
        return value.isoformat()
    if isinstance(value, dict):
        return _render_toml_inline_table({str(key): item for key, item in value.items()})
    if isinstance(value, list):
        return "[" + ", ".join(_render_toml_value(item) for item in value) + "]"
    msg = f"Unsupported TOML value type: {type(value).__name__}"
    raise TypeError(msg)


def _render_toml_inline_table(settings: dict[str, object]) -> str:
    """Render a parsed TOML table as a TOML inline table.

    ``tomllib`` represents TOML arrays-of-tables as ``list[dict]``.  When
    migrating a legacy Codex profile into a profile-v2 file those nested dicts
    must remain valid TOML instead of crashing after earlier setup mutations.
    """
    items = [
        f"{_render_toml_key(str(key))} = {_render_toml_value(value)}"
        for key, value in settings.items()
    ]
    return "{ " + ", ".join(items) + " }"


def _render_toml_mapping(settings: _CodexProfileSettings, prefix: str = "") -> list[str]:
    """Render simple TOML mappings, preserving nested profile subtables."""
    lines: list[str] = []
    nested: list[tuple[str, dict[str, object]]] = []
    for key, value in settings.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{_render_toml_key(str(key))} = {_render_toml_value(value)}")

    for key, value in nested:
        quoted_key = _render_toml_key(str(key))
        section_name = f"{prefix}.{quoted_key}" if prefix else quoted_key
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{section_name}]")
        lines.extend(_render_toml_mapping(value, section_name))

    return lines


def _existing_codex_profile_names(raw: str) -> set[str]:
    """Return configured Codex profile names from a TOML document."""
    import tomllib

    if not raw.strip():
        return set()

    parsed = tomllib.loads(raw)
    profiles = parsed.get("profiles")
    if profiles is None:
        return set()
    if not isinstance(profiles, dict):
        msg = "Codex config contains a non-table 'profiles' key."
        raise ValueError(msg)
    return {str(name) for name in profiles}


def _upsert_codex_profile_sections(raw: str) -> tuple[str, list[str]]:
    """Append missing managed Codex profile anchors without touching existing profiles."""
    existing_profiles = _existing_codex_profile_names(raw)
    missing_profiles = [
        name for name in _CODEX_DEFAULT_PROFILE_SECTIONS if name not in existing_profiles
    ]
    if not missing_profiles:
        return raw, []

    output = raw.rstrip()
    if output:
        output += "\n\n"
    output += "\n\n".join(
        _render_codex_profile_section(name, _CODEX_DEFAULT_PROFILE_SECTIONS[name])
        for name in missing_profiles
    )
    return output.rstrip() + "\n", missing_profiles


def _is_codex_ouroboros_profile_header(line: str, profile_names: set[str]) -> bool:
    path = _toml_table_header_path(line)
    return (
        path is not None and len(path) >= 2 and path[0] == "profiles" and path[1] in profile_names
    )


def _legacy_codex_profile_section_is_generated(
    raw: str,
    profile_name: str,
    settings: _CodexProfileSettings,
) -> bool:
    """Return whether a legacy profile section is byte-equivalent to setup output."""
    input_lines = raw.splitlines()
    for index, line in enumerate(input_lines):
        if not _toml_line_starts_structure(input_lines, index):
            continue
        path = _toml_table_header_path(line)
        if path != ("profiles", profile_name):
            continue
        start = index
        if index > 0 and input_lines[index - 1] == _CODEX_PROFILE_COMMENT:
            start = index - 1
        end = index + 1
        while end < len(input_lines):
            stripped = input_lines[end].strip()
            is_table_header = _toml_line_starts_structure(
                input_lines, end
            ) and _is_toml_table_header(stripped)
            if is_table_header and not _is_codex_ouroboros_profile_header(stripped, {profile_name}):
                break
            end += 1
        section = "\n".join(input_lines[start:end]).strip()
        return section == _render_codex_profile_section(profile_name, settings).strip()
    return False


def _remove_codex_legacy_profile_sections(
    raw: str, profile_names: set[str]
) -> tuple[str, dict[str, _CodexProfileSettings]]:
    """Remove legacy ``[profiles.ouroboros-*]`` sections and return their values."""
    import tomllib

    migrated: dict[str, _CodexProfileSettings] = {}
    if raw.strip():
        parsed = tomllib.loads(raw)
        profiles = parsed.get("profiles")
        if profiles is not None:
            if not isinstance(profiles, dict):
                msg = "Codex config contains a non-table 'profiles' key."
                raise ValueError(msg)
            for name in profile_names:
                profile = profiles.get(name)
                if isinstance(profile, dict):
                    migrated[name] = {str(key): value for key, value in profile.items()}

    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    current_table_path: tuple[str, ...] | None = None
    while index < len(input_lines):
        stripped = input_lines[index].strip()
        starts_structure = _toml_line_starts_structure(input_lines, index)
        path = _toml_table_header_path(stripped) if starts_structure else None
        if path is not None:
            current_table_path = path
        if starts_structure and _is_codex_ouroboros_profile_header(stripped, profile_names):
            _trim_managed_codex_worker_profile_comments(output_lines)
            while output_lines and output_lines[-1] == _CODEX_PROFILE_COMMENT:
                output_lines.pop()
            while output_lines and not output_lines[-1].strip():
                output_lines.pop()

            section_start = index
            index += 1
            while index < len(input_lines):
                next_stripped = input_lines[index].strip()
                is_table_header = _is_toml_table_header(next_stripped)
                if is_table_header:
                    if not _toml_table_boundary_is_real(input_lines, section_start, index):
                        index += 1
                        continue
                    if not _is_codex_ouroboros_profile_header(next_stripped, profile_names):
                        break
                index += 1
            continue

        assignment_path = (
            _toml_assignment_path(stripped, parent_path=current_table_path or ())
            if starts_structure
            else None
        )
        if assignment_path == ("profiles",):
            rewritten = _remove_key_from_root_inline_table_assignment(
                input_lines,
                index,
                table_name="profiles",
                remove_keys=profile_names,
            )
            if rewritten is not None:
                _trim_managed_codex_worker_profile_comments(output_lines)
                while output_lines and output_lines[-1] == _CODEX_PROFILE_COMMENT:
                    output_lines.pop()
                replacement, index = rewritten
                output_lines.extend(replacement)
                continue
        if (
            assignment_path is not None
            and len(assignment_path) >= 2
            and assignment_path[0] == "profiles"
            and assignment_path[1] in profile_names
        ):
            _trim_managed_codex_worker_profile_comments(output_lines)
            while output_lines and output_lines[-1] == _CODEX_PROFILE_COMMENT:
                output_lines.pop()
            index = _toml_assignment_span_end(
                input_lines,
                index,
                parent_path=current_table_path or (),
            )
            continue

        output_lines.append(input_lines[index])
        index += 1

    return "\n".join(output_lines).rstrip() + ("\n" if output_lines else ""), migrated


def _write_codex_profile_v2_files(
    codex_dir: Path, profile_settings: dict[str, _CodexProfileSettings]
) -> list[str]:
    """Create missing ``<profile>.config.toml`` files without overwriting users."""
    added_profiles: list[str] = []
    codex_dir.mkdir(parents=True, exist_ok=True)
    for name, settings in profile_settings.items():
        profile_path = codex_dir / f"{name}.config.toml"
        if profile_path.exists():
            continue
        profile_path.write_text(_render_codex_profile_v2_file(settings), encoding="utf-8")
        added_profiles.append(name)
    return added_profiles


def _render_codex_worker_profile_v2_file(settings: _CodexProfileSettings) -> str:
    """Render the managed Codex worker profile-v2 file."""
    return (
        _CODEX_WORKER_PROFILE_V2_CONTENT
        + "\n".join(_render_toml_mapping(settings)).rstrip()
        + ("\n" if settings else "")
    )


def _codex_worker_profile_v2_matches_migrated_legacy(
    profile_path: Path,
    settings: _CodexProfileSettings,
) -> bool:
    """Return whether an existing worker profile-v2 file is our migration output."""
    try:
        return profile_path.read_text(encoding="utf-8") == _render_codex_worker_profile_v2_file(
            settings
        )
    except OSError:
        return False


def _remove_created_profile_v2_file_if_current_matches(
    profile_path: Path,
    expected_content: str,
) -> None:
    """Remove a just-created worker profile file if it still matches setup output."""
    try:
        raw = profile_path.read_text(encoding="utf-8")
    except OSError:
        return
    if raw != expected_content:
        return
    try:
        profile_path.unlink()
    except OSError:
        pass


def _codex_worker_profile_v2_is_recoverable_migration(
    profile_path: Path,
    settings: _CodexProfileSettings,
) -> bool:
    """Return whether a v2 worker file is a complete or interrupted managed migration."""
    expected = _render_codex_worker_profile_v2_file(settings)
    try:
        contents = profile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return contents == expected


def _codex_profile_v2_file_exists(codex_dir: Path, profile_name: str) -> bool:
    """Return whether a Codex profile-v2 file already exists for a profile."""
    return (codex_dir / f"{profile_name}.config.toml").exists()


def _warn_preserved_legacy_codex_profiles(codex_config: Path, profile_names: set[str]) -> None:
    """Warn when setup preserves legacy profiles to avoid deleting user overrides."""
    if not profile_names:
        return

    profiles = ", ".join(sorted(profile_names))
    print_warning(
        "Preserved legacy Codex profile table(s) in "
        f"{codex_config} because matching profile-v2 file(s) already exist: {profiles}. "
        "If your Codex CLI rejects startup because both legacy and profile-v2 profiles "
        "are present, manually reconcile the settings and remove the legacy "
        "[profiles.<name>] table(s)."
    )


def _retire_codex_default_profiles(
    *,
    protected_profile_names: set[str] | None = None,
    expected_snapshots: dict[Path, _PathSnapshot] | None = None,
) -> None:
    """Remove only untouched task-profile anchors superseded by per-run effort.

    The task profiles used to contain nothing but a reasoning-effort setting.
    That setting is now supplied with ``codex exec -c``.  Never remove a
    profile whose parsed content differs from the generated default.
    """
    import tomllib

    protected = protected_profile_names or set()
    codex_dir = resolve_codex_home()
    codex_config = codex_dir / "config.toml"
    removed: list[str] = []
    if codex_config.exists():
        try:
            expected_config = (
                expected_snapshots[codex_config]
                if expected_snapshots is not None and codex_config in expected_snapshots
                else _snapshot_path(codex_config)
            )
            config_read_snapshot = _require_path_snapshot(codex_config, expected_config)
            raw = codex_config.read_text(encoding="utf-8")
            parsed = tomllib.loads(raw)
            profiles = parsed.get("profiles")
            removable = {
                name
                for name, settings in _CODEX_DEFAULT_PROFILE_SECTIONS.items()
                if name not in protected
                if isinstance(profiles, dict) and profiles.get(name) == settings
                if _legacy_codex_profile_section_is_generated(raw, name, settings)
            }
            if removable:
                updated_raw, _ = _remove_codex_legacy_profile_sections(raw, removable)
                written_snapshot = _atomic_write_text_if_current_matches(
                    codex_config,
                    updated_raw,
                    config_read_snapshot,
                )
                if expected_snapshots is not None:
                    expected_snapshots[codex_config] = written_snapshot
                removed.extend(sorted(removable))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Could not parse {codex_config}: {exc}") from exc

    for name, settings in _CODEX_DEFAULT_PROFILE_SECTIONS.items():
        if name in protected:
            continue
        profile_path = codex_dir / f"{name}.config.toml"
        try:
            expected_profile = (
                expected_snapshots[profile_path]
                if expected_snapshots is not None and profile_path in expected_snapshots
                else _snapshot_path(profile_path)
            )
            profile_read_snapshot = _require_path_snapshot(profile_path, expected_profile)
            if profile_path.read_text(encoding="utf-8") == _render_codex_profile_v2_file(settings):
                _require_path_snapshot(profile_path, profile_read_snapshot)
                profile_path.unlink()
                if expected_snapshots is not None:
                    expected_snapshots[profile_path] = _PathSnapshot(kind="missing")
                removed.append(name)
        except _ConcurrentSetupMutationError:
            raise
        except OSError:
            continue

    if removed:
        print_info(
            "Retired superseded Codex task profile anchors: " + ", ".join(sorted(set(removed)))
        )


def _legacy_codex_profile_is_customized(profile_name: str) -> bool:
    """Return whether an old Ouroboros-owned Codex anchor was edited by its user."""
    import tomllib

    expected = _CODEX_DEFAULT_PROFILE_SECTIONS[profile_name]
    codex_dir = resolve_codex_home()
    codex_config = codex_dir / "config.toml"
    if codex_config.exists():
        try:
            raw = codex_config.read_text(encoding="utf-8")
            profiles = tomllib.loads(raw).get("profiles")
        except (OSError, tomllib.TOMLDecodeError):
            # A malformed file is not ours to reinterpret or modify.
            return True
        if isinstance(profiles, dict) and profile_name in profiles:
            if not _legacy_codex_profile_section_is_generated(raw, profile_name, expected):
                return True

    profile_path = codex_dir / f"{profile_name}.config.toml"
    try:
        if profile_path.exists() and profile_path.read_text(
            encoding="utf-8"
        ) != _render_codex_profile_v2_file(expected):
            return True
    except OSError:
        return True
    return False


def _migrate_legacy_codex_profile_mappings(config_dict: dict) -> list[str]:
    """Move untouched setup-owned profile references to per-call effort settings.

    Earlier setup releases wrote both ``profile: ouroboros-*`` in config.yaml
    and a matching native Codex profile anchor.  The two must migrate together:
    removing only the anchor would leave existing users with a dangling
    ``--profile`` reference.  A customized native anchor is deliberately left
    intact because it may contain a model pin the user chose.
    """
    llm_profiles = config_dict.get("llm_profiles")
    if not isinstance(llm_profiles, dict):
        return []

    migrated: list[str] = []
    for task_profile, default in _CODEX_DEFAULT_LLM_PROFILES.items():
        profile = llm_profiles.get(task_profile)
        if not isinstance(profile, dict):
            continue
        providers = profile.get("providers")
        if not isinstance(providers, dict):
            continue
        codex_provider = providers.get("codex")
        if not isinstance(codex_provider, dict):
            continue

        native_name = f"ouroboros-{task_profile}"
        if codex_provider.get("profile") != native_name:
            continue
        if "model" in codex_provider or "reasoning_effort" in codex_provider:
            continue
        if _legacy_codex_profile_is_customized(native_name):
            continue

        codex_provider.pop("profile")
        codex_provider["reasoning_effort"] = default["providers"]["codex"]["reasoning_effort"]  # type: ignore[index]
        migrated.append(task_profile)
    return migrated


_CODEX_PROVIDER_ALIASES = frozenset({"codex", "codex_cli"})


def _is_codex_provider_alias(provider_name: object) -> bool:
    """Return whether a config provider key resolves to the Codex backend."""
    return (
        isinstance(provider_name, str) and provider_name.strip().lower() in _CODEX_PROVIDER_ALIASES
    )


def _referenced_legacy_codex_profiles(config_dict: dict) -> set[str]:
    """Find old task anchors still intentionally referenced from config.yaml."""
    llm_profiles = config_dict.get("llm_profiles")
    if not isinstance(llm_profiles, dict):
        return set()
    known = set(_CODEX_DEFAULT_PROFILE_SECTIONS)
    referenced: set[str] = set()
    for profile in llm_profiles.values():
        if not isinstance(profile, dict) or not isinstance(profile.get("providers"), dict):
            continue
        for provider_name, provider_config in profile["providers"].items():
            if not _is_codex_provider_alias(provider_name) or not isinstance(provider_config, dict):
                continue
            profile_name = provider_config.get("profile")
            if isinstance(profile_name, str) and profile_name in known:
                referenced.add(profile_name)
    return referenced


def _register_codex_default_profiles(*, codex_path: str | None = None) -> None:
    """Register default Codex profile anchors for Ouroboros task profiles."""
    import tomllib

    codex_config = resolve_codex_home() / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    raw = codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""

    if _codex_uses_profile_v2(codex_path):
        existing_v2_profiles = {
            name
            for name in _CODEX_DEFAULT_PROFILE_SECTIONS
            if _codex_profile_v2_file_exists(codex_config.parent, name)
        }
        try:
            legacy_profiles = _existing_codex_profile_names(raw)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} - skipping Codex profile registration.")
            print_info(str(exc))
            return
        preserved_legacy_profiles = legacy_profiles & existing_v2_profiles
        removable_profiles = set(_CODEX_DEFAULT_PROFILE_SECTIONS) - existing_v2_profiles
        try:
            updated_raw, migrated_profiles = _remove_codex_legacy_profile_sections(
                raw, removable_profiles
            )
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} - skipping Codex profile registration.")
            print_info(str(exc))
            return

        profile_settings = {
            name: migrated_profiles.get(name, settings)
            for name, settings in _CODEX_DEFAULT_PROFILE_SECTIONS.items()
        }
        added_profiles = _write_codex_profile_v2_files(codex_config.parent, profile_settings)
        if updated_raw != raw:
            try:
                tomllib.loads(updated_raw)
            except tomllib.TOMLDecodeError as exc:
                print_error(
                    f"Could not update {codex_config} — profile migration would create invalid TOML."
                )
                print_info(str(exc))
                return
            _atomic_write_text(codex_config, updated_raw)
        _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)

        if added_profiles:
            print_success(
                "Registered Codex profile-v2 task profiles in "
                f"{codex_config.parent}: {', '.join(added_profiles)}"
            )
        elif updated_raw != raw:
            print_success(f"Migrated legacy Codex task profiles out of {codex_config}")
        else:
            print_info("Codex Ouroboros task profile-v2 files already present.")
        return

    try:
        updated_raw, added_profiles = _upsert_codex_profile_sections(raw)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        print_error(f"Could not parse {codex_config} - skipping Codex profile registration.")
        print_info(str(exc))
        return

    if not added_profiles:
        print_info("Codex Ouroboros task profiles already present.")
        return

    _atomic_write_text(codex_config, updated_raw)
    print_success(f"Registered Codex task profiles in {codex_config}: {', '.join(added_profiles)}")


def _register_codex_worker_profile(
    *,
    codex_path: str | None = None,
    expected_snapshots: dict[Path, _PathSnapshot] | None = None,
) -> bool:
    """Register the managed Codex worker profile in ~/.codex/config.toml."""
    import tomllib

    codex_config = resolve_codex_home() / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    expected_config = (
        expected_snapshots[codex_config]
        if expected_snapshots is not None and codex_config in expected_snapshots
        else _snapshot_path(codex_config)
    )
    config_read_snapshot = _require_path_snapshot(codex_config, expected_config)

    if codex_config.exists():
        raw = codex_config.read_text(encoding="utf-8")
        try:
            _existing_codex_profile_names(raw)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} — skipping worker-profile registration.")
            print_info(str(exc))
            return False
    else:
        raw = ""

    if _codex_uses_profile_v2(codex_path):
        profile_path = codex_config.parent / f"{_CODEX_WORKER_PROFILE_NAME}.config.toml"
        legacy_profiles = _existing_codex_profile_names(raw)
        try:
            migrated_raw, migrated_profiles = _remove_codex_legacy_profile_sections(
                raw, {_CODEX_WORKER_PROFILE_NAME}
            )
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} — skipping worker-profile registration.")
            print_info(str(exc))
            return False

        settings = migrated_profiles.get(_CODEX_WORKER_PROFILE_NAME, {})
        profile_exists = _codex_profile_v2_file_exists(
            codex_config.parent, _CODEX_WORKER_PROFILE_NAME
        )
        recover_interrupted_migration = (
            _CODEX_WORKER_PROFILE_NAME in legacy_profiles
            and profile_exists
            and _codex_worker_profile_v2_is_recoverable_migration(profile_path, settings)
        )
        removable_profiles = (
            {_CODEX_WORKER_PROFILE_NAME}
            if not profile_exists or recover_interrupted_migration
            else set()
        )
        updated_raw = migrated_raw if removable_profiles else raw
        preserved_legacy_profiles = (
            legacy_profiles & {_CODEX_WORKER_PROFILE_NAME} - removable_profiles
        )
        created_profile = False
        profile_snapshot: _PathSnapshot | None = None
        profile_expected_snapshot: _PathSnapshot | None = None
        profile_content: str | None = None

        def _rollback_profile_write() -> None:
            if not created_profile or profile_content is None:
                return
            if not profile_exists:
                _remove_created_profile_v2_file_if_current_matches(
                    profile_path,
                    profile_content,
                )
                return
            if profile_snapshot is not None:
                _restore_path_snapshot_if_current_matches(
                    profile_path,
                    profile_snapshot,
                    profile_expected_snapshot,
                    restore_link_targets=False,
                )

        if not profile_exists or recover_interrupted_migration:
            profile_content = _render_codex_worker_profile_v2_file(settings)
            try:
                tomllib.loads(profile_content)
            except tomllib.TOMLDecodeError as exc:
                print_error(
                    f"Could not update {profile_path} — worker-profile migration would create invalid TOML."
                )
                print_info(str(exc))
                return False
            expected_profile = (
                expected_snapshots[profile_path]
                if expected_snapshots is not None and profile_path in expected_snapshots
                else _snapshot_path(profile_path)
            )
            profile_snapshot = _require_path_snapshot(profile_path, expected_profile)
            profile_expected_snapshot = _atomic_write_text_if_current_matches(
                profile_path,
                profile_content,
                profile_snapshot,
            )
            created_profile = True
        if updated_raw != raw:
            try:
                tomllib.loads(updated_raw)
            except tomllib.TOMLDecodeError as exc:
                print_error(
                    f"Could not update {codex_config} — worker-profile migration would create invalid TOML."
                )
                print_info(str(exc))
                _rollback_profile_write()
                return False
            try:
                written_config_snapshot = _atomic_write_text_if_current_matches(
                    codex_config,
                    updated_raw,
                    config_read_snapshot,
                )
            except _ConcurrentSetupMutationError as exc:
                print_error(str(exc))
                _rollback_profile_write()
                return False
            except OSError:
                _rollback_profile_write()
                raise
            if expected_snapshots is not None:
                expected_snapshots[codex_config] = written_config_snapshot
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_success(f"Migrated legacy Codex worker profile out of {codex_config}")
        elif created_profile:
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_success(f"Registered Codex worker profile-v2 file in {profile_path}")
        else:
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_info("Codex worker profile-v2 file already present.")
        if expected_snapshots is not None and created_profile:
            assert profile_expected_snapshot is not None
            expected_snapshots[profile_path] = profile_expected_snapshot
        return True

    updated_raw, existed_before = _upsert_codex_worker_profile_section(raw)
    try:
        tomllib.loads(updated_raw)
    except tomllib.TOMLDecodeError as exc:
        print_error(
            f"Could not update {codex_config} — worker-profile registration would create invalid TOML."
        )
        print_info(str(exc))
        return False
    if updated_raw == raw:
        print_info("Codex worker profile already up to date.")
        return True

    written_config_snapshot = _atomic_write_text_if_current_matches(
        codex_config,
        updated_raw,
        config_read_snapshot,
    )
    if expected_snapshots is not None:
        expected_snapshots[codex_config] = written_config_snapshot
    if existed_before:
        print_success(f"Updated Codex worker profile in {codex_config}")
    else:
        print_success(f"Registered Codex worker profile in {codex_config}")
    return True


def _ensure_mapping_section(config_dict: dict, key: str) -> dict:
    """Ensure a top-level YAML section is a mapping before mutating it."""
    section = config_dict.get(key)
    if isinstance(section, dict):
        return section
    if section is not None:
        msg = f"Invalid non-mapping {key!r} section in config.yaml."
        raise ValueError(msg)
    section = {}
    config_dict[key] = section
    return section


def _ensure_profile_provider_mapping(profile: dict, provider: str) -> dict:
    """Ensure an llm_profiles entry has a mutable provider mapping."""
    providers = profile.get("providers")
    if not isinstance(providers, dict):
        if providers is not None:
            msg = "Invalid non-mapping 'providers' in an LLM profile."
            raise ValueError(msg)
        providers = {}
        profile["providers"] = providers

    provider_config = providers.get(provider)
    if isinstance(provider_config, dict):
        return provider_config
    if provider_config is not None:
        msg = f"Invalid non-mapping {provider!r} provider profile."
        raise ValueError(msg)
    provider_config = {}
    providers[provider] = provider_config
    return provider_config


def _ensure_codex_profile_provider_mapping(profile: dict) -> dict:
    """Return the existing Codex provider slot, or create the canonical one.

    ``codex`` and ``codex_cli`` resolve to the same backend.  In particular,
    do not add an effort-only ``codex`` mapping beside a user-owned
    ``codex_cli`` model/profile mapping: profile resolution intentionally
    prefers the exact canonical key, so doing so would silently shadow the
    user's effective pin.
    """
    providers = profile.get("providers")
    if not isinstance(providers, dict):
        if providers is not None:
            msg = "Invalid non-mapping 'providers' in an LLM profile."
            raise ValueError(msg)
        providers = {}
        profile["providers"] = providers

    codex_entries = [
        (provider, provider_config)
        for provider, provider_config in providers.items()
        if _is_codex_provider_alias(provider)
    ]
    if len(codex_entries) > 1:
        names = ", ".join(repr(provider) for provider, _ in codex_entries)
        msg = f"Duplicate Codex provider aliases in an LLM profile: {names}"
        raise ValueError(msg)

    for provider in ("codex", "codex_cli"):
        provider_config = providers.get(provider)
        if isinstance(provider_config, dict):
            return provider_config
        if provider_config is not None:
            msg = f"Invalid non-mapping {provider!r} provider profile."
            raise ValueError(msg)

    for provider, provider_config in providers.items():
        if not _is_codex_provider_alias(provider):
            continue
        if isinstance(provider_config, dict):
            return provider_config
        msg = f"Invalid non-mapping {provider!r} provider profile."
        raise ValueError(msg)

    provider_config = {}
    providers["codex"] = provider_config
    return provider_config


def _install_codex_default_llm_profiles(
    config_dict: dict,
    *,
    preserve_legacy_model_overrides: bool = True,
) -> tuple[list[str], list[str], list[str]]:
    """Install missing provider-neutral Ouroboros task profiles for Codex setup.

    Existing profile definitions and explicit legacy per-role model overrides
    are preserved. A freshly generated config is the exception: its shipped
    legacy model fields are defaults, not user pins, so callers pass
    ``preserve_legacy_model_overrides=False`` to establish every Codex role
    effort mapping. If a default profile name already exists for another
    backend, merge in only the missing Codex provider mapping before adding
    role defaults that target that profile.
    """
    llm_profiles = _ensure_mapping_section(config_dict, "llm_profiles")
    llm_role_profiles = _ensure_mapping_section(config_dict, "llm_role_profiles")

    added_profiles: list[str] = []
    updated_profiles: list[str] = []
    for name, profile in _CODEX_DEFAULT_LLM_PROFILES.items():
        if name not in llm_profiles:
            llm_profiles[name] = deepcopy(profile)
            added_profiles.append(name)
            continue

        existing_profile = llm_profiles[name]
        if not isinstance(existing_profile, dict):
            msg = f"Invalid non-mapping llm_profiles.{name!s}."
            raise ValueError(msg)

        default_codex = profile["providers"]["codex"]  # type: ignore[index]
        codex_provider = _ensure_codex_profile_provider_mapping(existing_profile)
        provider_had_profile = "profile" in codex_provider
        provider_had_model = "model" in codex_provider
        provider_had_effort = "reasoning_effort" in codex_provider
        changed = False
        if not provider_had_profile and not provider_had_model and not provider_had_effort:
            codex_provider["reasoning_effort"] = default_codex["reasoning_effort"]  # type: ignore[index]
            changed = True
        if changed:
            updated_profiles.append(name)

    added_role_profiles: list[str] = []
    for role, profile_name in _CODEX_DEFAULT_LLM_ROLE_PROFILES.items():
        if role in llm_role_profiles or (
            preserve_legacy_model_overrides
            and _has_explicit_codex_model_override(config_dict, role)
        ):
            continue
        llm_role_profiles[role] = profile_name
        added_role_profiles.append(role)

    return added_profiles, updated_profiles, added_role_profiles


def _print_codex_config_guidance(config_path: Path) -> None:
    """Explain where Codex users should configure Ouroboros vs. Codex settings."""
    print_info(f"Configure Ouroboros runtime and per-role model overrides in {config_path}.")
    print_info(
        f"Use {resolve_codex_home() / 'config.toml'} for the Codex MCP/env hookup and any profiles you manage yourself."
    )


def _codex_home_candidate_for_setup() -> Path:
    """Return Codex home exactly as supplied before symlink resolution."""
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _install_codex_artifacts(
    codex_dir: str | Path | None = None,
    *,
    expected_snapshots: dict[Path, _PathSnapshot] | None = None,
    required_snapshots: dict[Path, _PathSnapshot] | None = None,
) -> bool:
    """Install packaged Ouroboros rules and skills into ~/.codex/."""
    from ouroboros.codex import CodexArtifactGeneration, install_codex_artifacts

    install_target = _codex_home_candidate_for_setup() if codex_dir is None else codex_dir
    display_dir = Path(install_target).expanduser()
    current_snapshots = dict(required_snapshots or {})

    def _require_read_generation(target_path: Path) -> None:
        if required_snapshots is None:
            return
        try:
            expected = current_snapshots[target_path]
        except KeyError as exc:
            raise _ConcurrentSetupMutationError(
                f"Managed Codex artifact was not included in the setup snapshot: {target_path}"
            ) from exc
        _require_path_snapshot(target_path, expected)

    def _record_generation(generation: CodexArtifactGeneration) -> None:
        if generation.missing:
            written_snapshot = _PathSnapshot(kind="missing")
            current_snapshots[generation.target_path] = written_snapshot
            if expected_snapshots is not None:
                expected_snapshots[generation.target_path] = written_snapshot
            return
        if generation.source_path is None:
            raise ValueError("Artifact generation is missing its source path")
        source_snapshot = _snapshot_path(generation.source_path)
        if generation.contents is not None:
            source_snapshot = _PathSnapshot(
                kind="file",
                mode=source_snapshot.mode,
                contents=generation.contents,
            )
        current_snapshots[generation.target_path] = source_snapshot
        if expected_snapshots is not None:
            expected_snapshots[generation.target_path] = source_snapshot

    try:
        result = install_codex_artifacts(
            codex_dir=install_target,
            prune=True,
            on_generation=_record_generation,
            before_mutation=_require_read_generation,
        )
        print_success(f"Installed Codex rules → {result.rules_path}")
        print_success(
            f"Installed {len(result.skill_paths)} Codex skills → {display_dir / 'skills'}"
        )
        return True
    except (FileNotFoundError, OSError) as exc:
        print_error(f"Could not install packaged Codex rules or skills: {exc}")
        return False


def _snapshot_file(path: Path) -> bytes | None:
    """Return a file snapshot for rollback, or ``None`` if it did not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_file_snapshot(path: Path, snapshot: bytes | None) -> None:
    """Restore a file snapshot captured before a setup side effect."""
    if snapshot is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(snapshot)


@dataclass(frozen=True)
class _PathSnapshot:
    """Topology-preserving snapshot for one managed setup path."""

    kind: Literal["missing", "file", "directory", "symlink", "other"]
    mode: int | None = None
    contents: bytes | None = None
    link_target: str | None = None
    link_target_mode: int | None = None
    link_target_contents: bytes | None = None
    link_target_missing: bool = False
    link_target_snapshot: _PathSnapshot | None = None
    children: tuple[tuple[str, _PathSnapshot], ...] = ()


class _ConcurrentSetupMutationError(OSError):
    """A managed path no longer matches the generation setup read."""


def _snapshot_path(
    path: Path, *, _seen: frozenset[Path] = frozenset(), follow_links: bool = True
) -> _PathSnapshot:
    """Snapshot a managed file or directory without following symlinks."""
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return _PathSnapshot(kind="missing")

    current_path = path.expanduser().absolute()
    mode = stat.S_IMODE(stat_result.st_mode)
    if stat.S_ISLNK(stat_result.st_mode):
        link_target = os.readlink(path)
        if not follow_links:
            return _PathSnapshot(kind="symlink", mode=mode, link_target=link_target)
        target_path = Path(link_target)
        if not target_path.is_absolute():
            target_path = path.parent / target_path
        normalized_target = target_path.expanduser().absolute()
        if normalized_target in _seen:
            return _PathSnapshot(kind="symlink", mode=mode, link_target=link_target)
        target_snapshot = (
            _snapshot_path(target_path, _seen=_seen | {current_path})
            if follow_links
            else _snapshot_path(
                target_path,
                _seen=_seen | {current_path},
                follow_links=False,
            )
        )
        try:
            target_stat = target_path.lstat()
        except FileNotFoundError:
            return _PathSnapshot(
                kind="symlink",
                mode=mode,
                link_target=link_target,
                link_target_missing=True,
                link_target_snapshot=target_snapshot,
            )
        if stat.S_ISREG(target_stat.st_mode):
            return _PathSnapshot(
                kind="symlink",
                mode=mode,
                link_target=link_target,
                link_target_mode=stat.S_IMODE(target_stat.st_mode),
                link_target_contents=target_path.read_bytes(),
                link_target_snapshot=target_snapshot,
            )
        return _PathSnapshot(
            kind="symlink",
            mode=mode,
            link_target=link_target,
            link_target_snapshot=target_snapshot,
        )
    if stat.S_ISREG(stat_result.st_mode):
        return _PathSnapshot(kind="file", mode=mode, contents=path.read_bytes())
    if not stat.S_ISDIR(stat_result.st_mode):
        return _PathSnapshot(kind="other", mode=mode)

    children: list[tuple[str, _PathSnapshot]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        children.append(
            (
                child.name,
                (
                    _snapshot_path(child, _seen=_seen | {current_path})
                    if follow_links
                    else _snapshot_path(
                        child,
                        _seen=_seen | {current_path},
                        follow_links=False,
                    )
                ),
            )
        )
    return _PathSnapshot(kind="directory", mode=mode, children=tuple(children))


def _remove_path_topology(path: Path) -> None:
    """Remove a path without following directory symlinks."""
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(stat_result.st_mode) and not stat.S_ISLNK(stat_result.st_mode):
        for child in path.iterdir():
            _remove_path_topology(child)
        path.rmdir()
    else:
        path.unlink()


def _restore_path_snapshot(
    path: Path,
    snapshot: _PathSnapshot,
    *,
    restore_link_targets: bool = True,
) -> None:
    """Restore one managed path without touching sibling Codex user state."""
    if snapshot.kind == "missing":
        _remove_path_topology(path)
        return

    _remove_path_topology(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if snapshot.kind == "symlink":
        if snapshot.link_target is not None:
            os.symlink(snapshot.link_target, path)
            target_path = Path(snapshot.link_target)
            if not target_path.is_absolute():
                target_path = path.parent / target_path
            if restore_link_targets and snapshot.link_target_snapshot is not None:
                _restore_path_snapshot(target_path, snapshot.link_target_snapshot)
            elif restore_link_targets and snapshot.link_target_missing:
                _remove_path_topology(target_path)
            elif restore_link_targets and snapshot.link_target_contents is not None:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(snapshot.link_target_contents)
                if snapshot.link_target_mode is not None:
                    target_path.chmod(snapshot.link_target_mode)
        return

    if snapshot.kind == "file":
        path.write_bytes(snapshot.contents or b"")
        if snapshot.mode is not None:
            path.chmod(snapshot.mode)
        return

    if snapshot.kind == "directory":
        path.mkdir(parents=True, exist_ok=True)
        if snapshot.mode is not None:
            path.chmod(snapshot.mode)
        for child_name, child_snapshot in snapshot.children:
            _restore_path_snapshot(
                path / child_name,
                child_snapshot,
                restore_link_targets=restore_link_targets,
            )


def _restore_path_snapshot_if_current_matches(
    path: Path,
    snapshot: _PathSnapshot,
    expected_current: _PathSnapshot | None,
    *,
    restore_link_targets: bool = True,
    follow_links: bool = True,
) -> bool:
    """Restore only when the current path still matches setup's last known write."""
    if (
        expected_current is not None
        and _snapshot_path(path, follow_links=follow_links) != expected_current
    ):
        print_warning(f"Preserved concurrently changed setup path during rollback: {path}")
        return False
    _restore_path_snapshot(
        path,
        snapshot,
        restore_link_targets=restore_link_targets,
    )
    return True


def _require_path_snapshot(path: Path, expected: _PathSnapshot) -> _PathSnapshot:
    """Fail closed when a managed path changed since the caller last read it."""
    current = _snapshot_path(path)
    if current != expected:
        raise _ConcurrentSetupMutationError(f"Managed setup path changed concurrently: {path}")
    return current


def _atomic_write_text_if_current_matches(
    path: Path,
    content: str,
    expected_current: _PathSnapshot,
    *,
    mode: int | None = None,
) -> _PathSnapshot:
    """Atomically replace text only while the caller still owns the read generation."""
    _require_path_snapshot(path, expected_current)
    return _atomic_write_text(
        path,
        content,
        mode=mode,
        expected_current=expected_current,
    )


def _managed_codex_setup_paths(codex_home: Path) -> tuple[Path, ...]:
    """Return only Codex paths Ouroboros setup owns and may roll back."""
    paths: set[Path] = {
        codex_home / "config.toml",
        codex_home / f"{_CODEX_WORKER_PROFILE_NAME}.config.toml",
        *(
            codex_home / f"{profile_name}.config.toml"
            for profile_name in _CODEX_DEFAULT_PROFILE_SECTIONS
        ),
    }

    rules_dir = codex_home / "rules"
    if rules_dir.is_dir():
        paths.update(
            path
            for path in rules_dir.iterdir()
            if path.name == "ouroboros.md"
            or (path.name.startswith("ouroboros-") and path.suffix == ".md")
        )
    elif rules_dir.exists():
        paths.add(rules_dir)
    skills_dir = codex_home / "skills"
    if skills_dir.is_dir():
        paths.update(path for path in skills_dir.iterdir() if path.name.startswith("ouroboros-"))
    elif skills_dir.exists():
        paths.add(skills_dir)

    try:
        from ouroboros.codex import resolve_packaged_codex_assets

        with resolve_packaged_codex_assets() as assets:
            paths.update(
                codex_home / artifact.relative_install_path for artifact in assets.managed_artifacts
            )
    except (FileNotFoundError, OSError, ValueError):
        # Artifact installation will surface the real failure later.  Rollback
        # still protects already-installed managed paths discovered above.
        pass

    return tuple(sorted(paths))


def _snapshot_managed_codex_setup_paths(
    codex_home: Path,
) -> dict[Path, _PathSnapshot]:
    """Snapshot setup-owned Codex paths without snapshotting the whole home."""
    return {path: _snapshot_path(path) for path in _managed_codex_setup_paths(codex_home)}


def _restore_managed_codex_setup_paths(
    snapshot: dict[Path, _PathSnapshot],
    *,
    expected_current: dict[Path, _PathSnapshot] | None = None,
) -> None:
    """Restore only setup-owned Codex paths captured before setup."""
    for path, path_snapshot in snapshot.items():
        _restore_path_snapshot_if_current_matches(
            path,
            path_snapshot,
            None if expected_current is None else expected_current.get(path),
            restore_link_targets=False,
        )


def _snapshot_created_directory_topology(paths: tuple[Path, ...]) -> dict[Path, bool]:
    """Record which setup-created parent directories did not exist beforehand."""
    return {path: path.exists() for path in paths}


def _restore_created_directory_topology(snapshot: dict[Path, bool]) -> None:
    """Remove directories setup created, but only when they are still empty."""
    for path, existed_before in sorted(
        snapshot.items(),
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        if existed_before:
            continue
        try:
            path.rmdir()
        except (FileNotFoundError, NotADirectoryError, OSError):
            pass


def _find_managed_codex_symlink_conflicts(codex_home: Path) -> list[Path]:
    """Return managed Codex paths that setup would write through as symlinks."""
    candidates: set[Path] = {
        codex_home / "config.toml",
        codex_home / f"{_CODEX_WORKER_PROFILE_NAME}.config.toml",
        codex_home / "rules",
        codex_home / "skills",
        *(
            codex_home / f"{profile_name}.config.toml"
            for profile_name in _CODEX_DEFAULT_PROFILE_SECTIONS
        ),
    }
    for path in _managed_codex_setup_paths(codex_home):
        candidates.add(path)
        parent = path.parent
        while parent != codex_home and codex_home in (parent, *parent.parents):
            candidates.add(parent)
            parent = parent.parent

    conflicts: list[Path] = []
    for path in sorted(candidates):
        try:
            if path.is_symlink():
                conflicts.append(path)
        except OSError:
            conflicts.append(path)
    return conflicts


def _find_managed_codex_topology_conflicts(codex_home: Path) -> list[Path]:
    """Return managed Codex directories blocked by non-directory leaves."""
    conflicts: list[Path] = []
    for directory in (codex_home / "rules", codex_home / "skills"):
        try:
            stat_result = directory.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(stat_result.st_mode):
            continue
        if not stat.S_ISDIR(stat_result.st_mode):
            conflicts.append(directory)
    return conflicts


def _find_ouroboros_config_symlink_conflicts(config_dir: Path) -> list[Path]:
    """Return Ouroboros setup files that are unsafe write-through symlinks."""
    conflicts: list[Path] = []
    for path in (config_dir / "config.yaml", config_dir / "credentials.yaml"):
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                conflicts.append(path)
        except FileNotFoundError:
            continue
        except OSError:
            conflicts.append(path)
    return conflicts


def _snapshot_directory(path: Path) -> dict[Path, bytes]:
    """Return a recursive file snapshot for rollback."""
    snapshot: dict[Path, bytes] = {}
    if not path.exists():
        return snapshot
    for child in path.rglob("*"):
        if child.is_file():
            snapshot[child.relative_to(path)] = child.read_bytes()
    return snapshot


def _restore_directory_snapshot(path: Path, snapshot: dict[Path, bytes]) -> None:
    """Restore a directory snapshot, including files deleted during setup."""
    if path.exists():
        for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if child.is_file() and child.relative_to(path) not in snapshot:
                child.unlink()
            elif child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass

    for relative_path, contents in snapshot.items():
        target = path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


def _config_execute_runtime_backend(config_dict: dict) -> str:
    """Return the saved Execute runtime backend using setup's inheritance rules."""
    orchestrator = config_dict.get("orchestrator")
    if not isinstance(orchestrator, dict):
        return "claude"
    runtime_profile = orchestrator.get("runtime_profile")
    stage_backend = None
    profile_default = None
    if isinstance(runtime_profile, dict):
        stages = runtime_profile.get("stages")
        if isinstance(stages, dict):
            stage_backend = stages.get("execute")
        profile_default = runtime_profile.get("default")
    backend = str(
        stage_backend or profile_default or orchestrator.get("runtime_backend") or "claude"
    )
    return "codex" if backend.strip().lower() in {"codex", "codex_cli"} else backend


def _setup_codex(
    codex_path: str,
    *,
    mcp_mode: CodexMcpMode = "auto",
    preserve_existing_llm: bool = False,
) -> bool:
    """Configure Ouroboros for the Codex runtime."""
    from ouroboros.config.loader import ensure_config_dir, get_default_config
    from ouroboros.config.models import get_config_dir, get_default_credentials

    codex_home = resolve_codex_home()
    config_dir_candidate = get_config_dir()
    setup_directory_topology_snapshot = _snapshot_created_directory_topology(
        (
            config_dir_candidate / "data",
            config_dir_candidate / "logs",
            config_dir_candidate,
            codex_home,
            codex_home / "rules",
            codex_home / "skills",
        )
    )
    config_dir = ensure_config_dir()
    ouroboros_symlink_conflicts = _find_ouroboros_config_symlink_conflicts(config_dir)
    if ouroboros_symlink_conflicts:
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        formatted = ", ".join(str(path) for path in ouroboros_symlink_conflicts)
        print_error(
            f"Codex setup refuses to rewrite Ouroboros config through symlinks: {formatted}"
        )
        print_info("Replace the symlink with a regular Ouroboros config file, then rerun setup.")
        return False
    config_path = config_dir / "config.yaml"
    credentials_path = config_dir / "credentials.yaml"
    config_snapshot = _snapshot_path(config_path)
    credentials_snapshot = _snapshot_path(credentials_path)
    fresh_config = config_snapshot.kind == "missing"

    if not fresh_config:
        try:
            config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            _restore_created_directory_topology(setup_directory_topology_snapshot)
            print_error(f"Could not read config.yaml; aborting without changes: {exc}")
            return False
    else:
        config_dict = get_default_config().model_dump(mode="json")

    if not isinstance(config_dict, dict):
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_error("Invalid non-mapping config.yaml contents; aborting without changes.")
        return False

    try:
        previous_execute_backend = _config_execute_runtime_backend(config_dict)
        # Runtime integration and authoring/evaluation provider selection are
        # independent. Interactive setup keeps its historical behavior, while
        # updater-driven refreshes preserve an explicitly split LLM backend.
        orchestrator_config = _ensure_mapping_section(config_dict, "orchestrator")
        orchestrator_config["runtime_backend"] = "codex"
        orchestrator_config["codex_cli_path"] = codex_path

        llm_config = _ensure_mapping_section(config_dict, "llm")
        if not preserve_existing_llm or fresh_config or not llm_config.get("backend"):
            llm_config["backend"] = "codex"

        if fresh_config:
            _neutralize_fresh_codex_model_defaults(config_dict)

        added_profiles, updated_profiles, added_role_profiles = _install_codex_default_llm_profiles(
            config_dict,
            preserve_legacy_model_overrides=not fresh_config,
        )
        next_execute_backend = _config_execute_runtime_backend(config_dict)
        execution_config = config_dict.get("execution")
        if (
            isinstance(execution_config, dict)
            and "default_model" in execution_config
            and previous_execute_backend != next_execute_backend
            and next_execute_backend == "codex"
        ):
            execution_config["default_model"] = None
        migrated_legacy_profiles = _migrate_legacy_codex_profile_mappings(config_dict)
        protected_legacy_profiles = _referenced_legacy_codex_profiles(config_dict)
    except ValueError as exc:
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_error(f"Invalid config.yaml structure: {exc}")
        print_info("Aborting Codex setup without rewriting config.yaml.")
        return False

    # Register MCP before committing Codex runtime selection.  A config.yaml that
    # says "codex" without a launchable Codex MCP endpoint strands first-use
    # setup in a false-success state.
    topology_conflicts = _find_managed_codex_topology_conflicts(codex_home)
    if topology_conflicts:
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        formatted = ", ".join(str(path) for path in topology_conflicts)
        print_error(
            f"Codex setup refuses to install managed rules/skills over non-directories: {formatted}"
        )
        print_info("Move or remove the stale file, then rerun setup.")
        return False
    symlink_conflicts = _find_managed_codex_symlink_conflicts(codex_home)
    if symlink_conflicts:
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        formatted = ", ".join(str(path) for path in symlink_conflicts)
        print_error(f"Codex setup refuses to rewrite managed paths through symlinks: {formatted}")
        print_info("Replace the symlink with a regular Codex config path, then rerun setup.")
        return False
    managed_codex_snapshot = _snapshot_managed_codex_setup_paths(codex_home)
    managed_codex_expected_snapshot: dict[Path, _PathSnapshot] | None = None
    config_expected_snapshot: _PathSnapshot | None = config_snapshot
    credentials_expected_snapshot: _PathSnapshot | None = credentials_snapshot
    try:
        mcp_expected_snapshot: dict[Path, _PathSnapshot] = {}
        mcp_registered = _register_codex_mcp_server(
            mode=mcp_mode,
            expected_snapshots=mcp_expected_snapshot,
        )
    except OSError as exc:
        _restore_managed_codex_setup_paths(
            managed_codex_snapshot,
            expected_current=managed_codex_snapshot,
        )
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_error(f"Could not save Codex MCP config: {exc}")
        print_info("Restored unchanged Codex MCP config paths; setup incomplete.")
        return False
    if not mcp_registered:
        _restore_managed_codex_setup_paths(
            managed_codex_snapshot,
            expected_current=managed_codex_snapshot,
        )
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_info("Aborting Codex setup without rewriting config.yaml.")
        return False
    if mcp_expected_snapshot:
        managed_codex_expected_snapshot = dict(managed_codex_snapshot)
        managed_codex_expected_snapshot.update(mcp_expected_snapshot)
    else:
        # A successful registrar that authored no managed path (for example a
        # test double or a future no-op fast path) must not absorb concurrent
        # operator edits into setup's rollback generation.
        managed_codex_expected_snapshot = dict(managed_codex_snapshot)

    try:
        config_expected_snapshot = _atomic_write_text_if_current_matches(
            config_path,
            yaml.dump(config_dict, default_flow_style=False, sort_keys=False),
            config_snapshot,
        )
        if fresh_config and not credentials_path.exists():
            credentials_dict = get_default_credentials().model_dump(mode="json")
            credentials_expected_snapshot = _atomic_write_text_if_current_matches(
                credentials_path,
                yaml.dump(credentials_dict, default_flow_style=False, sort_keys=False),
                credentials_snapshot,
                mode=0o600,
            )
    except OSError as exc:
        if fresh_config and credentials_snapshot.kind == "missing" and credentials_path.exists():
            credentials_expected_snapshot = _snapshot_path(credentials_path)
        _restore_managed_codex_setup_paths(
            managed_codex_snapshot,
            expected_current=managed_codex_expected_snapshot,
        )
        _restore_path_snapshot_if_current_matches(
            config_path,
            config_snapshot,
            config_expected_snapshot,
        )
        _restore_path_snapshot_if_current_matches(
            credentials_path,
            credentials_snapshot,
            credentials_expected_snapshot,
        )
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_error(f"Could not save Codex runtime config: {exc}")
        print_info("Restored Codex MCP config; setup incomplete.")
        return False

    try:
        # Install Codex-native rules and skills into the active Codex home.
        artifact_expected_snapshots: dict[Path, _PathSnapshot] = {}
        if not _install_codex_artifacts(
            expected_snapshots=artifact_expected_snapshots,
            required_snapshots=managed_codex_expected_snapshot,
        ):
            if managed_codex_expected_snapshot is not None:
                managed_codex_expected_snapshot.update(artifact_expected_snapshots)
            raise OSError("Codex artifact installation failed")
        if managed_codex_expected_snapshot is not None:
            managed_codex_expected_snapshot.update(artifact_expected_snapshots)
        _retire_codex_default_profiles(
            protected_profile_names=protected_legacy_profiles,
            expected_snapshots=managed_codex_expected_snapshot,
        )
        if not _register_codex_worker_profile(
            codex_path=codex_path,
            expected_snapshots=managed_codex_expected_snapshot,
        ):
            raise OSError("Codex worker profile registration failed")
    except (OSError, TypeError, ValueError) as exc:
        _restore_managed_codex_setup_paths(
            managed_codex_snapshot,
            expected_current=managed_codex_expected_snapshot,
        )
        _restore_path_snapshot_if_current_matches(
            config_path,
            config_snapshot,
            config_expected_snapshot,
        )
        _restore_path_snapshot_if_current_matches(
            credentials_path,
            credentials_snapshot,
            credentials_expected_snapshot,
        )
        _restore_created_directory_topology(setup_directory_topology_snapshot)
        print_error(f"Could not finish Codex setup: {exc}")
        print_info("Restored Codex and Ouroboros config; setup incomplete.")
        return False
    print_success(f"Configured Codex runtime (CLI: {codex_path})")
    print_info(f"Config saved to: {config_path}")
    if added_profiles:
        print_info(f"Installed Ouroboros LLM profiles: {', '.join(added_profiles)}")
    if updated_profiles:
        print_info(
            "Added Codex provider mappings to existing Ouroboros LLM profiles: "
            f"{', '.join(updated_profiles)}"
        )
    if migrated_legacy_profiles:
        print_info(
            "Migrated legacy Codex task profiles to per-run reasoning effort: "
            + ", ".join(migrated_legacy_profiles)
        )
    if added_role_profiles:
        print_info(
            f"Installed Ouroboros role profile defaults for {len(added_role_profiles)} roles."
        )
    _print_codex_config_guidance(config_path)
    return True


def _install_hermes_artifacts() -> object | bool:
    """Install packaged Ouroboros skills into ~/.hermes/."""
    from ouroboros.hermes.artifacts import install_hermes_skills

    hermes_dir = Path.home() / ".hermes"

    try:
        publication = install_hermes_skills(
            hermes_dir=hermes_dir,
            prune=True,
            return_receipt=True,
        )
        print_success(f"Installed Hermes skills → {publication.target}")
    except OSError as exc:
        print_error(f"Could not install packaged skills for Hermes: {exc}")
        return False
    return publication


def _install_runtime_instruction_artifact(backend: str, **kwargs: object) -> bool:
    """Install a setup-owned runtime instruction artifact when supported."""
    from ouroboros.runtime_instruction_artifacts import (
        install_copilot_instruction_artifact,
        install_gemini_instruction_artifact,
        install_gjc_instruction_artifact,
        install_kiro_instruction_artifact,
        install_opencode_instruction_artifact,
    )

    installers = {
        "opencode": install_opencode_instruction_artifact,
        "gemini": install_gemini_instruction_artifact,
        "kiro": install_kiro_instruction_artifact,
        "copilot": install_copilot_instruction_artifact,
        "gjc": install_gjc_instruction_artifact,
    }
    installer = installers.get(backend)
    if installer is None:
        return False
    try:
        artifact = installer(**kwargs)
    except OSError as exc:
        print_warning(f"Could not install {backend} instruction artifact: {exc}")
        return False
    print_success(f"Installed {backend.title()} instruction guide → {artifact.path}")
    return True


def _register_hermes_mcp_server(*, detected: dict[str, object] | None = None) -> bool:
    """Register the Ouroboros MCP hookup in ~/.hermes/config.yaml."""
    detected = detected or _detect_mcp_entry()
    if detected is None:
        print_error(
            "Cannot register Hermes MCP server without an isolated launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    hermes_config = Path.home() / ".hermes" / "config.yaml"
    hermes_config.parent.mkdir(parents=True, exist_ok=True)

    config_data: dict = {}
    if hermes_config.exists():
        try:
            loaded_config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
        except Exception:
            print_error(f"Could not parse {hermes_config} — skipping MCP registration.")
            return False
        if loaded_config is None:
            config_data = {}
        elif isinstance(loaded_config, dict):
            config_data = loaded_config
        else:
            print_warning(f"{hermes_config} top-level is not a mapping — resetting.")
            config_data = {}

    mcp_servers = config_data.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        if mcp_servers is not None:
            print_warning(f"{hermes_config} 'mcp_servers' section is not a mapping — resetting.")
        config_data["mcp_servers"] = {}

    config_data["mcp_servers"]["ouroboros"] = {
        "command": detected["command"],
        "args": detected["args"],
        "enabled": True,
    }

    _atomic_write_text(
        hermes_config,
        yaml.safe_dump(config_data, default_flow_style=False, sort_keys=False),
    )

    print_success(f"Registered Ouroboros MCP server in {hermes_config}")
    return True


def _setup_hermes(hermes_path: str) -> bool:
    """Configure Ouroboros for the Hermes runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir
    from ouroboros.config.models import get_default_config

    detected = _detect_mcp_entry()
    if detected is None:
        print_error(
            "Hermes setup requires an isolated MCP 2 launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    config_was_missing = not config_path.exists()

    if not config_was_missing:
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        config_dict = get_default_config().model_dump(mode="json")

    if not isinstance(config_dict, dict):
        print_warning("~/.ouroboros/config.yaml top-level is not a mapping — resetting.")
        config_dict = {}

    # Set runtime to Hermes. Do not rewrite llm.backend until Hermes also
    # supports the LLM-only adapter contract used elsewhere in Ouroboros.
    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "hermes"
    orch["hermes_cli_path"] = hermes_path
    from ouroboros.hermes.artifacts import (
        HERMES_SKILL_CATEGORY,
        HERMES_SKILL_NAME,
        HermesPublicationReceipt,
    )

    hermes_skill_target = (
        Path.home() / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME
    )
    if hermes_skill_target.is_symlink():
        print_error(f"Hermes activation refused symlinked skill target: {hermes_skill_target}")
        return False
    hermes_skill_snapshot = _snapshot_path(hermes_skill_target, follow_links=False)
    hermes_publication = _install_hermes_artifacts()
    if not hermes_publication:
        print_error("Hermes runtime activation incomplete: required skills were not installed.")
        return False

    try:
        hermes_skill_published = _snapshot_path(hermes_skill_target, follow_links=False)
    except OSError as exc:
        print_error(f"Hermes activation could not snapshot published skills: {exc}")
        try:
            restored = isinstance(
                hermes_publication, HermesPublicationReceipt
            ) and restore_hermes_receipt(
                hermes_skill_target,
                hermes_skill_snapshot,
                hermes_publication,
            )
            if not restored:
                print_warning("Preserved concurrent Hermes skill changes during rollback.")
        except OSError as rollback_exc:
            print_error(f"Hermes activation rollback could not restore skills: {rollback_exc}")
        return False

    def rollback_hermes_skills() -> None:
        try:
            if isinstance(
                hermes_publication, HermesPublicationReceipt
            ) and not hermes_publication.matches(hermes_skill_target):
                print_warning("Preserved concurrent Hermes skill changes during rollback.")
                return
            restored = restore_hermes(
                hermes_skill_target, hermes_skill_snapshot, hermes_skill_published
            )
            if not restored:
                print_warning("Preserved concurrent Hermes skill changes during rollback.")
        except OSError as exc:
            print_error(f"Hermes activation rollback could not restore skills: {exc}")

    if not _commit_runtime_activation(
        runtime_name="Hermes",
        host_path=Path.home() / ".hermes" / "config.yaml",
        config_path=config_path,
        config_was_missing=config_was_missing,
        runtime_content=yaml.safe_dump(
            config_dict,
            default_flow_style=False,
            sort_keys=False,
        ),
        register_host=lambda: _register_hermes_mcp_server(detected=detected),
        create_defaults=create_default_config,
    ):
        rollback_hermes_skills()
        return False

    print_success(f"Configured Hermes runtime (CLI: {hermes_path})")
    print_info(f"Config saved to: {config_path}")
    return True


def _detect_mcp_entry_for_kiro() -> dict[str, object] | None:
    """Build Kiro's package-isolated MCP 2 launcher entry."""
    return _detect_mcp_entry(package_spec="ouroboros-ai[mcp]")


def _register_kiro_mcp_server(*, detected: dict[str, object] | None = None) -> bool:
    """Register the Ouroboros MCP hookup in ``~/.kiro/settings/mcp.json``.

    Mirrors ``_ensure_claude_mcp_entry`` (same detection and idempotency
    policy), with two Kiro-specific additions baked into the entry:

    * ``OUROBOROS_RUNTIME=kiro`` — so the MCP server routes agent work to
      the Kiro adapter by default when Kiro spawns it.
    * ``OUROBOROS_LLM_BACKEND=kiro`` — so interview / seed generation use
      the Kiro LLM adapter instead of falling back to Claude SDK.

    These env vars are required for the end-user "drop-in backend"
    experience: without them a user who runs ``kiro-cli chat`` sees the
    Ouroboros MCP server pick Claude as default and fail when Claude is
    not configured.

    Uses :func:`_detect_mcp_entry_for_kiro`, which only returns a package-
    isolated MCP 2 launcher. Kiro should increase its first-start timeout
    rather than silently binding a faster but incompatible global binary.
    """
    detected = detected or _detect_mcp_entry_for_kiro()
    if detected is None:
        print_error(
            "Cannot register Kiro MCP server without an isolated launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    mcp_config_path = Path.home() / ".kiro" / "settings" / "mcp.json"
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    mcp_data: dict = {}
    if mcp_config_path.exists():
        try:
            mcp_data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print_error(f"Could not parse {mcp_config_path} — skipping Kiro MCP registration.")
            return False
        if not isinstance(mcp_data, dict):
            print_warning(f"{mcp_config_path} top-level is not a mapping — resetting.")
            mcp_data = {}

    servers = mcp_data.get("mcpServers")
    if not isinstance(servers, dict):
        if servers is not None:
            print_warning(f"{mcp_config_path} 'mcpServers' section is not a mapping — resetting.")
        servers = {}
        mcp_data["mcpServers"] = servers

    # Known legacy direct commands are accepted only so setup can replace them
    # with the current isolated launcher on the next run.
    target_env = {
        "OUROBOROS_RUNTIME": "kiro",
        "OUROBOROS_LLM_BACKEND": "kiro",
    }

    existing = servers.get("ouroboros")
    if existing is not None and not isinstance(existing, dict):
        print_warning(f"{mcp_config_path} mcpServers.ouroboros is not a mapping — replacing it.")
        existing = None

    needs_write = False

    if existing is None:
        servers["ouroboros"] = {
            "command": detected["command"],
            "args": detected["args"],
            "disabled": False,
            "env": target_env,
        }
        needs_write = True
        print_success(f"Registered Ouroboros MCP server in {mcp_config_path}")
    else:
        # Update command/args for known standard commands only; leave custom
        # entries (docker, nix, etc.) alone so we don't break user setups.
        # Absolute paths whose basename is ``ouroboros`` are also considered
        # setup-managed — the binary-first detector (_detect_mcp_entry_for_kiro)
        # writes absolute paths from venvs, and we want re-runs of setup to
        # be able to upgrade those entries.
        _KNOWN_COMMANDS = {"uvx", "pipx", "ouroboros", "python3", "python", "uv"}
        existing_cmd = existing.get("command")
        is_setup_managed = existing_cmd in _KNOWN_COMMANDS or (
            isinstance(existing_cmd, str)
            and os.path.basename(existing_cmd) in {"ouroboros", "python3", "python"}
        )
        if is_setup_managed:
            if (
                existing.get("command") != detected["command"]
                or existing.get("args") != detected["args"]
            ):
                existing["command"] = detected["command"]
                existing["args"] = detected["args"]
                needs_write = True
                print_info("Updated Kiro MCP entry to match current install method.")

        current_env = existing.get("env")
        merged_env: dict[str, str] = dict(current_env) if isinstance(current_env, dict) else {}
        for key, value in target_env.items():
            if merged_env.get(key) != value:
                merged_env[key] = value
                needs_write = True
        if merged_env != current_env:
            existing["env"] = merged_env

        if existing.get("disabled") is True:
            existing["disabled"] = False
            needs_write = True

        if not needs_write:
            print_info("Kiro MCP entry already registered.")

    if needs_write:
        _atomic_write_text(mcp_config_path, json.dumps(mcp_data, indent=2) + "\n")
    return True


def _setup_kiro(kiro_path: str) -> bool:
    """Configure Ouroboros for the Kiro CLI runtime.

    Writes ``~/.ouroboros/config.yaml`` with ``orchestrator.runtime_backend =
    kiro`` / ``llm.backend = kiro`` and registers the MCP server in
    ``~/.kiro/settings/mcp.json`` so the user's very next
    ``kiro-cli chat`` session can invoke ``ooo <skill>`` prefixes without
    hand-editing any config file.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir
    from ouroboros.config.models import get_default_config

    detected = _detect_mcp_entry_for_kiro()
    if detected is None:
        print_error(
            "Kiro setup requires an isolated MCP 2 launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    config_was_missing = not config_path.exists()

    if not config_was_missing:
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        config_dict = get_default_config().model_dump(mode="json")

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Kiro setup.")
        return False

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "kiro"
    orch["kiro_cli_path"] = kiro_path

    llm = config_dict.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        config_dict["llm"] = llm
    llm["backend"] = "kiro"

    if not _commit_runtime_activation(
        runtime_name="Kiro",
        host_path=Path.home() / ".kiro" / "settings" / "mcp.json",
        config_path=config_path,
        config_was_missing=config_was_missing,
        runtime_content=yaml.safe_dump(
            config_dict,
            default_flow_style=False,
            sort_keys=False,
        ),
        register_host=lambda: _register_kiro_mcp_server(detected=detected),
        create_defaults=create_default_config,
    ):
        return False

    print_success(f"Configured Kiro runtime (CLI: {kiro_path})")
    print_info(f"Config saved to: {config_path}")

    _install_runtime_instruction_artifact("kiro")
    return True


def _register_copilot_mcp_server(*, detected: dict[str, object] | None = None) -> bool:
    """Register or refresh the Ouroboros MCP entry in ~/.copilot/mcp-config.json.

    Copilot CLI loads MCP servers from ``~/.copilot/mcp-config.json``. We add
    the Ouroboros server (built from whichever package install method we
    detect) and set the env so the MCP child uses the Copilot backend.
    """
    detected = detected or _detect_mcp_entry(package_spec="ouroboros-ai[mcp]")
    if detected is None:
        print_error(
            "Cannot register Copilot MCP server without an isolated launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    mcp_path = Path.home() / ".copilot" / "mcp-config.json"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            print_warning("~/.copilot/mcp-config.json is not valid JSON — leaving it untouched.")
            return False

    if not isinstance(data, dict):
        print_warning("~/.copilot/mcp-config.json top-level is not an object — skipping.")
        return False

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    target_env = {
        "OUROBOROS_AGENT_RUNTIME": "copilot",
        "OUROBOROS_LLM_BACKEND": "copilot",
    }

    existing = servers.get("ouroboros")
    if existing is not None and not isinstance(existing, dict):
        print_warning(
            "~/.copilot/mcp-config.json mcpServers.ouroboros is not an object — replacing it."
        )
        existing = None

    needs_write = False
    if existing is None:
        servers["ouroboros"] = {
            "command": detected["command"],
            "args": detected["args"],
            "env": target_env,
        }
        needs_write = True
        print_success(f"Registered MCP server in {mcp_path}")
    else:
        _KNOWN_COMMANDS = {"uvx", "pipx", "ouroboros", "python3", "python", "uv"}
        existing_cmd = existing.get("command")
        is_setup_managed = existing_cmd in _KNOWN_COMMANDS or (
            isinstance(existing_cmd, str)
            and os.path.basename(existing_cmd) in {"ouroboros", "python3", "python"}
        )
        if is_setup_managed and (
            existing.get("command") != detected["command"]
            or existing.get("args") != detected["args"]
        ):
            existing["command"] = detected["command"]
            existing["args"] = detected["args"]
            needs_write = True
            print_info("Updated Copilot MCP entry to match current install method.")

        current_env = existing.get("env")
        merged_env: dict[str, str] = dict(current_env) if isinstance(current_env, dict) else {}
        for key, value in target_env.items():
            if merged_env.get(key) != value:
                merged_env[key] = value
                needs_write = True
        if merged_env != current_env:
            existing["env"] = merged_env

        if not needs_write:
            print_info("MCP server already registered for Copilot CLI.")

    if needs_write:
        _atomic_write_text(mcp_path, json.dumps(data, indent=2) + "\n")
    return True


_COPILOT_DEFAULT_MODEL_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("llm", "qa_model", DEFAULT_SONNET_MODEL),
    ("llm", "dependency_analysis_model", DEFAULT_SONNET_MODEL),
    ("llm", "ontology_analysis_model", DEFAULT_SONNET_MODEL),
    ("llm", "context_compression_model", "gpt-4"),
    ("clarification", "default_model", DEFAULT_OPUS_MODEL),
    ("evaluation", "semantic_model", DEFAULT_OPUS_MODEL),
    ("evaluation", "assertion_extraction_model", DEFAULT_SONNET_MODEL),
    ("resilience", "wonder_model", DEFAULT_OPUS_MODEL),
    ("resilience", "reflect_model", DEFAULT_OPUS_MODEL),
    ("consensus", "advocate_model", DEFAULT_CONSENSUS_OPUS_MODEL),
    ("consensus", "devil_model", "openrouter/openai/gpt-4o"),
    ("consensus", "judge_model", "openrouter/google/gemini-2.5-pro"),
)

_COPILOT_DEFAULT_MODEL_LIST_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "consensus",
        "models",
        (
            "openrouter/openai/gpt-4o",
            DEFAULT_CONSENSUS_OPUS_MODEL,
            "openrouter/google/gemini-2.5-pro",
        ),
    ),
)


def _apply_copilot_default_model(
    config_dict: dict,
    chosen_model: str,
    model_roster: tuple[str, ...],
) -> None:
    """Persist the setup-selected Copilot model into supported model fields.

    There is no generic ``llm.default_model`` contract in ``LLMConfig``.
    Treat setup's selected model as the default for model fields that are
    absent or still equal to Ouroboros' shipped defaults, while preserving
    explicit user overrides.

    "Shipped default" means the *current* pin OR any prior-release shipped
    default (``recognized_shipped_defaults``): a config persisted before a pin
    bump holds the old literal but is still an untouched shipped default the
    user never chose, so Copilot setup must rewrite it to the discovered model
    rather than mistaking it for an explicit override (Q00/ouroboros#1324).
    """
    for section_name, key, shipped_default in _COPILOT_DEFAULT_MODEL_TARGETS:
        section = _ensure_mapping_section(config_dict, section_name)
        current = section.get(key)
        if current is None or current in recognized_shipped_defaults(shipped_default):
            section[key] = chosen_model

    for section_name, key, shipped_default in _COPILOT_DEFAULT_MODEL_LIST_TARGETS:
        section = _ensure_mapping_section(config_dict, section_name)
        current = section.get(key)
        if current is None or (
            isinstance(current, (list, tuple))
            and _is_shipped_default_roster(current, shipped_default)
        ):
            section[key] = list(model_roster)


def _setup_copilot(copilot_path: str, *, non_interactive: bool = False) -> bool:
    """Configure Ouroboros for the GitHub Copilot CLI runtime.

    Writes ``~/.ouroboros/config.yaml`` with ``orchestrator.runtime_backend =
    copilot`` / ``llm.backend = copilot`` and persists the user's chosen
    default model after live-discovering the Copilot model catalog. Also
    registers the MCP server in ``~/.copilot/mcp-config.json``.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir
    from ouroboros.config.models import get_default_config
    from ouroboros.copilot.model_discovery import (
        list_copilot_models,
        used_fallback,
    )

    detected = _detect_mcp_entry(package_spec="ouroboros-ai[mcp]")
    if detected is None:
        print_error(
            "Copilot setup requires an isolated MCP 2 launcher. "
            "Install uv/uvx or pipx, then re-run setup."
        )
        return False

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    config_was_missing = not config_path.exists()

    if not config_was_missing:
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        config_dict = get_default_config().model_dump(mode="json")

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Copilot setup.")
        return False

    # Live-discover available Copilot models. When the GitHub API is
    # unreachable or unauthenticated, use a bundled snapshot and warn that it
    # may be stale.
    models = list_copilot_models(refresh=True)
    if used_fallback():
        print_warning(
            "Could not reach the GitHub Copilot models API — using a bundled "
            "fallback list. Run `gh auth login` and re-run setup to refresh."
        )
    if not models:
        print_error("No Copilot models available; cannot pick a default.")
        return False

    preferred_default = next(
        (m.id for m in models if m.id.startswith("claude-opus-4.6")),
        models[0].id,
    )

    if non_interactive:
        chosen_model = preferred_default
        print_info(f"Non-interactive mode, default model: {chosen_model}")
    else:
        console.print("\n[bold]Available Copilot models:[/bold]")
        for idx, model in enumerate(models, 1):
            tag = " [yellow](recommended)[/yellow]" if model.id == preferred_default else ""
            label = f" — {model.name}" if model.name else ""
            console.print(f"  [{idx}] {model.id}{label}{tag}")
        console.print()

        try:
            default_idx = str(next(i for i, m in enumerate(models, 1) if m.id == preferred_default))
        except StopIteration:
            default_idx = "1"

        choice = typer.prompt("Select default model", default=default_idx)
        try:
            idx = int(choice) - 1
            chosen_model = models[idx].id
        except (ValueError, IndexError):
            chosen_model = choice.strip() or preferred_default

    try:
        orch = _ensure_mapping_section(config_dict, "orchestrator")
        orch["runtime_backend"] = "copilot"
        orch["copilot_cli_path"] = copilot_path

        llm = _ensure_mapping_section(config_dict, "llm")
        llm["backend"] = "copilot"
        llm.pop("default_model", None)
        model_roster = tuple(model.id for model in models[:3])
        if len(model_roster) < 3:
            model_roster = (*model_roster, *((chosen_model,) * (3 - len(model_roster))))
        _apply_copilot_default_model(config_dict, chosen_model, model_roster)
    except ValueError as exc:
        print_error(f"Invalid config.yaml structure: {exc}")
        print_info("Aborting Copilot setup without rewriting config.yaml.")
        return False

    if not _commit_runtime_activation(
        runtime_name="Copilot",
        host_path=Path.home() / ".copilot" / "mcp-config.json",
        config_path=config_path,
        config_was_missing=config_was_missing,
        runtime_content=yaml.safe_dump(
            config_dict,
            default_flow_style=False,
            sort_keys=False,
        ),
        register_host=lambda: _register_copilot_mcp_server(detected=detected),
        create_defaults=create_default_config,
    ):
        return False

    print_success(f"Configured Copilot runtime (CLI: {copilot_path})")
    print_info(f"Default model: {chosen_model}")
    print_info(f"Config saved to: {config_path}")

    _install_runtime_instruction_artifact("copilot")
    return True


_PI_OOO_BRIDGE_FILENAME = "ouroboros-ooo-bridge.ts"
_GJC_OOO_BRIDGE_SUBDIR = "ouroboros-ooo-bridge"
_GJC_OOO_BRIDGE_FILENAME = "index.ts"


def _detect_pi_bridge_dispatch_entry() -> tuple[str, list[str]]:
    """Return the launcher a managed Pi bridge should use for this install."""
    if _is_source_tree_ouroboros_build():
        return sys.executable, ["-m", "ouroboros"]
    try:
        version = importlib_metadata.version("ouroboros-ai")
    except importlib_metadata.PackageNotFoundError:
        return sys.executable, ["-m", "ouroboros"]
    if ".dev" in version:
        return sys.executable, ["-m", "ouroboros"]
    return shutil.which("ouroboros") or "ouroboros", []


def _pi_ooo_bridge_source_text() -> str:
    """Return the managed Pi extension source for ``ooo`` frontdoor dispatch."""
    command, args = _detect_pi_bridge_dispatch_entry()
    return pi_ooo_bridge_source_text(command=command, args=args)


def _install_pi_ooo_bridge() -> bool:
    """Install the managed Pi extension that routes interactive ``ooo`` input.

    Pi auto-discovers global extensions in ``~/.pi/agent/extensions/*.ts``.
    Writing a single managed file there avoids modifying Pi itself or any
    third-party package such as roach-pi, while making the bridge available to
    every Pi-based/customized session after ``/reload`` or restart.
    """
    import hashlib

    dest = Path.home() / ".pi" / "agent" / "extensions" / _PI_OOO_BRIDGE_FILENAME
    content = _pi_ooo_bridge_source_text()
    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    existing_hash: str | None = None
    if dest.exists():
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None

    if existing_hash == new_hash:
        print_info(f"Pi ooo bridge already up to date: {dest}")
        return True

    try:
        _atomic_write_text(dest, content)
    except OSError as exc:
        print_warning(f"Could not install Pi ooo bridge at {dest}: {exc}")
        return False

    print_success(
        f"{'Updated' if existing_hash is not None else 'Installed'} Pi ooo bridge: {dest}"
    )
    print_info("Restart Pi or run /reload in an existing Pi session to load the bridge.")
    return True


def _detect_gjc_bridge_dispatch_entry() -> tuple[str, list[str]]:
    if _is_source_tree_ouroboros_build():
        return sys.executable, ["-m", "ouroboros"]
    try:
        version = importlib_metadata.version("ouroboros-ai")
    except importlib_metadata.PackageNotFoundError:
        return sys.executable, ["-m", "ouroboros"]
    if ".dev" in version:
        return sys.executable, ["-m", "ouroboros"]
    return shutil.which("ouroboros") or "ouroboros", []


def _gjc_bridge_source_text() -> str | None:
    from importlib import resources

    try:
        source = (
            resources.files("ouroboros.gjc_bridge")
            .joinpath(_GJC_OOO_BRIDGE_FILENAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        dev = Path(__file__).resolve().parents[2] / "gjc_bridge" / _GJC_OOO_BRIDGE_FILENAME
        try:
            source = dev.read_text(encoding="utf-8") if dev.exists() else None
        except OSError:
            return None
    if source is None:
        return None
    command, args = _detect_gjc_bridge_dispatch_entry()
    return source.replace(
        'const DEFAULT_COMMAND = "ouroboros";',
        f"const DEFAULT_COMMAND = {json.dumps(command)};",
    ).replace(
        "const DEFAULT_ARGS: string[] = [];",
        f"const DEFAULT_ARGS: string[] = {json.dumps(args)};",
    )


def _install_gjc_ooo_bridge() -> bool:
    from ouroboros.runtime_instruction_artifacts import gjc_agent_dir

    dest = gjc_agent_dir() / "extensions" / _GJC_OOO_BRIDGE_SUBDIR / _GJC_OOO_BRIDGE_FILENAME
    content = _gjc_bridge_source_text()
    if content is None:
        print_warning("Could not locate packaged GJC ooo bridge source.")
        return False

    expected = content.encode("utf-8")
    try:
        dest_snapshot = _snapshot_path(dest, follow_links=False)
    except OSError as exc:
        print_warning(f"Could not inspect existing GJC ooo bridge at {dest}: {exc}")
        return False
    if dest_snapshot.kind == "file" and dest_snapshot.contents == expected:
        print_info(f"GJC ooo bridge already up to date: {dest}")
        return True
    if dest_snapshot.kind != "missing":
        print_warning(f"Refusing to overwrite existing GJC ooo bridge: {dest}")
        return False

    try:
        _atomic_write_text(dest, content, expected_current=dest_snapshot)
    except OSError as exc:
        print_warning(f"Could not install GJC ooo bridge at {dest}: {exc}")
        return False

    print_success(f"Installed GJC ooo bridge: {dest}")
    print_info("Restart GJC or run /reload in an existing GJC session to load the bridge.")
    return True


def _setup_gjc(gjc_path: str) -> bool:
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    config_snapshot = _snapshot_path(config_path, follow_links=False)
    config_dict: object = {}
    if config_snapshot.kind == "file":
        config_dict = yaml.safe_load((config_snapshot.contents or b"").decode("utf-8")) or {}
    elif config_snapshot.kind != "missing":
        print_error("~/.ouroboros/config.yaml is not a regular file — aborting GJC setup.")
        return False
    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting GJC setup.")
        return False
    if not _install_gjc_ooo_bridge():
        return False
    try:
        if config_snapshot.kind == "missing":
            create_default_config(config_dir)
            config_snapshot = _snapshot_path(config_path, follow_links=False)
            config_dict = yaml.safe_load((config_snapshot.contents or b"").decode("utf-8")) or {}
            if not isinstance(config_dict, dict):
                raise ConfigError("Generated config.yaml top-level is not a mapping")
        orch = config_dict.get("orchestrator")
        if not isinstance(orch, dict):
            orch = {}
            config_dict["orchestrator"] = orch
        orch["runtime_backend"] = "gjc"
        orch["gjc_cli_path"] = gjc_path
        llm = config_dict.get("llm")
        if not isinstance(llm, dict):
            llm = {}
            config_dict["llm"] = llm
        llm["backend"] = "gjc"
        _atomic_write_text(
            config_path,
            yaml.dump(config_dict, default_flow_style=False, sort_keys=False),
            expected_current=config_snapshot,
        )
    except (OSError, ConfigError) as exc:
        print_error(f"Could not save GJC configuration to {config_path}: {exc}")
        return False
    print_success(f"Configured GJC runtime (CLI: {gjc_path})")
    print_info(f"Config saved to: {config_path}")
    return True


def _setup_gemini(gemini_path: str) -> None:
    """Configure Ouroboros for the Gemini CLI runtime.

    Gemini is a base-package runtime — it does not require the ``[claude]``
    extra (or any provider-specific extra) to function, so the MCP entry is
    not rewritten as part of this flow. Users who want the Gemini runtime to
    drive the MCP server can keep their existing entry; switching the
    ``orchestrator.runtime_backend`` is sufficient.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Gemini setup.")
        return

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "gemini"
    orch["gemini_cli_path"] = gemini_path

    # Gemini also serves as an LLM-only backend for interview/seed/eval flows.
    llm = config_dict.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        config_dict["llm"] = llm
    llm["backend"] = "gemini"

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Gemini runtime (CLI: {gemini_path})")
    print_info(f"Config saved to: {config_path}")
    _install_runtime_instruction_artifact("gemini")


def _setup_runtime_only_backend(
    backend: str, cli_path: str | None, cli_config_key: str | None
) -> None:
    """Configure runtime setup for Antigravity, Grok, Zcode, or host.

    This setup path intentionally sets only ``orchestrator.runtime_backend`` and
    (when applicable) the CLI path. Antigravity and Grok are runtime-only; Zcode
    also supports explicit LLM-completion selection, but setup does not silently
    move ``llm.backend`` because that changes authoring/evaluation traffic.
    ``host`` has no CLI at all — pass ``cli_path=None, cli_config_key=None`` for
    it. No setup-owned instruction artifact is installed yet — a documented gap
    in ``docs/runtime-guides/skill-capability-guides.md``.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error(
            f"~/.ouroboros/config.yaml top-level is not a mapping — aborting {backend} setup."
        )
        return

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = backend
    if cli_path is not None and cli_config_key is not None:
        orch[cli_config_key] = cli_path

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    if cli_path is not None:
        print_success(f"Configured {backend} runtime (CLI: {cli_path})")
    else:
        print_success(f"Configured {backend} runtime (no CLI — host-driven dispatch)")
    print_info(f"Config saved to: {config_path}")


def _setup_host() -> bool:
    """Configure the CLI-less host runtime and its setup-owned MCP launchers."""
    import sys

    from ouroboros.cli.commands.setup_host import setup_host

    return setup_host(sys.modules[__name__])


def _setup_antigravity(antigravity_path: str) -> None:
    """Configure Ouroboros for the Antigravity CLI (``agy``) runtime."""
    _setup_runtime_only_backend("antigravity", antigravity_path, "antigravity_cli_path")


def _setup_grok(grok_path: str) -> None:
    """Configure Ouroboros for the Grok Build CLI (``grok``) runtime."""
    _setup_runtime_only_backend("grok", grok_path, "grok_cli_path")


def _setup_zcode(zcode_path: str) -> None:
    """Configure Ouroboros for the Zcode CLI runtime."""
    _setup_runtime_only_backend("zcode", zcode_path, "zcode_cli_path")
    _warn_zcode_model_config()


def _warn_zcode_model_config() -> None:
    """Warn when the Zcode CLI itself has no usable model provider config.

    Ouroboros setup only records the CLI path; the Zcode CLI separately
    resolves its model from ``~/.zcode/cli/config.json`` on every headless
    run and exits with ``Model config is missing`` when that file has no
    effective ``model.main`` reference. Without this probe the failure only
    appears mid-execution, after Ouroboros has already spawned the CLI.
    """

    from ouroboros.config.zcode_model_config import inspect_zcode_model_config

    status = inspect_zcode_model_config()
    if status.ok:
        return

    print_warning(f"Zcode model provider config not ready: {status.detail}")
    print_info(
        "Headless Zcode runs will exit with 'Model config is missing' until "
        f"{status.config_path} defines model.main."
    )
    print_info(
        'Example: {"model": {"main": "builtin:zai-coding-plan/GLM-5.3"}, '
        '"provider": {"builtin:zai-coding-plan": {"kind": "anthropic", '
        '"options": {"baseURL": "https://api.z.ai/api/anthropic", "apiKey": "..."}}}} '
        "(model.main must be the provider/model string). "
        "See docs/runtime-guides/zcode.md"
    )


def _setup_pi(pi_path: str) -> None:
    """Configure Ouroboros for the Pi CLI runtime.

    Pi is a base-package agent runtime: setup records the executable path and
    runtime backend, and installs a managed Pi extension so interactive Pi
    sessions can route ``ooo ...`` inputs into Ouroboros' shared skill
    dispatcher. The Pi LLM adapter remains opt-in so setup does not
    unexpectedly move authoring/evaluation traffic to a different provider;
    when selected explicitly, structured response_format requests are enforced
    cooperatively by that adapter.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Pi setup.")
        return

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "pi"
    orch["pi_cli_path"] = pi_path

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Pi runtime (CLI: {pi_path})")
    print_info(f"Config saved to: {config_path}")
    _install_pi_ooo_bridge()


def _setup_goose(goose_path: str) -> None:
    """Configure Ouroboros for the Goose runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_warning("~/.ouroboros/config.yaml top-level is not a mapping — resetting.")
        config_dict = {}

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "goose"
    orch["goose_cli_path"] = goose_path

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Goose runtime (CLI: {goose_path})")
    print_info(f"Config saved to: {config_path}")


def _strip_jsonc(text: str) -> str:
    """Strip JSONC features (comments, trailing commas) to produce valid JSON.

    .. deprecated::
        Forwards to :func:`ouroboros.cli.jsonc.strip_jsonc` which handles
        quoted strings correctly.
    """
    from ouroboros.cli.jsonc import strip_jsonc

    return strip_jsonc(text)


def _find_opencode_config() -> Path:
    """Locate the existing OpenCode config file, or return a default path.

    Delegates to :func:`ouroboros.cli.opencode_config.find_opencode_config`
    with ``allow_default=True`` so that new installations get a sensible
    default path (``opencode.json``) to write to.
    """
    result = find_opencode_config(allow_default=True)
    assert result is not None  # allow_default=True always returns a Path
    return result


def _ensure_opencode_mcp_entry() -> bool:
    """Ensure the platform-appropriate OpenCode config has a correct ouroboros MCP entry.

    OpenCode reads config from the platform config dir (see :func:`opencode_config_dir`)
    — either ``opencode.jsonc`` or ``opencode.json`` (both support JSONC).
    The ``mcp`` key is a record of named MCP server configs.

    MCP entry format (local):
        ``{ "type": "local", "command": [...], "environment": {...}, "timeout": 300000 }``

    Returns:
        True if the MCP entry is registered (or already present),
        False if registration failed.
    """
    config_path = _find_opencode_config()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(_strip_jsonc(config_path.read_text()))
        except (json.JSONDecodeError, OSError):
            print_warning(
                f"Could not parse {config_path} — skipping MCP registration to avoid "
                "overwriting existing settings.  Fix the JSON syntax and re-run setup."
            )
            return False

    if not isinstance(data, dict):
        print_warning(f"{config_path} top-level is not an object — resetting to {{}}.")
        data = {}

    mcp = data.get("mcp")
    if mcp is None:
        mcp = {}
        data["mcp"] = mcp
    elif not isinstance(mcp, dict):
        print_warning(f"{config_path} 'mcp' key is not an object — replacing with {{}}.")
        mcp = {}
        data["mcp"] = mcp

    # Detect the best command to run ouroboros mcp serve
    detected = _detect_opencode_mcp_command()
    if detected is None:
        print_warning(
            "Cannot register MCP server: no working ouroboros installation found.\n"
            "Install uv/uvx or pipx so setup can launch isolated ouroboros-ai[mcp]."
        )
        return False

    entry = {
        "type": "local",
        "command": detected["command"],
        "environment": {
            "OUROBOROS_AGENT_RUNTIME": "opencode",
            "OUROBOROS_LLM_BACKEND": "opencode",
        },
        "timeout": 300000,
    }

    existing = mcp.get("ouroboros")
    if not isinstance(existing, dict):
        mcp["ouroboros"] = entry
        print_success(f"Registered MCP server in {config_path}")
    else:
        # Update command only for known standard launchers. Custom entries
        # (docker, nix wrappers, etc.) are left untouched so we don't break
        # user-managed configurations — mirrors the Claude setup path.
        _KNOWN_COMMANDS = {"ouroboros", "python3", "python", "uvx", "pipx", "uv"}
        existing_cmd = existing.get("command")
        # OpenCode expects command: string[]. If it's a bare string (hand-edited
        # or legacy), replace it unconditionally since it can't launch.
        if isinstance(existing_cmd, str):
            existing["command"] = entry["command"]
            print_info("Replaced invalid command string with proper array format.")
        else:
            # First element is the binary
            existing_binary = (
                existing_cmd[0] if isinstance(existing_cmd, list) and existing_cmd else None
            )
            # Repair malformed arrays: empty list, non-string first element
            if not isinstance(existing_binary, str):
                existing["command"] = entry["command"]
                print_info("Replaced malformed command array with proper launcher.")
            elif existing_binary in _KNOWN_COMMANDS:
                if existing_cmd != entry["command"]:
                    existing["command"] = entry["command"]
                    print_info("Updated MCP server command to match current install.")
        # Normalise stale transport type (e.g. "remote" → "local")
        if existing.get("type") != "local":
            existing["type"] = "local"
        # Ensure runtime env vars are set — repair non-dict environment
        env = existing.get("environment")
        if not isinstance(env, dict):
            env = {}
            existing["environment"] = env
        env["OUROBOROS_AGENT_RUNTIME"] = "opencode"
        env["OUROBOROS_LLM_BACKEND"] = "opencode"
        if "timeout" not in existing:
            existing["timeout"] = 300000
        print_info("MCP server already registered — verified config.")

    # Warn if we're about to overwrite a .jsonc file that contained comments.
    if config_path.suffix == ".jsonc":
        try:
            original_text = config_path.read_text(encoding="utf-8")
        except OSError:
            original_text = ""
        if "//" in original_text or "/*" in original_text:
            print_warning(
                f"Note: JSONC comments in {config_path} were removed during config update."
            )

    # Write back as plain JSON.  This intentionally discards JSONC
    # comments — the same approach Claude and Codex setup use for their
    # respective config files.  A comment-preserving JSONC writer is out
    # of scope for this module.
    try:
        with config_path.open("w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except OSError:
        print_warning(f"Could not write {config_path} — skipping.")
        return False
    return True


def _detect_opencode_mcp_command() -> dict[str, list[str]] | None:
    """Detect the best command to run ouroboros MCP server for OpenCode.

    OpenCode MCP uses ``command: string[]`` format (array, not separate command+args).

    Detection order mirrors the Claude setup path: prefer ``uvx`` (pinned
    extras) over a bare ``ouroboros`` binary so that machines with both a
    stale global binary and a newer uvx install use the newer one.
    """
    if shutil.which("uvx"):
        return {"command": ["uvx", *_build_uvx_mcp_args("ouroboros-ai[mcp]")]}
    if shutil.which("pipx"):
        return {
            "command": [
                "pipx",
                "run",
                "--spec",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
            ]
        }
    return None


# Canonical relative path components for the bridge plugin install — single
# source of truth so install + config-registry + uninstall all agree.
# Re-exported from opencode_config as module-private aliases so the rest of
# this file keeps its historic `_BRIDGE_PLUGIN_*` naming.


@contextmanager
def _temporary_opencode_cli_path(opencode_path: str):
    """Expose the setup-selected OpenCode CLI path to config-dir discovery."""
    key = "OUROBOROS_OPENCODE_CLI_PATH"
    previous = os.environ.get(key)
    os.environ[key] = opencode_path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _bridge_plugin_source_text() -> str | None:
    """Return the bridge plugin TypeScript source, or ``None`` when missing.

    Tries the packaged wheel resource first (production installs), then falls
    back to the in-repo development tree.  Any IO or import failure → ``None``
    so the caller can warn instead of crashing setup.
    """
    import importlib.resources

    try:
        pkg = importlib.resources.files("ouroboros.opencode.plugin")
        return pkg.joinpath(_BRIDGE_PLUGIN_FILENAME).read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError, ModuleNotFoundError, OSError):
        pass
    dev = Path(__file__).resolve().parents[2] / "opencode" / "plugin" / _BRIDGE_PLUGIN_FILENAME
    try:
        return dev.read_text(encoding="utf-8") if dev.exists() else None
    except OSError:
        return None


def _atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int | None = None,
    expected_current: _PathSnapshot | None = None,
) -> _PathSnapshot:
    """Write *content* atomically through a temp file and ``os.replace``.

    Readers see either the previous file or the final content, never partial
    bytes. Raises :class:`OSError` on failure for callers to surface.
    """
    import os
    import tempfile

    write_path = path.resolve(strict=False) if path.is_symlink() else path
    write_path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(write_path.lstat().st_mode)
        except FileNotFoundError:
            mode = 0o644
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{write_path.name}.",
        suffix=".tmp",
        dir=str(write_path.parent),
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass  # e.g. Windows FAT — not fatal
        actual_mode = stat.S_IMODE(Path(tmp_name).lstat().st_mode)
        if expected_current is not None:
            _require_path_snapshot(path, expected_current)
        os.replace(tmp_name, write_path)
        return _PathSnapshot(kind="file", mode=actual_mode, contents=content.encode("utf-8"))
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _install_opencode_bridge_plugin() -> bool:
    """Install the ouroboros-bridge plugin into OpenCode's plugin directory.

    Writes to the platform-appropriate OpenCode plugins directory:

    * Linux:   ``~/.config/opencode/plugins/ouroboros-bridge/``
    * macOS:   ``~/.config/opencode/plugins/ouroboros-bridge/`` (or the directory reported by ``opencode debug paths``)
    * Windows: ``%APPDATA%\\OpenCode\\plugins\\ouroboros-bridge\\``

    Robustness:

    * Content hashed (SHA-256) before write → identical source skips disk IO,
      avoids bumping mtime (which would re-trigger opencode's plugin watcher).
    * Atomic write (temp file + ``os.replace``) → crash mid-write never
      leaves a corrupted ``.ts`` file that would fail the plugin loader.
    * Missing source (wheel built without package-data, truncated checkout)
      returns False — caller must abort setup.

    Returns:
        True if the bridge plugin is installed (or already up to date),
        False if installation failed.
    """
    import hashlib

    plugin_dir = opencode_config_dir()
    for part in _BRIDGE_PLUGIN_SUBDIR:
        plugin_dir = plugin_dir / part
    dest = plugin_dir / _BRIDGE_PLUGIN_FILENAME

    content = _bridge_plugin_source_text()
    if content is None:
        print_warning(
            f"Bridge plugin source not found — manually copy {_BRIDGE_PLUGIN_FILENAME} "
            f"into {plugin_dir}/"
        )
        return False

    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing_hash: str | None = None
    if dest.exists():
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None

    if existing_hash == new_hash:
        print_info(f"Bridge plugin already up to date: {dest}")
        return True

    try:
        _atomic_write_text(dest, content)
    except OSError as exc:
        print_warning(f"Could not install bridge plugin at {dest}: {exc}")
        return False

    print_success(
        f"{'Updated' if existing_hash is not None else 'Installed'} bridge plugin: {dest}"
    )
    return True


def _ensure_opencode_plugin_entry() -> bool:
    """Ensure the bridge plugin is registered in OpenCode's ``plugin`` array.

    Reads ``opencode.jsonc``/``opencode.json``, deduplicates any stale bridge
    entries (matching by directory tail, not exact string — handles path
    changes across XDG shifts and OS migrations), appends the canonical
    current path, and writes the config back atomically.  No-ops when the
    canonical entry is already present and no stale siblings exist.

    Returns:
        True if the entry is registered (or already present),
        False if registration failed.
    """
    canonical = opencode_config_dir()
    for part in _BRIDGE_PLUGIN_SUBDIR:
        canonical = canonical / part
    canonical_path = str(canonical / _BRIDGE_PLUGIN_FILENAME)

    config_path = _find_opencode_config()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(_strip_jsonc(config_path.read_text()))
        except (json.JSONDecodeError, OSError):
            print_warning(f"Could not parse {config_path} — skipping plugin registration.")
            return False

    if not isinstance(data, dict):
        data = {}

    raw_plugins = data.get("plugin")
    existing = raw_plugins if isinstance(raw_plugins, list) else []

    # Drop every stale bridge entry (including the canonical one — we re-add
    # it at the end so the list stays deduplicated and the bridge is always
    # loaded last, matching install order expectations).
    stale = [e for e in existing if _is_bridge_plugin_entry(e)]
    kept = [e for e in existing if not _is_bridge_plugin_entry(e)]
    cleaned = [*kept, canonical_path]

    already_ok = (
        isinstance(raw_plugins, list)
        and len(stale) == 1
        and stale[0] == canonical_path
        and existing == cleaned
    )
    if already_ok:
        print_info("Bridge plugin already registered in opencode config.")
        return True

    data["plugin"] = cleaned

    # Warn if we're about to overwrite a .jsonc file that contained comments.
    if config_path.suffix == ".jsonc":
        try:
            original_text = config_path.read_text(encoding="utf-8")
        except OSError:
            original_text = ""
        if "//" in original_text or "/*" in original_text:
            print_warning(
                f"Note: JSONC comments in {config_path} were removed during config update."
            )

    try:
        _atomic_write_text(config_path, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        print_warning(f"Could not write {config_path}: {exc}")
        return False

    if len(stale) > 1:
        print_info(f"Removed {len(stale) - 1} stale bridge entries from {config_path}.")
    if stale and stale[0] != canonical_path:
        print_info(f"Repointed bridge entry to {canonical_path} in {config_path}.")
    if not stale:
        print_success(f"Registered bridge plugin in {config_path}")
    else:
        print_success(f"Bridge plugin entry verified in {config_path}")
    return True


def _cleanup_plugin_artifacts() -> None:
    """Remove bridge-plugin files and config entries (subprocess mode cleanup).

    Called when switching to subprocess mode so both paths are not active
    simultaneously.  Best-effort — failures are warned but do not abort setup.
    """
    plugin_dir = opencode_config_dir() / "plugins" / "ouroboros-bridge"
    if plugin_dir.exists():
        try:
            shutil.rmtree(plugin_dir)
            print_info(f"Removed stale bridge plugin ({plugin_dir}/)")
        except OSError:
            print_warning(f"Could not remove {plugin_dir}/ — clean manually.")

    config_path = find_opencode_config(allow_default=False)
    if config_path is not None:
        try:
            raw = config_path.read_text()
            data = json.loads(_strip_jsonc(raw))
            plugins = data.get("plugin", [])
            if isinstance(plugins, list):
                kept = [e for e in plugins if not _is_bridge_plugin_entry(e)]
                if len(kept) != len(plugins):
                    data["plugin"] = kept
                    with config_path.open("w") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    print_info(f"Removed bridge plugin entry from {config_path}")
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # best effort


def _setup_opencode(opencode_path: str, mode: str = "plugin") -> bool:
    """Configure Ouroboros for the OpenCode runtime.

    mode (mutually exclusive — pick one, run setup twice if you deliberately want both):
        ``plugin``     install bridge plugin + register plugin/MCP in opencode.jsonc
                       (interactive OpenCode sessions; recommended default)
        ``subprocess`` write ~/.ouroboros/config.yaml runtime_backend=opencode only
                       (headless / CI / scripted ``ouroboros run``)

    Wiring both at once wastes tokens: an Ouroboros MCP tool called inside a
    subprocess-driven ``opencode run`` would also trigger the globally
    registered plugin, causing duplicate subagent dispatch. Choose one.

    Returns:
        True when setup completed; False when plugin-mode setup failed before
        config was persisted.
    """
    if mode not in ("plugin", "subprocess"):
        raise ValueError(f"Invalid opencode mode: {mode!r} (expected 'plugin' or 'subprocess')")

    from ouroboros.config.loader import create_default_config, ensure_config_dir

    # Persist mode to config.yaml for both branches so the MCP runtime gate
    # can read it later. Plugin branch still writes (no runtime_backend/cli
    # fields — plugin runs in-process inside OpenCode; but mode signal matters).
    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config_dict, dict):
        print_warning("~/.ouroboros/config.yaml top-level is not a mapping — resetting.")
        config_dict = {}
    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["opencode_mode"] = mode

    if mode == "subprocess":
        orch["runtime_backend"] = "opencode"
        orch["opencode_cli_path"] = opencode_path

        llm = config_dict.get("llm")
        if not isinstance(llm, dict):
            llm = {}
            config_dict["llm"] = llm
        llm["backend"] = "opencode"

        with config_path.open("w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

        # Mutual-exclusion cleanup: remove plugin-mode artifacts so both
        # paths are not active simultaneously (duplicate dispatch).
        with _temporary_opencode_cli_path(opencode_path):
            _cleanup_plugin_artifacts()
            _install_runtime_instruction_artifact("opencode", config_dir=opencode_config_dir())

        print_success(f"Configured OpenCode subprocess runtime (CLI: {opencode_path})")
        print_info(f"Config saved to: {config_path}")
        return True

    # mode == "plugin" — install plugin/MCP entries FIRST, only persist config
    # if ALL steps succeed (fail-closed).  Without this, a failed bridge install
    # leaves the user in plugin mode without a working bridge — subsequent runs
    # take the plugin dispatch path and silently break.
    with _temporary_opencode_cli_path(opencode_path):
        _install_ok = _install_opencode_bridge_plugin()
        _mcp_ok = _ensure_opencode_mcp_entry()
        _plugin_ok = _ensure_opencode_plugin_entry()

    if not (_install_ok and _mcp_ok and _plugin_ok):
        failed = []
        if not _install_ok:
            failed.append("bridge plugin installation")
        if not _mcp_ok:
            failed.append("MCP server registration")
        if not _plugin_ok:
            failed.append("plugin entry registration")
        print_error(
            f"Plugin-mode setup incomplete — failed: {', '.join(failed)}. "
            "Re-run 'ouroboros setup --runtime opencode --opencode-mode plugin' "
            "after fixing the issues above."
        )
        return False

    with _temporary_opencode_cli_path(opencode_path):
        _install_runtime_instruction_artifact("opencode", config_dir=opencode_config_dir())

    # All installs succeeded — now safe to persist config.
    # Plugin mode still needs runtime_backend=opencode so the MCP server's
    # should_dispatch_via_plugin() gate recognises the OpenCode context.
    # Without this, fresh installs default to runtime_backend=claude and the
    # gate always returns False — plugin dispatch never activates.
    # opencode_cli_path is also set so `ouroboros run` (subprocess fallback)
    # can still locate the CLI binary if needed.
    orch["runtime_backend"] = "opencode"
    orch["opencode_cli_path"] = opencode_path

    # Set llm.backend=opencode so subprocess fallback paths don't fall
    # back to claude_code. Without this, get_llm_backend() returns
    # "claude_code" and those paths try to invoke Claude, which fails on
    # OpenCode-only machines.
    llm = config_dict.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        config_dict["llm"] = llm
    llm["backend"] = "opencode"

    with config_path.open("w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success("Installed OpenCode bridge plugin and registered MCP entry")
    return True


# ── Brownfield repo helpers ──────────────────────────────────────


def _display_repos_table(
    repos: list[dict],
    *,
    show_default: bool = True,
) -> None:
    """Display a Rich table of brownfield repos.

    Args:
        repos: List of BrownfieldRepo-like dicts/objects with
               path, name, desc, is_default attributes.
        show_default: Whether to show the default marker column.
    """
    table = Table(show_header=True, header_style="bold cyan", expand=False)
    table.add_column("#", style="dim", width=4)
    if show_default:
        table.add_column("★", width=3)
    table.add_column("Name", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Description", style="dim italic")

    for idx, repo in enumerate(repos, 1):
        is_def = repo.get("is_default", False)
        default_marker = "[bold yellow]★[/]" if is_def else ""
        name = repo.get("name", "unnamed")
        path = repo.get("path", "")
        desc = repo.get("desc", "") or ""

        row = [str(idx)]
        if show_default:
            row.append(default_marker)
        row.extend([name, path, desc])
        table.add_row(*row)

    console.print(table)


def _prompt_repo_selection(
    repos: list[dict],
    prompt_text: str = "Toggle default repo",
) -> int | None:
    """Prompt the user to select a repo to toggle as default.

    Args:
        repos: List of repo dicts.
        prompt_text: Prompt text to display.

    Returns:
        0-based index of the selected repo, or None if cancelled.
    """
    raw = Prompt.ask(
        f"[yellow]{prompt_text}[/] (1-{len(repos)}, or 'skip' to skip)",
        default="skip",
    )

    stripped = raw.strip().lower()
    if stripped in ("skip", "s", ""):
        return None

    try:
        num = int(stripped)
        if 1 <= num <= len(repos):
            return num - 1
    except ValueError:
        pass

    print_warning(f"Invalid selection: {raw}")
    return None


# ── Brownfield async core logic ──────────────────────────────────


async def _scan_and_register_repos(scan_root: Path | None = None) -> list[dict]:
    """Scan a root directory and register repos/worktrees in DB.

    Uses upsert semantics so that manually-registered repos outside the
    scan root are preserved across re-scans. The walk is depth-bounded and
    registers each repo/worktree it reaches directly; Git worktree families
    are not expanded.

    Returns:
        List of repo dicts with path, name, desc, is_default.
    """
    scan_root = scan_root or Path.home()
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await scan_and_register(store, root=scan_root)
        return [
            {
                "path": r.path,
                "name": r.name,
                "desc": r.desc or "",
                "is_default": r.is_default,
            }
            for r in repos
        ]
    finally:
        await store.close()


async def _list_repos() -> list[dict]:
    """List all registered brownfield repos from DB.

    Returns:
        List of repo dicts with path, name, desc, is_default.
    """
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await store.list()
        return [
            {
                "path": r.path,
                "name": r.name,
                "desc": r.desc or "",
                "is_default": r.is_default,
            }
            for r in repos
        ]
    finally:
        await store.close()


async def _set_default_repo(path: str) -> bool:
    """Toggle a repo's default status in DB.

    If the repo is currently a default, removes it.
    If not, adds it as a default.

    Args:
        path: Absolute path of the repo.

    Returns:
        True if successful.
    """
    store = BrownfieldStore()
    try:
        await store.initialize()
        repos = await store.list()
        current = next((r for r in repos if r.path == path), None)
        if current is None:
            return False
        if current.is_default:
            # Remove from defaults
            result = await store.update_is_default(path, is_default=False)
        else:
            # Add as default
            result = await set_default_repo(store, path)
        return result is not None
    finally:
        await store.close()


# ── CLI Commands ─────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def setup(
    ctx: typer.Context,
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            "-r",
            help="Runtime backend to configure (claude, claude-sdk, claude-cli, codex, opencode, hermes, gemini, goose, kiro, copilot, pi, gjc, antigravity, grok, zcode, host).",
        ),
    ] = None,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Skip interactive prompts (for scripted installs).",
        ),
    ] = False,
    opencode_mode: Annotated[
        str,
        typer.Option(
            "--opencode-mode",
            help="OpenCode integration mode (mutually exclusive): plugin (default) or subprocess.",
        ),
    ] = "plugin",
    mcp_mode: Annotated[
        str,
        typer.Option(
            "--mcp-mode",
            help="Codex MCP config mode: auto preserves native-Windows safety and user entries; http explicitly writes the native-Windows loopback URL; preserve skips MCP changes; stdio replaces with the managed entry on supported hosts.",
        ),
    ] = "auto",
    preserve_existing_llm: Annotated[
        bool,
        typer.Option(
            "--preserve-existing-llm",
            help="Preserve an existing independent LLM backend during runtime refresh.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Set up Ouroboros for your environment.

    Detects available runtimes (Claude Code, Codex, OpenCode, Hermes, Gemini, Kiro, Copilot, Goose, Pi, GJC, Antigravity, Grok, Zcode)
    and configures Ouroboros to use the selected backend.

    [dim]Examples:[/dim]
    [dim]    ouroboros setup                      # auto-detect[/dim]
    [dim]    ouroboros setup --runtime codex      # use Codex[/dim]
    [dim]    ouroboros setup --runtime claude     # use Claude Code[/dim]
    [dim]    ouroboros setup --runtime opencode   # use OpenCode[/dim]
    [dim]    ouroboros setup --runtime gemini     # use Gemini CLI[/dim]
    [dim]    ouroboros setup --runtime kiro       # use Kiro CLI[/dim]
    [dim]    ouroboros setup --runtime copilot    # use GitHub Copilot CLI[/dim]
    [dim]    ouroboros setup --runtime pi         # use Pi CLI[/dim]
    [dim]    ouroboros setup --runtime goose      # use Goose[/dim]
    [dim]    ouroboros setup --runtime gjc        # use GJC[/dim]
    [dim]    ouroboros setup --runtime zcode      # use Zcode[/dim]
    [dim]    ouroboros setup --runtime host       # dispatch to the calling MCP host[/dim]
    [dim]    ouroboros setup scan               # scan brownfield repos[/dim]
    [dim]    ouroboros setup list               # list brownfield repos[/dim]
    [dim]    ouroboros setup default            # toggle default repos[/dim]
    """
    if ctx.invoked_subcommand is not None:
        return
    if has_unsupported_claude_sdk_mcp_mix():
        print_error(escape(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE))
        raise typer.Exit(1)

    console.print("\n[bold cyan]Ouroboros Setup[/bold cyan]\n")

    # Show current backend if already configured
    current_backend = _get_current_backend()
    if current_backend:
        console.print(f"[bold]Current backend:[/bold] [cyan]{current_backend}[/cyan]")
        console.print()

    # Detect available runtimes
    detected = _detect_runtimes()
    available = {k: v for k, v in detected.items() if v is not None}
    # All Claude profiles share one executable. Expose an explicit alias only
    # when already configured so no-argument setup preserves its transport.
    if current_backend in {"claude-sdk", "claude-cli"} and "claude" in available:
        available[current_backend] = available["claude"]

    if available:
        console.print("[bold]Detected runtimes:[/bold]")
        for name, path in available.items():
            marker = " [yellow](current)[/yellow]" if name == current_backend else ""
            console.print(f"  [green]✓[/green] {name} → {path}{marker}")
    else:
        console.print("[yellow]No runtimes detected in PATH.[/yellow]")

    unavailable = {k for k, v in detected.items() if v is None}
    for name in unavailable:
        console.print(f"  [dim]✗ {name} (not found)[/dim]")

    console.print()

    # Resolve which runtime to configure
    selected = runtime
    if selected is None:
        if len(available) == 1:
            selected = next(iter(available))
            print_info(f"Auto-selected: {selected}")
        elif len(available) > 1:
            if non_interactive:
                # Prefer the previously-configured backend so unattended
                # upgrades (e.g. install.sh re-runs) do not silently rewrite
                # a deliberate user choice. Fall back to claude only when no
                # prior choice exists, then to the first available CLI.
                if current_backend and current_backend in available:
                    selected = current_backend
                    print_info(f"Non-interactive mode, preserving current backend: {selected}")
                elif "claude" in available:
                    selected = "claude"
                    print_info(f"Non-interactive mode, selected: {selected}")
                else:
                    selected = next(iter(available))
                    print_info(f"Non-interactive mode, selected: {selected}")
            else:
                choices = list(available.keys())
                default_idx = "1"
                for i, name in enumerate(choices, 1):
                    current_mark = " [yellow](current)[/yellow]" if name == current_backend else ""
                    console.print(f"  [{i}] {name}{current_mark}")
                    if name == current_backend:
                        default_idx = str(i)
                console.print()
                choice = typer.prompt("Select runtime", default=default_idx)
                try:
                    idx = int(choice) - 1
                    selected = choices[idx]
                except (ValueError, IndexError):
                    selected = choice
        else:
            print_error(
                "No runtimes found.\n\n"
                "Install one of:\n"
                "  • Claude Code: https://claude.ai/download\n"
                "  • Codex CLI:   npm install -g @openai/codex\n"
                "  • OpenCode:    npm install -g opencode-ai\n"
                "  • Hermes CLI:  https://hermes.ai/cli\n"
                "  • Gemini CLI:  npm install -g @google/gemini-cli\n"
                "  • Kiro CLI:    https://kiro.dev/docs/cli/\n"
                "  • Copilot CLI: https://docs.github.com/copilot/github-copilot-in-the-cli\n"
                "  • Pi CLI:      npm install -g --ignore-scripts @earendil-works/pi-coding-agent\n"
                "  • GJC CLI:     npm install -g gajae-code"
            )
            raise typer.Exit(1)

    # Validate selection
    if selected in ("claude", "claude_code"):
        claude_path = available.get("claude")
        if not claude_path:
            print_error("Claude Code CLI not found in PATH.")
            raise typer.Exit(1)
        if not _setup_claude(claude_path):
            raise typer.Exit(1)
    elif selected in ("claude-cli", "claude_mcp"):
        claude_path = available.get("claude")
        if not claude_path:
            print_error("Claude Code CLI not found in PATH.")
            raise typer.Exit(1)
        if not _setup_claude_cli(claude_path):
            raise typer.Exit(1)
    elif selected in ("claude-sdk", "claude_sdk"):
        claude_path = available.get("claude")
        if not claude_path:
            print_error("Claude Code CLI not found in PATH.")
            raise typer.Exit(1)
        if not _setup_claude_sdk(claude_path):
            raise typer.Exit(1)
    elif selected in ("codex", "codex_cli"):
        codex_path = available.get("codex")
        if not codex_path:
            env_codex_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH", "").strip()
            if env_codex_path:
                print_error(
                    "Configured Codex CLI override is not executable: "
                    f"OUROBOROS_CODEX_CLI_PATH={env_codex_path}"
                )
            else:
                print_error("Codex CLI not found in PATH or Codex App bundle.")
            raise typer.Exit(1)
        if not _setup_codex(
            codex_path,
            mcp_mode=_normalize_codex_mcp_mode(mcp_mode),
            preserve_existing_llm=preserve_existing_llm,
        ):
            raise typer.Exit(1)
    elif selected in ("opencode", "opencode_cli"):
        opencode_path = available.get("opencode")
        if not opencode_path:
            print_error("OpenCode CLI not found in PATH.")
            raise typer.Exit(1)
        mode = opencode_mode
        if mode not in ("plugin", "subprocess"):
            print_error(f"Invalid --opencode-mode: {mode!r}. Use 'plugin' or 'subprocess'.")
            raise typer.Exit(1)
        if not non_interactive:
            console.print("\n[bold]OpenCode integration mode (pick one):[/bold]")
            console.print(
                "  [1] plugin      — bridge plugin (interactive OpenCode sessions, recommended)"
            )
            console.print(
                "  [2] subprocess  — subprocess runtime (headless ouroboros run, CI, scripted)"
            )
            console.print(
                "[dim]Mutually exclusive — wiring both causes duplicate subagent dispatch.[/dim]"
            )
            console.print(
                "[dim]To wire both deliberately: run setup twice with different --opencode-mode.[/dim]"
            )
            console.print()
            default_pick = "1" if mode == "plugin" else "2"
            pick = typer.prompt("Select mode", default=default_pick)
            mode = {"1": "plugin", "2": "subprocess"}.get(pick.strip(), pick.strip())
            if mode not in ("plugin", "subprocess"):
                print_error(f"Invalid selection: {pick!r}")
                raise typer.Exit(1)
        if not _setup_opencode(opencode_path, mode=mode):
            raise typer.Exit(1)
    elif selected in ("hermes", "hermes_cli"):
        hermes_path = available.get("hermes")
        if not hermes_path:
            print_error("Hermes CLI not found in PATH.")
            raise typer.Exit(1)
        if not _setup_hermes(hermes_path):
            raise typer.Exit(1)
    elif selected in ("gemini", "gemini_cli"):
        gemini_path = available.get("gemini")
        if not gemini_path:
            print_error(
                "Gemini CLI not found.\n"
                "Install it (npm install -g @google/gemini-cli), set "
                "OUROBOROS_GEMINI_CLI_PATH, or configure orchestrator.gemini_cli_path."
            )
            raise typer.Exit(1)
        _setup_gemini(gemini_path)
    elif selected in ("kiro", "kiro_cli"):
        kiro_path = available.get("kiro")
        if not kiro_path:
            print_error(
                "Kiro CLI not found.\n"
                "Install it from https://kiro.dev/docs/cli/, set "
                "OUROBOROS_KIRO_CLI_PATH, or configure orchestrator.kiro_cli_path."
            )
            raise typer.Exit(1)
        if not _setup_kiro(kiro_path):
            raise typer.Exit(1)
    elif selected in ("copilot", "copilot_cli"):
        copilot_path = available.get("copilot")
        if not copilot_path:
            print_error(
                "GitHub Copilot CLI not found.\n"
                "Install it from https://docs.github.com/copilot/github-copilot-in-the-cli, set "
                "OUROBOROS_COPILOT_CLI_PATH, or configure orchestrator.copilot_cli_path."
            )
            raise typer.Exit(1)
        if not _setup_copilot(copilot_path, non_interactive=non_interactive):
            raise typer.Exit(1)
    elif selected in ("pi", "pi_cli"):
        pi_path = available.get("pi")
        if not pi_path:
            print_error(
                "Pi CLI not found.\n"
                "Install it with npm install -g --ignore-scripts "
                "@earendil-works/pi-coding-agent, set OUROBOROS_PI_CLI_PATH, "
                "or configure orchestrator.pi_cli_path."
            )
            raise typer.Exit(1)
        _setup_pi(pi_path)
    elif selected in ("gjc", "gajae-code", "gajae_code"):
        gjc_path = available.get("gjc")
        if not gjc_path:
            print_error(
                "GJC CLI not found.\n"
                "Install it, set OUROBOROS_GJC_CLI_PATH, or configure orchestrator.gjc_cli_path."
            )
            raise typer.Exit(1)
        if not _setup_gjc(gjc_path):
            raise typer.Exit(1)
    elif selected in ("goose", "goose_cli"):
        goose_path = available.get("goose")
        if not goose_path:
            print_error(
                "Goose CLI not found.\n"
                "Install it from https://block.github.io/goose/, set "
                "OUROBOROS_GOOSE_CLI_PATH, or configure orchestrator.goose_cli_path."
            )
            raise typer.Exit(1)
        _setup_goose(goose_path)
    elif selected in ("antigravity", "agy"):
        antigravity_path = available.get("antigravity")
        if not antigravity_path:
            print_error(
                "Antigravity CLI (agy) not found.\n"
                "Install it, set OUROBOROS_ANTIGRAVITY_CLI_PATH, or configure "
                "orchestrator.antigravity_cli_path."
            )
            raise typer.Exit(1)
        _setup_antigravity(antigravity_path)
    elif selected in ("grok", "grok_cli", "grok_build"):
        grok_path = available.get("grok")
        if not grok_path:
            print_error(
                "Grok Build CLI (grok) not found.\n"
                "Install it (curl -fsSL https://x.ai/cli/install.sh | bash), set "
                "OUROBOROS_GROK_CLI_PATH, or configure orchestrator.grok_cli_path."
            )
            raise typer.Exit(1)
        _setup_grok(grok_path)
    elif selected in ("zcode", "zcode_cli"):
        zcode_path = available.get("zcode")
        if not zcode_path:
            print_error(
                "Zcode CLI not found.\n"
                "Install ZCode, set OUROBOROS_ZCODE_CLI_PATH, configure "
                "orchestrator.zcode_cli_path, or put a zcode executable on PATH."
            )
            raise typer.Exit(1)
        _setup_zcode(zcode_path)
    elif selected in ("host", "host_dispatch"):
        if not _setup_host():
            raise typer.Exit(1)
    else:
        print_error(f"Unsupported runtime: {selected}")
        raise typer.Exit(1)

    print_setup_completion(console, selected)


# ── Artifact refresh subcommand ──────────────────────────────────


def _opencode_bridge_dest() -> Path:
    plugin_dir = opencode_config_dir()
    for part in _BRIDGE_PLUGIN_SUBDIR:
        plugin_dir = plugin_dir / part
    return plugin_dir / _BRIDGE_PLUGIN_FILENAME


@app.command("refresh")
def refresh_artifacts() -> None:
    """Refresh artifacts installed by a previous setup."""
    from ouroboros.cli.commands.setup_refresh import refresh_runtime_artifacts

    refresh_runtime_artifacts()


# ── Brownfield subcommands ───────────────────────────────────────


@app.command()
def scan(
    scan_root: Annotated[
        Path | None,
        typer.Argument(
            help="Root directory for the brownfield scan. Defaults to the current user's home directory.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Re-scan a root directory and register new repos.

    Scans the requested root (up to a shallow depth) for valid seed git
    repos/worktrees and updates the brownfield registry. Each repo/worktree
    the walk reaches directly is registered self-only; Git worktree families
    are not expanded. Local repos and repos with any remote name are eligible.
    Existing repos are preserved (upsert).
    """
    effective_scan_root = scan_root or Path.home()
    console.print("\n[bold cyan]Brownfield Scan[/]\n")

    try:
        repos = asyncio.run(_run_scan_only(effective_scan_root))
    except KeyboardInterrupt:
        print_info("\nScan interrupted.")
        raise typer.Exit(code=0)

    if not repos:
        print_warning("No repos found.")
        return

    print_success(f"Registered {len(repos)} repo(s).\n")
    _display_repos_table(repos)


async def _run_scan_only(scan_root: Path) -> list[dict]:
    """Scan and register, returning repo list."""
    with console.status("[cyan]Scanning scan root for repos...[/]", spinner="dots"):
        return await _scan_and_register_repos(scan_root)


@app.command(name="list")
def list_command() -> None:
    """List all registered brownfield repos."""
    console.print("\n[bold cyan]Registered Brownfield Repos[/]\n")

    try:
        repos = asyncio.run(_list_repos())
    except KeyboardInterrupt:
        raise typer.Exit(code=0)

    if not repos:
        print_info("No repos registered. Run [bold]ouroboros setup scan[/] first.")
        return

    _display_repos_table(repos)

    total = len(repos)
    default_count = sum(1 for r in repos if r.get("is_default"))
    console.print(f"\n[dim]Total: {total} repo(s), {default_count} default(s)[/]\n")


@app.command()
def default() -> None:
    """Toggle default brownfield repos for PM interviews.

    Displays all registered repos and lets you toggle defaults (multi-default supported).
    """
    console.print("\n[bold cyan]Set Default Brownfield Repos[/]\n")

    try:
        asyncio.run(_run_set_default())
    except KeyboardInterrupt:
        print_info("\nCancelled.")
        raise typer.Exit(code=0)


async def _run_set_default() -> None:
    """Interactive default repo selection."""
    repos = await _list_repos()

    if not repos:
        print_warning("No repos registered. Run [bold]ouroboros setup scan[/] first.")
        return

    _display_repos_table(repos)
    console.print()

    idx = _prompt_repo_selection(repos, "Select default repos")
    if idx is None:
        print_info("No changes made.")
        return

    selected = repos[idx]
    with console.status("[cyan]Setting defaults...[/]", spinner="dots"):
        success = await _set_default_repo(selected["path"])

    if success:
        print_success(f"Default repos updated: [cyan]{selected['name']}[/] ({selected['path']})")
    else:
        print_error(f"Failed to set defaults: {selected['path']}")
