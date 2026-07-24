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
from contextlib import contextmanager
from copy import deepcopy
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Annotated, Literal, NoReturn

from rich.prompt import Prompt
from rich.table import Table
import typer
import yaml

from ouroboros.bigbang.brownfield import scan_and_register, set_default_repo
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
from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
from ouroboros.persistence.brownfield import BrownfieldStore


def _build_uvx_mcp_args(package_spec: str) -> list[str]:
    """Return the canonical uvx args for the requested Ouroboros package spec."""
    return ["--from", package_spec, "ouroboros", "mcp", "serve"]


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _detect_mcp_entry(*, package_spec: str = "ouroboros-ai[mcp]") -> dict[str, object] | None:
    """Build the correct MCP entry based on how ouroboros is installed.

    Priority: uvx > ouroboros binary > python3 -m ouroboros (verified).
    Returns None if no working method is found.
    Matches the contract in install.sh and skills/setup/SKILL.md.
    """
    if shutil.which("uvx"):
        return {"command": "uvx", "args": _build_uvx_mcp_args(package_spec)}
    if shutil.which("ouroboros"):
        return {"command": "ouroboros", "args": ["mcp", "serve"]}
    # Only use python3 fallback if ouroboros is actually importable
    import subprocess

    try:
        subprocess.run(
            ["python3", "-c", "import ouroboros"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return {"command": "python3", "args": ["-m", "ouroboros", "mcp", "serve"]}


def _ensure_claude_mcp_entry() -> None:
    """Ensure ~/.claude/mcp.json has a correct ouroboros MCP entry.

    Creates the entry if missing (detecting install method), updates stale
    uvx args (e.g. ouroboros-ai without [claude] extras), and removes the
    legacy timeout key.  Skips the file write when nothing changed.
    """
    mcp_config_path = Path.home() / ".claude" / "mcp.json"
    try:
        mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_warning(
            f"Could not create {mcp_config_path.parent} — MCP registration skipped: {exc}"
        )
        return

    try:
        if mcp_config_path.is_symlink():
            print_warning(
                f"Could not update {mcp_config_path} because it is a symlink — "
                "leaving it untouched. Update the linked configuration target manually "
                "and re-run setup."
            )
            return
        if mcp_config_path.exists() and mcp_config_path.stat().st_nlink > 1:
            print_warning(
                f"Could not update {mcp_config_path} because it is hard-linked — "
                "leaving it untouched. Update the linked configuration manually "
                "and re-run setup."
            )
            return
        config_exists = mcp_config_path.exists()
    except OSError as exc:
        print_warning(f"Could not inspect {mcp_config_path} — leaving it untouched: {exc}")
        return

    mcp_data: dict[str, object] = {}
    if config_exists:
        try:
            mcp_data = json.loads(
                mcp_config_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except UnicodeDecodeError:
            print_warning(
                f"Could not decode {mcp_config_path} as UTF-8 — leaving it untouched. "
                "Fix the file encoding and re-run setup."
            )
            return
        except (json.JSONDecodeError, ValueError):
            print_warning(
                f"Could not parse {mcp_config_path} — skipping MCP registration to avoid "
                "overwriting existing settings. Fix the JSON syntax and re-run setup."
            )
            return
        except OSError as exc:
            print_warning(f"Could not read {mcp_config_path} — leaving it untouched: {exc}")
            return
        if not isinstance(mcp_data, dict):
            print_warning(f"{mcp_config_path} top-level is not an object — leaving it untouched.")
            return

    if "mcpServers" not in mcp_data:
        servers: dict[str, object] = {}
        mcp_data["mcpServers"] = servers
    else:
        raw_servers = mcp_data["mcpServers"]
        if not isinstance(raw_servers, dict):
            print_warning(
                f"{mcp_config_path} 'mcpServers' section is not an object — leaving it untouched."
            )
            return
        servers = raw_servers

    if "ouroboros" not in servers:
        existing: dict[str, object] | None = None
    else:
        raw_existing = servers["ouroboros"]
        if not isinstance(raw_existing, dict):
            print_warning(
                f"{mcp_config_path} mcpServers.ouroboros is not an object — leaving it untouched."
            )
            return
        existing = raw_existing
        if "command" in existing and not isinstance(existing["command"], str):
            print_warning(
                f"{mcp_config_path} mcpServers.ouroboros.command is not a string — "
                "leaving it untouched."
            )
            return

    detected = _detect_mcp_entry(package_spec="ouroboros-ai[mcp,claude]")
    needs_write = False
    registered = False
    removed_timeout = False
    updated_entry = False

    if existing is None:
        if detected is None:
            print_warning(
                "Cannot register MCP server: no working ouroboros installation found.\n"
                "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
            )
            return
        servers["ouroboros"] = detected
        needs_write = True
        registered = True
    else:
        # Remove legacy timeout key
        if "timeout" in existing:
            del existing["timeout"]
            needs_write = True
            removed_timeout = True

        # Update entry to match currently detected install method, but only
        # for known standard commands. Custom entries (docker, nix, etc.) are
        # left untouched so we don't break user-managed configurations.
        _KNOWN_COMMANDS = {"uvx", "ouroboros", "python3", "python"}
        if detected is not None and existing.get("command") in _KNOWN_COMMANDS:
            if (
                existing.get("command") != detected["command"]
                or existing.get("args") != detected["args"]
            ):
                existing["command"] = detected["command"]
                existing["args"] = detected["args"]
                needs_write = True
                updated_entry = True

        if not needs_write:
            print_info("MCP server already registered.")

    if needs_write:
        try:
            mode = stat.S_IMODE(mcp_config_path.stat().st_mode) if config_exists else 0o600
            _atomic_write_text(mcp_config_path, json.dumps(mcp_data, indent=2), mode=mode)
        except (OSError, TypeError, ValueError) as exc:
            print_warning(f"Could not write {mcp_config_path} — leaving it untouched: {exc}")
            return

        if registered:
            print_success("Registered MCP server in ~/.claude/mcp.json")
        if removed_timeout:
            print_info("Removed legacy MCP timeout override.")
        if updated_entry:
            print_info("Updated MCP server entry to match current install method.")


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
        return data.get("orchestrator", {}).get("runtime_backend")
    except Exception:
        return None


def _detect_runtimes() -> dict[str, str | None]:
    """Detect available runtime CLIs in PATH.

    For Gemini, Goose, and Kiro, we additionally honor explicit-path overrides
    and persisted orchestrator ``*_cli_path`` settings so users with non-PATH
    installs are still detected.
    """
    runtimes: dict[str, str | None] = {}
    for name in ("claude", "codex", "opencode", "hermes"):
        path = shutil.which(name)
        runtimes[name] = path

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

CodexMcpMode = Literal["auto", "preserve", "stdio"]
_CODEX_UVX_MCP_ARGS = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
_CODEX_DIRECT_MCP_ARGS = ["mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"]
_CODEX_MODULE_MCP_ARGS = ["-m", "ouroboros", *_CODEX_DIRECT_MCP_ARGS]
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
        "providers": {"codex": {"profile": "ouroboros-fast"}},
    },
    "standard": {
        "max_turns": 3,
        "temperature": 0.3,
        "providers": {"codex": {"profile": "ouroboros-standard"}},
    },
    "deep": {
        "max_turns": 5,
        "temperature": 0.4,
        "providers": {"codex": {"profile": "ouroboros-deep"}},
    },
    "frontier": {
        "max_turns": 8,
        "temperature": 0.4,
        "providers": {"codex": {"profile": "ouroboros-frontier"}},
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

_DEFAULT_CONSENSUS_MODELS = (
    "openrouter/openai/gpt-4o",
    DEFAULT_CONSENSUS_OPUS_MODEL,
    "openrouter/google/gemini-2.5-pro",
)

_MISSING = object()

_CODEX_ROLE_MODEL_OVERRIDE_DEFAULTS: dict[str, tuple[tuple[tuple[str, ...], object], ...]] = {
    "ambiguity": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "assertion_extraction": ((("evaluation", "assertion_extraction_model"), DEFAULT_SONNET_MODEL),),
    "brownfield_explore": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "clarification": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "consensus_advocate": ((("consensus", "advocate_model"), DEFAULT_CONSENSUS_OPUS_MODEL),),
    "consensus_judge": ((("consensus", "judge_model"), "openrouter/google/gemini-2.5-pro"),),
    "consensus_vote": ((("consensus", "models"), _DEFAULT_CONSENSUS_MODELS),),
    "context_compression": ((("llm", "context_compression_model"), "gpt-4"),),
    "dependency_analysis": ((("llm", "dependency_analysis_model"), DEFAULT_SONNET_MODEL),),
    "mechanical_detection": ((("evaluation", "assertion_extraction_model"), DEFAULT_SONNET_MODEL),),
    "ontology_analysis": (
        (("llm", "ontology_analysis_model"), DEFAULT_SONNET_MODEL),
        (("consensus", "devil_model"), "openrouter/openai/gpt-4o"),
    ),
    "pm_interview": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "qa": ((("llm", "qa_model"), DEFAULT_SONNET_MODEL),),
    "reflect": ((("resilience", "reflect_model"), DEFAULT_OPUS_MODEL),),
    "seed_generation": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "semantic_evaluation": ((("evaluation", "semantic_model"), DEFAULT_OPUS_MODEL),),
    "wonder": ((("resilience", "wonder_model"), DEFAULT_OPUS_MODEL),),
}


def _normalize_codex_mcp_mode(value: str) -> CodexMcpMode:
    """Validate and normalize the Codex MCP setup mode."""
    normalized = value.lower()
    if normalized not in {"auto", "preserve", "stdio"}:
        print_error("Unsupported Codex MCP mode. Use one of: auto, preserve, stdio.")
        raise typer.Exit(1)
    return normalized  # type: ignore[return-value]


def _codex_mcp_entry_from_toml(data: dict[str, object]) -> dict[str, object] | None:
    """Return the parsed Ouroboros Codex MCP entry, if present."""
    mcp_servers = data.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return None
    entry = mcp_servers.get("ouroboros")
    return entry if isinstance(entry, dict) else None


def _is_source_tree_ouroboros_build() -> bool:
    """Return whether this module is executing from an Ouroboros source tree."""
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        pyproject = parent / "pyproject.toml"
        source_package = parent / "src" / "ouroboros"
        if not pyproject.exists():
            continue
        if not current_file.is_relative_to(source_package):
            continue
        try:
            pyproject_text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if 'name = "ouroboros-ai"' in pyproject_text and source_package.is_dir():
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


def _render_codex_mcp_section() -> str:
    """Render the managed Codex MCP block for the current install source.

    Release installs keep the historical ``uvx --from ouroboros-ai[mcp]``
    command so Codex can bootstrap the MCP extra even if the invoking Python
    environment lacks it. Dev/git installs must instead point Codex at this
    Python environment; otherwise setup silently downgrades the MCP server back
    to the latest PyPI release and hides main-branch fixes under test.
    """
    if _is_dev_ouroboros_build():
        command_lines = "\n".join(
            (
                f"command = {json.dumps(sys.executable)}",
                f"args = {json.dumps(_CODEX_MODULE_MCP_ARGS)}",
            )
        )
    else:
        command_lines = "\n".join(
            (
                'command = "uvx"',
                f"args = {json.dumps(_CODEX_UVX_MCP_ARGS)}",
            )
        )
    return _CODEX_MCP_SECTION_TEMPLATE.format(command_lines=command_lines)


def _has_managed_codex_mcp_comment(raw: str) -> bool:
    """Return whether the Ouroboros MCP table carries setup's managed comment."""
    lines = raw.splitlines()
    expected = list(_CODEX_MCP_COMMENT_LINES)
    for index, line in enumerate(lines):
        if line.strip() != "[mcp_servers.ouroboros]":
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

    # Current and legacy setup-managed uvx configs both end by launching
    # `ouroboros mcp serve`; keep accepting those even before setup added the
    # managed comment block. Non-uvx dev/worktree shapes are only setup-owned
    # when that managed comment is present, so user-pinned interpreters or
    # wrapper scripts with the same args remain preserved in auto mode.
    if command == "uvx":
        return len(args) >= 3 and args[-3:] == ["ouroboros", "mcp", "serve"]
    if not has_managed_comment:
        return False
    if Path(command).name == "ouroboros":
        return args == _CODEX_DIRECT_MCP_ARGS
    return args == _CODEX_MODULE_MCP_ARGS


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
    return line == "[profiles.ouroboros-worker]" or line.startswith("[profiles.ouroboros-worker.")


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
        if stripped == "[profiles.ouroboros-worker]" and not refreshed:
            existed_before = True
            refreshed = True
            _trim_managed_codex_worker_profile_comments(output_lines)
            if output_lines and output_lines[-1].strip():
                output_lines.append("")
            output_lines.extend(section_lines)
            index += 1
            continue
        if _is_codex_ouroboros_worker_profile_header(stripped):
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


def _is_codex_ouroboros_table_header(line: str) -> bool:
    """Return True when the line starts the managed Codex MCP table."""
    return line == "[mcp_servers.ouroboros]" or line.startswith("[mcp_servers.ouroboros.")


def _trim_managed_codex_comments(lines: list[str]) -> None:
    """Remove the managed Codex comment block immediately before a table."""
    while lines and not lines[-1].strip():
        lines.pop()

    comment_index = len(lines)
    for expected in reversed(_CODEX_MCP_COMMENT_LINES):
        if comment_index == 0 or lines[comment_index - 1] != expected:
            return
        comment_index -= 1

    del lines[comment_index:]


def _upsert_codex_mcp_section(raw: str) -> tuple[str, bool]:
    """Insert or replace the managed Codex MCP block.

    Returns:
        Tuple of (updated_contents, existed_before).
    """
    section_lines = _render_codex_mcp_section().strip("\n").splitlines()
    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    existed_before = False
    inserted = False

    while index < len(input_lines):
        stripped = input_lines[index].strip()
        if _is_codex_ouroboros_table_header(stripped):
            existed_before = True
            if not inserted:
                _trim_managed_codex_comments(output_lines)
                if output_lines and output_lines[-1].strip():
                    output_lines.append("")
                output_lines.extend(section_lines)
                inserted = True

            index += 1
            while index < len(input_lines):
                next_stripped = input_lines[index].strip()
                is_table_header = next_stripped.startswith("[") and next_stripped.endswith("]")
                if is_table_header and not _is_codex_ouroboros_table_header(next_stripped):
                    break
                index += 1
            continue

        output_lines.append(input_lines[index])
        index += 1

    if not inserted:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.extend(section_lines)

    return "\n".join(output_lines).rstrip() + "\n", existed_before


def _codex_uses_profile_v2(codex_path: str | None = None) -> bool:
    """Return whether ``codex --profile`` expects ``<name>.config.toml`` files."""
    if codex_path is None:
        return False

    command = codex_path or "codex"
    try:
        result = subprocess.run(
            [command, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False

    help_text = f"{result.stdout}\n{result.stderr}"
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if "--profile-v2" in line:
            continue
        if "--profile" not in line:
            continue

        description_lines: list[str] = []
        for description_line in lines[index + 1 :]:
            stripped = description_line.strip()
            if stripped.startswith("-") or stripped.startswith("--"):
                break
            if stripped:
                description_lines.append(stripped)
        return any(
            "Layer $CODEX_HOME/<name>.config.toml on top of the base user config"
            in description_line
            for description_line in description_lines
        )

    return False


def _register_codex_mcp_server(*, mode: CodexMcpMode = "auto") -> None:
    """Register the Ouroboros MCP/env hookup in ~/.codex/config.toml."""
    import tomllib

    if mode == "preserve":
        print_info("Preserved Codex MCP config.")
        return

    codex_config = Path.home() / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)

    if codex_config.exists():
        raw = codex_config.read_text(encoding="utf-8")
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            print_error(f"Could not parse {codex_config} — skipping MCP registration.")
            return

        entry = _codex_mcp_entry_from_toml(parsed)
        has_managed_comment = _has_managed_codex_mcp_comment(raw)
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
            return

        updated_raw, existed_before = _upsert_codex_mcp_section(raw)
        if updated_raw == raw:
            print_info("Codex MCP server already up to date.")
            return

        codex_config.write_text(updated_raw, encoding="utf-8")
        if existed_before:
            print_success(f"Updated Ouroboros MCP server in {codex_config}")
        else:
            print_success(f"Registered Ouroboros MCP server in {codex_config}")
    else:
        codex_config.write_text(_render_codex_mcp_section().lstrip("\n"), encoding="utf-8")
        print_success(f"Registered Ouroboros MCP server in {codex_config}")


def _render_codex_profile_section(name: str, settings: _CodexProfileSettings) -> str:
    """Render a sparse Codex profile table used as an Ouroboros anchor."""
    lines = [_CODEX_PROFILE_COMMENT, f"[profiles.{name}]"]
    for key, value in settings.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines)


def _render_codex_profile_v2_file(settings: _CodexProfileSettings) -> str:
    """Render a sparse Codex profile-v2 file."""
    lines = [_CODEX_PROFILE_V2_COMMENT, *_render_toml_mapping(settings)]
    return "\n".join(lines).rstrip() + "\n"


def _render_toml_mapping(settings: _CodexProfileSettings, prefix: str = "") -> list[str]:
    """Render simple TOML mappings, preserving nested profile subtables."""
    lines: list[str] = []
    nested: list[tuple[str, dict[str, object]]] = []
    for key, value in settings.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{key} = {json.dumps(value)}")

    for key, value in nested:
        section_name = f"{prefix}.{key}" if prefix else key
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
    for name in profile_names:
        if line == f"[profiles.{name}]" or line.startswith(f"[profiles.{name}."):
            return True
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
                    migrated[name] = {
                        str(key): value
                        for key, value in profile.items()
                        if isinstance(value, str | int | float | bool | list | dict)
                    }

    input_lines = raw.splitlines()
    output_lines: list[str] = []
    index = 0
    while index < len(input_lines):
        stripped = input_lines[index].strip()
        if _is_codex_ouroboros_profile_header(stripped, profile_names):
            _trim_managed_codex_worker_profile_comments(output_lines)
            while output_lines and output_lines[-1] == _CODEX_PROFILE_COMMENT:
                output_lines.pop()
            while output_lines and not output_lines[-1].strip():
                output_lines.pop()

            index += 1
            while index < len(input_lines):
                next_stripped = input_lines[index].strip()
                is_table_header = next_stripped.startswith("[") and next_stripped.endswith("]")
                if is_table_header and not _is_codex_ouroboros_profile_header(
                    next_stripped, profile_names
                ):
                    break
                index += 1
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


def _register_codex_default_profiles(*, codex_path: str | None = None) -> None:
    """Register default Codex profile anchors for Ouroboros task profiles."""
    import tomllib

    codex_config = Path.home() / ".codex" / "config.toml"
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
            codex_config.write_text(updated_raw, encoding="utf-8")
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

    codex_config.write_text(updated_raw, encoding="utf-8")
    print_success(f"Registered Codex task profiles in {codex_config}: {', '.join(added_profiles)}")


def _register_codex_worker_profile(*, codex_path: str | None = None) -> None:
    """Register the managed Codex worker profile in ~/.codex/config.toml."""
    import tomllib

    codex_config = Path.home() / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)

    if codex_config.exists():
        raw = codex_config.read_text(encoding="utf-8")
        try:
            _existing_codex_profile_names(raw)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} — skipping worker-profile registration.")
            print_info(str(exc))
            return
    else:
        raw = ""

    if _codex_uses_profile_v2(codex_path):
        profile_path = codex_config.parent / f"{_CODEX_WORKER_PROFILE_NAME}.config.toml"
        legacy_profiles = _existing_codex_profile_names(raw)
        removable_profiles = (
            set()
            if _codex_profile_v2_file_exists(codex_config.parent, _CODEX_WORKER_PROFILE_NAME)
            else {_CODEX_WORKER_PROFILE_NAME}
        )
        preserved_legacy_profiles = (
            legacy_profiles & {_CODEX_WORKER_PROFILE_NAME} - removable_profiles
        )
        try:
            updated_raw, migrated_profiles = _remove_codex_legacy_profile_sections(
                raw, removable_profiles
            )
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print_error(f"Could not parse {codex_config} — skipping worker-profile registration.")
            print_info(str(exc))
            return

        settings = migrated_profiles.get(_CODEX_WORKER_PROFILE_NAME, {})
        created_profile = False
        if not profile_path.exists():
            profile_path.write_text(
                _CODEX_WORKER_PROFILE_V2_CONTENT
                + "\n".join(_render_toml_mapping(settings)).rstrip()
                + ("\n" if settings else ""),
                encoding="utf-8",
            )
            created_profile = True
        if updated_raw != raw:
            codex_config.write_text(updated_raw, encoding="utf-8")
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_success(f"Migrated legacy Codex worker profile out of {codex_config}")
        elif created_profile:
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_success(f"Registered Codex worker profile-v2 file in {profile_path}")
        else:
            _warn_preserved_legacy_codex_profiles(codex_config, preserved_legacy_profiles)
            print_info("Codex worker profile-v2 file already present.")
        return

    updated_raw, existed_before = _upsert_codex_worker_profile_section(raw)
    try:
        tomllib.loads(updated_raw)
    except tomllib.TOMLDecodeError as exc:
        print_error(
            f"Could not update {codex_config} — worker-profile registration would create invalid TOML."
        )
        print_info(str(exc))
        return
    if updated_raw == raw:
        print_info("Codex worker profile already up to date.")
        return

    codex_config.write_text(updated_raw, encoding="utf-8")
    if existed_before:
        print_success(f"Updated Codex worker profile in {codex_config}")
    else:
        print_success(f"Registered Codex worker profile in {codex_config}")


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


def _get_nested_value(config_dict: dict, path: tuple[str, ...]) -> object:
    """Read a nested config value, returning _MISSING when absent."""
    current: object = config_dict
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _has_explicit_codex_model_override(config_dict: dict, role: str) -> bool:
    """Return True when an existing model setting should beat setup defaults."""
    for path, _default in _CODEX_ROLE_MODEL_OVERRIDE_DEFAULTS.get(role, ()):
        value = _get_nested_value(config_dict, path)
        if value is not _MISSING:
            return True
    return False


def _install_codex_default_llm_profiles(
    config_dict: dict,
) -> tuple[list[str], list[str], list[str]]:
    """Install missing provider-neutral Ouroboros task profiles for Codex setup.

    Existing profile definitions and explicit legacy per-role model overrides
    are preserved. If a default profile name already exists for another backend,
    merge in only the missing Codex provider mapping before adding role defaults
    that target that profile.
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
        codex_provider = _ensure_profile_provider_mapping(existing_profile, "codex")
        if "profile" not in codex_provider and "model" not in codex_provider:
            codex_provider["profile"] = default_codex["profile"]  # type: ignore[index]
            updated_profiles.append(name)

    added_role_profiles: list[str] = []
    for role, profile_name in _CODEX_DEFAULT_LLM_ROLE_PROFILES.items():
        if role in llm_role_profiles or _has_explicit_codex_model_override(config_dict, role):
            continue
        llm_role_profiles[role] = profile_name
        added_role_profiles.append(role)

    return added_profiles, updated_profiles, added_role_profiles


def _print_codex_config_guidance(config_path: Path) -> None:
    """Explain where Codex users should configure Ouroboros vs. Codex settings."""
    print_info(f"Configure Ouroboros runtime and per-role model overrides in {config_path}.")
    print_info(
        "Use ~/.codex/config.toml for the Codex MCP/env hookup. Codex profile-v2 anchors live in ~/.codex/<profile>.config.toml on current Codex CLI releases."
    )


def _install_codex_artifacts() -> None:
    """Install packaged Ouroboros rules and skills into ~/.codex/."""
    from ouroboros.codex import install_codex_artifacts

    codex_dir = Path.home() / ".codex"

    try:
        result = install_codex_artifacts(codex_dir=codex_dir, prune=True)
        print_success(f"Installed Codex rules → {result.rules_path}")
        print_success(f"Installed {len(result.skill_paths)} Codex skills → {codex_dir / 'skills'}")
    except FileNotFoundError:
        print_error("Could not locate packaged Codex rules or skills.")


def _setup_codex(codex_path: str, *, mcp_mode: CodexMcpMode = "auto") -> None:
    """Configure Ouroboros for the Codex runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("Invalid non-mapping config.yaml contents; aborting without changes.")
        return

    try:
        # Set runtime and LLM backend to codex
        orchestrator_config = _ensure_mapping_section(config_dict, "orchestrator")
        orchestrator_config["runtime_backend"] = "codex"
        orchestrator_config["codex_cli_path"] = codex_path

        llm_config = _ensure_mapping_section(config_dict, "llm")
        llm_config["backend"] = "codex"

        added_profiles, updated_profiles, added_role_profiles = _install_codex_default_llm_profiles(
            config_dict
        )
    except ValueError as exc:
        print_error(f"Invalid config.yaml structure: {exc}")
        print_info("Aborting Codex setup without rewriting config.yaml.")
        return

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Codex runtime (CLI: {codex_path})")
    print_info(f"Config saved to: {config_path}")
    if added_profiles:
        print_info(f"Installed Ouroboros LLM profiles: {', '.join(added_profiles)}")
    if updated_profiles:
        print_info(
            "Added Codex provider mappings to existing Ouroboros LLM profiles: "
            f"{', '.join(updated_profiles)}"
        )
    if added_role_profiles:
        print_info(
            f"Installed Ouroboros role profile defaults for {len(added_role_profiles)} roles."
        )

    # Install Codex-native rules and skills into ~/.codex/
    _install_codex_artifacts()

    # Register MCP server in Codex config (~/.codex/config.toml)
    _register_codex_mcp_server(mode=mcp_mode)
    _register_codex_default_profiles(codex_path=codex_path)
    _register_codex_worker_profile(codex_path=codex_path)
    _print_codex_config_guidance(config_path)


def _install_hermes_artifacts() -> None:
    """Install packaged Ouroboros skills into ~/.hermes/."""
    from ouroboros.hermes.artifacts import install_hermes_skills

    hermes_dir = Path.home() / ".hermes"

    try:
        skill_path = install_hermes_skills(hermes_dir=hermes_dir, prune=True)
        print_success(f"Installed Hermes skills → {skill_path}")
    except FileNotFoundError:
        print_error("Could not locate packaged skills for Hermes.")


def _install_runtime_instruction_artifact(backend: str, **kwargs: object) -> None:
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
        return
    try:
        artifact = installer(**kwargs)
    except OSError as exc:
        print_warning(f"Could not install {backend} instruction artifact: {exc}")
        return
    print_success(f"Installed {backend.title()} instruction guide → {artifact.path}")


def _register_hermes_mcp_server() -> None:
    """Register the Ouroboros MCP hookup in ~/.hermes/config.yaml."""
    hermes_config = Path.home() / ".hermes" / "config.yaml"
    hermes_config.parent.mkdir(parents=True, exist_ok=True)

    config_data: dict = {}
    if hermes_config.exists():
        try:
            loaded_config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
        except Exception:
            print_error(f"Could not parse {hermes_config} — skipping MCP registration.")
            return
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

    # Use UVX install by default for robustness
    detected = _detect_mcp_entry()
    if detected is None:
        print_warning("Cannot register Hermes MCP server: no working Ouroboros installation found.")
        return

    config_data["mcp_servers"]["ouroboros"] = {
        "command": detected["command"],
        "args": detected["args"],
        "enabled": True,
    }

    with hermes_config.open("w", encoding="utf-8") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    print_success(f"Registered Ouroboros MCP server in {hermes_config}")


def _setup_hermes(hermes_path: str) -> None:
    """Configure Ouroboros for the Hermes runtime."""
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

    # Set runtime to Hermes. Do not rewrite llm.backend until Hermes also
    # supports the LLM-only adapter contract used elsewhere in Ouroboros.
    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "hermes"
    orch["hermes_cli_path"] = hermes_path

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Hermes runtime (CLI: {hermes_path})")
    print_info(f"Config saved to: {config_path}")

    # Install Ouroboros skills for Hermes
    _install_hermes_artifacts()

    # Register MCP server
    _register_hermes_mcp_server()


def _detect_mcp_entry_for_kiro() -> dict[str, object] | None:
    """Build an MCP command entry optimized for Kiro CLI.

    Unlike ``_detect_mcp_entry`` (which prefers ``uvx`` for install-method
    robustness), Kiro needs **fast cold-start** because its MCP init timeout
    is shorter than ``uvx``'s first-time environment build can take. When the
    ``ouroboros`` binary is already available (i.e. the user has done
    ``pip install ouroboros-ai``), spawning it directly skips ``uvx``'s
    dependency resolution and keeps startup under Kiro's init deadline.

    Priority: ouroboros binary > uvx > python3 -m ouroboros.
    """
    if (ouroboros_bin := shutil.which("ouroboros")) is not None:
        return {"command": ouroboros_bin, "args": ["mcp", "serve"]}
    if shutil.which("uvx"):
        return {
            "command": "uvx",
            "args": _build_uvx_mcp_args("ouroboros-ai[mcp,claude]"),
        }
    # python3 -m fallback: only valid if ouroboros is importable
    import subprocess

    try:
        subprocess.run(
            ["python3", "-c", "import ouroboros"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return {"command": "python3", "args": ["-m", "ouroboros", "mcp", "serve"]}


def _register_kiro_mcp_server() -> None:
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

    Uses :func:`_detect_mcp_entry_for_kiro` which prefers the direct
    ``ouroboros`` binary over ``uvx`` to stay within Kiro's MCP init
    timeout.
    """
    mcp_config_path = Path.home() / ".kiro" / "settings" / "mcp.json"
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    mcp_data: dict = {}
    if mcp_config_path.exists():
        try:
            mcp_data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print_error(f"Could not parse {mcp_config_path} — skipping Kiro MCP registration.")
            return
        if not isinstance(mcp_data, dict):
            print_warning(f"{mcp_config_path} top-level is not a mapping — resetting.")
            mcp_data = {}

    servers = mcp_data.get("mcpServers")
    if not isinstance(servers, dict):
        if servers is not None:
            print_warning(f"{mcp_config_path} 'mcpServers' section is not a mapping — resetting.")
        servers = {}
        mcp_data["mcpServers"] = servers

    detected = _detect_mcp_entry_for_kiro()
    if detected is None:
        print_warning("Cannot register Kiro MCP server: no working ouroboros installation found.")
        return

    # _KNOWN_COMMANDS also accepts an absolute-path match so that an entry
    # previously written with the venv-resident ``ouroboros`` binary (detector
    # priority 1 output) can still be upgraded on later setup runs.
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
        _KNOWN_COMMANDS = {"uvx", "ouroboros", "python3", "python", "uv"}
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
        with mcp_config_path.open("w", encoding="utf-8") as f:
            json.dump(mcp_data, f, indent=2)


def _setup_kiro(kiro_path: str) -> None:
    """Configure Ouroboros for the Kiro CLI runtime.

    Writes ``~/.ouroboros/config.yaml`` with ``orchestrator.runtime_backend =
    kiro`` / ``llm.backend = kiro`` and registers the MCP server in
    ``~/.kiro/settings/mcp.json`` so the user's very next
    ``kiro-cli chat`` session can invoke ``ooo <skill>`` prefixes without
    hand-editing any config file.
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
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Kiro setup.")
        return

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

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Kiro runtime (CLI: {kiro_path})")
    print_info(f"Config saved to: {config_path}")

    _register_kiro_mcp_server()
    _install_runtime_instruction_artifact("kiro")


def _register_copilot_mcp_server() -> None:
    """Register or refresh the Ouroboros MCP entry in ~/.copilot/mcp-config.json.

    Copilot CLI loads MCP servers from ``~/.copilot/mcp-config.json``. We add
    the Ouroboros server (built from whichever package install method we
    detect) and set the env so the MCP child uses the Copilot backend.
    """
    mcp_path = Path.home() / ".copilot" / "mcp-config.json"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            print_warning("~/.copilot/mcp-config.json is not valid JSON — leaving it untouched.")
            return

    if not isinstance(data, dict):
        print_warning("~/.copilot/mcp-config.json top-level is not an object — skipping.")
        return

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    detected = _detect_mcp_entry(package_spec="ouroboros-ai[mcp]")
    if detected is None:
        print_warning(
            "Cannot register MCP server: no working ouroboros installation found.\n"
            "Install with one of:\n"
            "  pipx install 'ouroboros-ai[mcp]'\n"
            "  uv tool install 'ouroboros-ai[mcp]'\n"
            "  pip install 'ouroboros-ai[mcp]'"
        )
        return

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
        _KNOWN_COMMANDS = {"uvx", "ouroboros", "python3", "python", "uv"}
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
        mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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


def _is_shipped_default_roster(
    current: list | tuple,
    shipped_roster: tuple[str, ...],
) -> bool:
    """True when ``current`` is a shipped default roster (current or legacy).

    Element-wise match against ``recognized_shipped_defaults`` so a roster
    persisted before a pin bump (e.g. the old OpenRouter Opus slug in the
    consensus slot) is still recognized as an untouched shipped default.
    """
    current_tuple = tuple(current)
    if len(current_tuple) != len(shipped_roster):
        return False
    return all(
        str(candidate) in recognized_shipped_defaults(default)
        for candidate, default in zip(current_tuple, shipped_roster, strict=True)
    )


def _setup_copilot(copilot_path: str, *, non_interactive: bool = False) -> None:
    """Configure Ouroboros for the GitHub Copilot CLI runtime.

    Writes ``~/.ouroboros/config.yaml`` with ``orchestrator.runtime_backend =
    copilot`` / ``llm.backend = copilot`` and persists the user's chosen
    default model after live-discovering the Copilot model catalog. Also
    registers the MCP server in ``~/.copilot/mcp-config.json``.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir
    from ouroboros.copilot.model_discovery import (
        list_copilot_models,
        used_fallback,
    )

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting Copilot setup.")
        return

    # Live-discover available Copilot models. Falls back silently to a
    # bundled snapshot when the GitHub API is unreachable or unauthenticated.
    models = list_copilot_models(refresh=True)
    if used_fallback():
        print_warning(
            "Could not reach the GitHub Copilot models API — using a bundled "
            "fallback list. Run `gh auth login` and re-run setup to refresh."
        )
    if not models:
        print_error("No Copilot models available; cannot pick a default.")
        return

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
        return

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured Copilot runtime (CLI: {copilot_path})")
    print_info(f"Default model: {chosen_model}")
    print_info(f"Config saved to: {config_path}")

    _register_copilot_mcp_server()
    _install_runtime_instruction_artifact("copilot")


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
    default_command = json.dumps(command)
    default_args = json.dumps(args)
    return f"""/**
 * Ouroboros ooo bridge for Pi.
 *
 * Managed by `ouroboros setup --runtime pi`.
 * Routes exact-prefix `ooo ...` inputs from interactive Pi into Ouroboros'
 * shared skill dispatcher instead of sending them to the model as chat.
 */
import type {{ ExtensionAPI, ExtensionContext }} from "@earendil-works/pi-coding-agent";

const COMMAND_RE = /^\\s*ooo(?:\\s+|$)/i;
const UNSUPPORTED_DISPATCH_EXIT_CODE = 78;
const TIMEOUT_MS = Number(process.env.OUROBOROS_PI_BRIDGE_TIMEOUT_MS || 6 * 60 * 60 * 1000);
const DEFAULT_COMMAND = {default_command};
const DEFAULT_ARGS = {default_args};

function ouroborosEntry(): {{ command: string; args: string[] }} {{
  if (process.env.OUROBOROS_CLI) return {{ command: process.env.OUROBOROS_CLI, args: [] }};
  return {{ command: DEFAULT_COMMAND, args: DEFAULT_ARGS }};
}}

function outputText(stdout: string, stderr: string): string {{
  const out = stdout.trim();
  const err = stderr.trim();
  if (out && err) return `${{out}}\\n\\n${{err}}`;
  return out || err || "(no output)";
}}

async function dispatch(pi: ExtensionAPI, text: string, ctx: ExtensionContext): Promise<boolean> {{
  ctx.ui.notify(`Ouroboros dispatch: ${{text}}`, "info");
  const entry = ouroborosEntry();
  const result = await pi.exec(
    entry.command,
    [...entry.args, "dispatch", "--runtime", "pi", "--cwd", ctx.cwd, text],
    {{ cwd: ctx.cwd, timeout: TIMEOUT_MS }},
  );
  if (result.code === UNSUPPORTED_DISPATCH_EXIT_CODE) {{
    ctx.ui.notify(`Ouroboros did not claim command; continuing in Pi`, "info");
    return false;
  }}
  const body = outputText(result.stdout || "", result.stderr || "");
  pi.sendMessage({{
    customType: "ouroboros",
    content: body,
    display: true,
    details: {{ command: text, exitCode: result.code }},
  }});
  if (result.code !== 0) {{
    ctx.ui.notify(`Ouroboros dispatch failed (${{result.code ?? "unknown"}})`, "error");
  }}
  return true;
}}

export default function ouroborosBridge(pi: ExtensionAPI) {{
  pi.registerCommand("ooo", {{
    description: "Dispatch an Ouroboros ooo command",
    handler: async (args, ctx) => {{
      const text = `ooo ${{args}}`.trim();
      await dispatch(pi, text, ctx);
    }},
  }});

  pi.on("input", async (event, ctx) => {{
    if (event.source === "extension") {{
      return {{ action: "continue" }};
    }}
    if (!COMMAND_RE.test(event.text)) {{
      return {{ action: "continue" }};
    }}
    const handled = await dispatch(pi, event.text.trim(), ctx);
    return {{ action: handled ? "handled" : "continue" }};
  }});
}}
"""


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
    """Return the launcher a managed GJC bridge should use for this install."""
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
    """Return the packaged managed GJC bridge extension source."""
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
    """Install the managed GJC extension that routes interactive ``ooo`` input."""
    import hashlib

    from ouroboros.runtime_instruction_artifacts import gjc_agent_dir

    dest = gjc_agent_dir() / "extensions" / _GJC_OOO_BRIDGE_SUBDIR / _GJC_OOO_BRIDGE_FILENAME
    content = _gjc_bridge_source_text()
    if content is None:
        print_warning("Could not locate packaged GJC ooo bridge source.")
        return False

    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing_hash: str | None = None
    if dest.exists():
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None

    if existing_hash == new_hash:
        print_info(f"GJC ooo bridge already up to date: {dest}")
        return True

    try:
        _atomic_write_text(dest, content)
    except OSError as exc:
        print_warning(f"Could not install GJC ooo bridge at {dest}: {exc}")
        return False

    print_success(
        f"{'Updated' if existing_hash is not None else 'Installed'} GJC ooo bridge: {dest}"
    )
    print_info("Restart GJC or run /reload in an existing GJC session to load the bridge.")
    return True


def _setup_gjc(gjc_path: str) -> None:
    """Configure Ouroboros for the GJC CLI runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting GJC setup.")
        return

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

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured GJC runtime (CLI: {gjc_path})")
    print_info(f"Config saved to: {config_path}")
    _install_runtime_instruction_artifact("gjc")
    _install_gjc_ooo_bridge()


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


def _setup_runtime_only_backend(backend: str, cli_path: str, cli_config_key: str) -> None:
    """Configure runtime setup for Antigravity, Grok, or Zcode.

    This setup path intentionally sets only ``orchestrator.runtime_backend`` and
    the CLI path. Antigravity and Grok are runtime-only; Zcode also supports
    explicit LLM-completion selection, but setup does not silently move
    ``llm.backend`` because that changes authoring/evaluation traffic.
    No setup-owned instruction artifact is installed yet — a documented gap in
    ``docs/runtime-guides/skill-capability-guides.md``.
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
    orch[cli_config_key] = cli_path

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured {backend} runtime (CLI: {cli_path})")
    print_info(f"Config saved to: {config_path}")


def _setup_antigravity(antigravity_path: str) -> None:
    """Configure Ouroboros for the Antigravity CLI (``agy``) runtime."""
    _setup_runtime_only_backend("antigravity", antigravity_path, "antigravity_cli_path")


def _setup_grok(grok_path: str) -> None:
    """Configure Ouroboros for the Grok Build CLI (``grok``) runtime."""
    _setup_runtime_only_backend("grok", grok_path, "grok_cli_path")


def _setup_zcode(zcode_path: str) -> None:
    """Configure Ouroboros for the Zcode CLI runtime."""
    _setup_runtime_only_backend("zcode", zcode_path, "zcode_cli_path")


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


def _setup_claude(claude_path: str) -> None:
    """Configure Ouroboros for the Claude Code runtime."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text()) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text()) or {}

    # Set runtime and LLM backend to claude
    config_dict.setdefault("orchestrator", {})
    config_dict["orchestrator"]["runtime_backend"] = "claude"
    config_dict["orchestrator"]["cli_path"] = claude_path

    config_dict.setdefault("llm", {})
    config_dict["llm"]["backend"] = "claude"

    with config_path.open("w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    # Register/fix MCP server in ~/.claude/mcp.json
    _ensure_claude_mcp_entry()

    print_success(f"Configured Claude Code runtime (CLI: {claude_path})")
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
            "Install with: pip install ouroboros-ai[all]"
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
        _KNOWN_COMMANDS = {"ouroboros", "python3", "python", "uvx", "uv"}
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
        return {"command": ["uvx", "--from", "ouroboros-ai[all]", "ouroboros", "mcp", "serve"]}
    if shutil.which("ouroboros"):
        return {"command": ["ouroboros", "mcp", "serve"]}
    # Check if ouroboros is importable via python
    import subprocess

    python_path = shutil.which("python3") or shutil.which("python")
    if python_path:
        try:
            subprocess.run(
                [python_path, "-c", "import ouroboros"],
                capture_output=True,
                timeout=10,
                check=True,
            )
            return {"command": [python_path, "-m", "ouroboros", "mcp", "serve"]}
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pass
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


def _atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write *content* to *path* atomically — temp file + ``os.replace``.

    Readers always see either the pre-existing file or the final content —
    never a truncated partial.  Caller is expected to have created
    ``path.parent`` already.  Raises :class:`OSError` on failure; callers
    decide how to surface that.
    """
    import os
    import tempfile

    if path.is_symlink():
        raise OSError(f"Refusing to replace symlink: {path}")

    try:
        if path.stat().st_nlink > 1:
            raise OSError(f"Refusing to replace hard-linked file: {path}")
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        if os.name == "posix":
            os.fchmod(fd, mode)
        else:
            os.chmod(tmp_name, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except OSError:
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
            help="Runtime backend to configure (claude, codex, opencode, hermes, gemini, goose, kiro, copilot, pi, gjc, antigravity, grok, zcode).",
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
            help="Codex MCP config mode: auto preserves user-managed entries, preserve skips MCP changes, stdio replaces with the managed stdio entry.",
        ),
    ] = "auto",
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
    [dim]    ouroboros setup scan               # scan brownfield repos[/dim]
    [dim]    ouroboros setup list               # list brownfield repos[/dim]
    [dim]    ouroboros setup default            # toggle default repos[/dim]
    """
    if ctx.invoked_subcommand is not None:
        return

    console.print("\n[bold cyan]Ouroboros Setup[/bold cyan]\n")

    # Show current backend if already configured
    current_backend = _get_current_backend()
    if current_backend:
        console.print(f"[bold]Current backend:[/bold] [cyan]{current_backend}[/cyan]")
        console.print()

    # Detect available runtimes
    detected = _detect_runtimes()
    available = {k: v for k, v in detected.items() if v is not None}

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
        _setup_claude(claude_path)
    elif selected in ("codex", "codex_cli"):
        codex_path = available.get("codex")
        if not codex_path:
            print_error("Codex CLI not found in PATH.")
            raise typer.Exit(1)
        _setup_codex(codex_path, mcp_mode=_normalize_codex_mcp_mode(mcp_mode))
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
        _setup_hermes(hermes_path)
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
        _setup_kiro(kiro_path)
    elif selected in ("copilot", "copilot_cli"):
        copilot_path = available.get("copilot")
        if not copilot_path:
            print_error(
                "GitHub Copilot CLI not found.\n"
                "Install it from https://docs.github.com/copilot/github-copilot-in-the-cli, set "
                "OUROBOROS_COPILOT_CLI_PATH, or configure orchestrator.copilot_cli_path."
            )
            raise typer.Exit(1)
        _setup_copilot(copilot_path, non_interactive=non_interactive)
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
        _setup_gjc(gjc_path)
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
    else:
        print_error(f"Unsupported runtime: {selected}")
        raise typer.Exit(1)

    console.print("\n[bold green]Setup complete![/bold green]")
    console.print("\n[dim]Next steps:[/dim]")
    console.print('  ouroboros init start "your idea here"')
    console.print("  ouroboros run workflow seed.yaml\n")


# ── Artifact refresh subcommand ──────────────────────────────────


def _opencode_bridge_dest() -> Path:
    plugin_dir = opencode_config_dir()
    for part in _BRIDGE_PLUGIN_SUBDIR:
        plugin_dir = plugin_dir / part
    return plugin_dir / _BRIDGE_PLUGIN_FILENAME


@app.command("refresh")
def refresh_artifacts() -> None:
    """Refresh installed Ouroboros artifacts for every detected runtime.

    Rewrites rules, skills, bridges, and instruction guides that a previous
    setup already installed — without changing MCP registrations, the runtime
    selection, or ~/.ouroboros/config.yaml. Codex follows ``ouroboros codex
    refresh`` semantics and refreshes whenever Codex is present; every other
    artifact refreshes only when it already exists, so a deliberately removed
    integration (e.g. OpenCode subprocess mode) is never resurrected.
    """
    from ouroboros.hermes.artifacts import HERMES_SKILL_CATEGORY, HERMES_SKILL_NAME
    from ouroboros.runtime_instruction_artifacts import (
        copilot_instruction_path,
        gemini_instruction_path,
        gjc_agent_dir,
        gjc_instruction_path,
        has_managed_section,
        kiro_instruction_path,
        opencode_instruction_path,
    )

    refreshed: list[str] = []

    codex_dir = Path.home() / ".codex"
    if codex_dir.exists() or shutil.which("codex"):
        from ouroboros.codex import install_codex_artifacts

        try:
            result = install_codex_artifacts(codex_dir=codex_dir, prune=False)
        except OSError as exc:
            # One runtime failing must not leave the remaining ones stale.
            print_warning(f"Could not refresh Codex artifacts: {exc}")
        else:
            print_success(f"Installed Codex rules → {result.rules_path}")
            print_success(
                f"Installed {len(result.skill_paths)} Codex skills → {codex_dir / 'skills'}"
            )
            refreshed.append("codex")

    hermes_skill_dir = Path.home() / ".hermes" / "skills" / HERMES_SKILL_CATEGORY
    if (hermes_skill_dir / HERMES_SKILL_NAME).exists():
        try:
            _install_hermes_artifacts()
        except OSError as exc:
            print_warning(f"Could not refresh Hermes artifacts: {exc}")
        else:
            refreshed.append("hermes")

    opencode_dir = opencode_config_dir()
    opencode_touched = False
    if _opencode_bridge_dest().exists():
        opencode_touched = _install_opencode_bridge_plugin()
    if has_managed_section(opencode_instruction_path(opencode_dir)):
        _install_runtime_instruction_artifact("opencode", config_dir=opencode_dir)
        opencode_touched = True
    if opencode_touched:
        refreshed.append("opencode")

    if has_managed_section(gemini_instruction_path()):
        _install_runtime_instruction_artifact("gemini")
        refreshed.append("gemini")

    if kiro_instruction_path().exists():
        _install_runtime_instruction_artifact("kiro")
        refreshed.append("kiro")

    if copilot_instruction_path().exists():
        _install_runtime_instruction_artifact("copilot")
        refreshed.append("copilot")

    pi_bridge = Path.home() / ".pi" / "agent" / "extensions" / _PI_OOO_BRIDGE_FILENAME
    if pi_bridge.exists():
        if _install_pi_ooo_bridge():
            refreshed.append("pi")

    gjc_touched = False
    gjc_bridge = gjc_agent_dir() / "extensions" / _GJC_OOO_BRIDGE_SUBDIR / _GJC_OOO_BRIDGE_FILENAME
    if gjc_bridge.exists():
        gjc_touched = _install_gjc_ooo_bridge()
    if gjc_instruction_path().exists():
        _install_runtime_instruction_artifact("gjc")
        gjc_touched = True
    if gjc_touched:
        refreshed.append("gjc")

    if refreshed:
        print_success(f"Refreshed runtime artifacts: {', '.join(refreshed)}")
    else:
        print_info("No installed runtime artifacts found to refresh.")


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
