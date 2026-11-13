"""Read-only client catalog for the Connect page.

Each card answers three questions for one agent tool: is it installed, is
Ouroboros wired into it, and is it the agent Ouroboros runs work on. Nothing
here writes a file: connecting goes through ``ouroboros setup`` (see
:mod:`ouroboros.gui.jobs`), the same path the CLI and the ``ooo setup`` skill use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import tomllib
from typing import Any

import yaml

_DOCS_BASE = "https://github.com/Q00/ouroboros/blob/main/docs/runtime-guides/"
_CACHE_TTL_SEC = 8.0


@dataclass(frozen=True, slots=True)
class ClientSpec:
    """Static facts about one agent tool Ouroboros can work with."""

    id: str
    label: str
    vendor: str
    blurb: str
    docs: str
    # How Ouroboros reaches into this tool once it is set up.
    wiring: str
    # ``runtime`` tools only run Ouroboros work; ``host`` tools can also call
    # the ``ooo`` tools from their own sessions.
    role: str = "host"
    setup_note: str = ""


CLIENTS: tuple[ClientSpec, ...] = (
    ClientSpec(
        id="claude",
        label="Claude Code",
        vendor="Anthropic",
        blurb="Ouroboros ships as a Claude Code plugin with skills, hooks and the MCP server.",
        docs="claude-code.md",
        wiring="Claude plugin",
        setup_note="Install the plugin with `claude plugin install ouroboros@ouroboros`.",
    ),
    ClientSpec(
        id="codex",
        label="Codex CLI",
        vendor="OpenAI",
        blurb="Registers the Ouroboros MCP server, rules and skills in your Codex home.",
        docs="codex.md",
        wiring="~/.codex/config.toml",
    ),
    ClientSpec(
        id="opencode",
        label="OpenCode",
        vendor="SST",
        blurb="Installs the bridge plugin so OpenCode sessions can run ooo commands.",
        docs="opencode.md",
        wiring="opencode.json plugin",
    ),
    ClientSpec(
        id="hermes",
        label="Hermes",
        vendor="Nous Research",
        blurb="Adds the Ouroboros MCP server and skill to your Hermes config.",
        docs="hermes.md",
        wiring="~/.hermes/config.yaml",
    ),
    ClientSpec(
        id="kiro",
        label="Kiro CLI",
        vendor="AWS",
        blurb="Adds the Ouroboros MCP server to Kiro's MCP settings.",
        docs="kiro.md",
        wiring="~/.kiro/settings/mcp.json",
    ),
    ClientSpec(
        id="copilot",
        label="GitHub Copilot CLI",
        vendor="GitHub",
        blurb="Adds the Ouroboros MCP server to Copilot's MCP config.",
        docs="copilot.md",
        wiring="~/.copilot/mcp-config.json",
    ),
    ClientSpec(
        id="gemini",
        label="Gemini CLI",
        vendor="Google",
        blurb="Runs Ouroboros work on Gemini and uses it for interview and evaluation calls.",
        docs="gemini.md",
        wiring="Runtime only",
        role="runtime",
    ),
    ClientSpec(
        id="goose",
        label="Goose",
        vendor="Block",
        blurb="Runs Ouroboros work through the Goose CLI.",
        docs="goose.md",
        wiring="Runtime only",
        role="runtime",
    ),
    ClientSpec(
        id="pi",
        label="Pi",
        vendor="Pi",
        blurb="Installs an extension so Pi sessions can route ooo commands to Ouroboros.",
        docs="pi.md",
        wiring="~/.pi/agent/extensions",
    ),
    ClientSpec(
        id="omp",
        label="Oh My Pi",
        vendor="OMP",
        blurb="Installs the ooo bridge extension for Oh My Pi.",
        docs="omp.md",
        wiring="~/.omp/agent/extensions",
    ),
    ClientSpec(
        id="gjc",
        label="GJC",
        vendor="GJC",
        blurb="Installs the ooo bridge so GJC sessions can call Ouroboros.",
        docs="gjc.md",
        wiring="GJC extensions",
    ),
    ClientSpec(
        id="zcode",
        label="Zcode",
        vendor="Zhipu",
        blurb="Runs Ouroboros work on Zcode; the bridge plugin comes from Zcode's plugin store.",
        docs="zcode.md",
        wiring="Zcode plugin",
    ),
    ClientSpec(
        id="antigravity",
        label="Antigravity",
        vendor="Google",
        blurb="Runs Ouroboros work through the agy CLI.",
        docs="antigravity.md",
        wiring="Runtime only",
        role="runtime",
    ),
    ClientSpec(
        id="grok",
        label="Grok Build",
        vendor="xAI",
        blurb="Runs Ouroboros work through the grok CLI.",
        docs="grok.md",
        wiring="Runtime only",
        role="runtime",
    ),
)

CLIENT_IDS = frozenset(spec.id for spec in CLIENTS)


@dataclass(slots=True)
class Integration:
    """How far Ouroboros is wired into one tool."""

    state: str  # connected | partial | missing | runtime
    detail: str
    path: str | None = None
    # Stable id for the page's translated sentence; ``vars`` fill its placeholders.
    code: str = ""
    vars: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SignIn:
    """Whether the tool's own CLI reports a signed-in account."""

    state: str  # signed_in | signed_out | unknown
    detail: str = ""
    fix: str | None = None


@dataclass(slots=True)
class ClientCard:
    """One Connect-page card, ready to serialize."""

    id: str
    label: str
    vendor: str
    blurb: str
    docs: str
    wiring: str
    role: str
    setup_note: str
    installed: bool
    cli_path: str | None
    integration: Integration
    is_default: bool
    stages: list[str] = field(default_factory=list)
    sign_in: SignIn | None = None


def _home() -> Path:
    return Path.home()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return None


def _has_server(data: dict[str, Any] | None, section: str) -> bool:
    if not data:
        return False
    servers = data.get(section)
    return isinstance(servers, dict) and isinstance(servers.get("ouroboros"), dict)


def _json_mcp(path: Path, section: str = "mcpServers") -> Integration:
    if not path.exists():
        return Integration("missing", "No MCP config yet", str(path), "mcp_config_missing")
    if _has_server(_read_json(path), section):
        return Integration(
            "connected", "Ouroboros MCP server registered", str(path), "mcp_registered"
        )
    return Integration(
        "missing", "Ouroboros is not in this MCP config", str(path), "mcp_not_registered"
    )


def _detect_claude() -> Integration:
    from ouroboros.cli.commands.doctor import collect_install_surfaces

    surfaces = {s.name: s for s in collect_install_surfaces()}
    launcher = surfaces.get("claude_plugin_launcher")
    plugin = surfaces.get("claude_plugin_marketplace")
    if launcher and launcher.status == "pass":
        version = plugin.version if plugin else None
        detail = f"Plugin {version} installed" if version else "Plugin installed"
        return Integration(
            "connected", detail, launcher.path, "plugin_installed", {"version": version or ""}
        )
    if plugin and plugin.status == "pass":
        return Integration(
            "partial",
            "Plugin found but its MCP launcher is missing",
            plugin.path,
            "plugin_no_launcher",
        )
    path = plugin.path if plugin else None
    return Integration("missing", "Ouroboros plugin not installed", path, "plugin_missing")


def _detect_codex() -> Integration:
    codex_home = Path(os.environ.get("CODEX_HOME") or _home() / ".codex").expanduser()
    config = codex_home / "config.toml"
    data = _read_toml(config) if config.exists() else None
    skills = codex_home / "skills"
    has_skills = any(skills.glob("ouroboros*")) if skills.is_dir() else False
    if _has_server(data, "mcp_servers"):
        if has_skills:
            return Integration(
                "connected", "MCP server and skills installed", str(config), "mcp_and_skills"
            )
        return Integration(
            "partial", "MCP server registered; skills missing", str(config), "mcp_no_skills"
        )
    if has_skills:
        return Integration(
            "partial", "Skills installed; MCP server missing", str(config), "skills_no_mcp"
        )
    return Integration(
        "missing", "Ouroboros MCP server not in config.toml", str(config), "codex_mcp_missing"
    )


def _detect_opencode() -> Integration:
    from ouroboros.cli.opencode_config import find_opencode_config, is_bridge_plugin_entry

    try:
        config = find_opencode_config(allow_default=False)
    except Exception:
        config = None
    if config is None:
        return Integration("missing", "No OpenCode config found", code="opencode_no_config")
    try:
        from ouroboros.cli.jsonc import parse_jsonc

        data = parse_jsonc(config.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if isinstance(data, dict):
        plugins = data.get("plugin") or []
        if isinstance(plugins, list) and any(is_bridge_plugin_entry(p) for p in plugins):
            return Integration(
                "connected", "Bridge plugin registered", str(config), "bridge_registered"
            )
        if _has_server(data, "mcp"):
            return Integration(
                "partial", "MCP registered without the bridge plugin", str(config), "mcp_no_bridge"
            )
    return Integration(
        "missing", "Bridge plugin not registered", str(config), "bridge_not_registered"
    )


def _detect_hermes() -> Integration:
    path = _home() / ".hermes" / "config.yaml"
    if _has_server(_read_yaml(path), "mcp_servers"):
        return Integration(
            "connected", "Ouroboros MCP server registered", str(path), "mcp_registered"
        )
    return Integration(
        "missing", "Ouroboros is not in Hermes' MCP servers", str(path), "mcp_not_registered"
    )


def _bridge(path: Path, label: str) -> Integration:
    if path.is_file():
        return Integration("connected", f"{label} installed", str(path), "bridge_installed")
    return Integration("missing", f"{label} not installed", str(path), "bridge_missing")


def _detect_gjc() -> Integration:
    try:
        from ouroboros.cli.commands.setup import _GJC_OOO_BRIDGE_FILENAME, _GJC_OOO_BRIDGE_SUBDIR
        from ouroboros.runtime_instruction_artifacts import gjc_agent_dir

        dest = gjc_agent_dir() / "extensions" / _GJC_OOO_BRIDGE_SUBDIR / _GJC_OOO_BRIDGE_FILENAME
    except Exception:
        return Integration("missing", "Bridge location unknown", code="bridge_unknown")
    return _bridge(dest, "ooo bridge")


def _detect_zcode() -> Integration:
    from ouroboros.cli.commands.doctor import collect_install_surfaces

    surfaces = {s.name: s for s in collect_install_surfaces()}
    launcher = surfaces.get("zcode_bridge_launcher")
    if launcher and launcher.status == "pass":
        return Integration(
            "connected", "Bridge plugin installed", launcher.path, "bridge_installed"
        )
    path = launcher.path if launcher else None
    return Integration("missing", "Bridge plugin not installed", path, "bridge_missing")


def _runtime_only() -> Integration:
    return Integration(
        "runtime", "Works as a runtime; nothing to install in the tool", code="runtime_only"
    )


_DETECTORS: dict[str, Callable[[], Integration]] = {
    "claude": _detect_claude,
    "codex": _detect_codex,
    "opencode": _detect_opencode,
    "hermes": _detect_hermes,
    "kiro": lambda: _json_mcp(_home() / ".kiro" / "settings" / "mcp.json"),
    "copilot": lambda: _json_mcp(_home() / ".copilot" / "mcp-config.json"),
    "gemini": _runtime_only,
    "goose": _runtime_only,
    "pi": lambda: _bridge(
        _home() / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts", "ooo extension"
    ),
    "omp": lambda: _bridge(
        _home() / ".omp" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts", "ooo bridge"
    ),
    "gjc": _detect_gjc,
    "zcode": _detect_zcode,
    "antigravity": _runtime_only,
    "grok": _runtime_only,
}


def _run_quiet(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv
            argv, capture_output=True, text=True, timeout=4, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _sign_in(client_id: str, cli_path: str | None) -> SignIn | None:
    """Ask the CLI itself; only tools with a status command are checked."""
    if not cli_path:
        return None
    if client_id == "claude":
        result = _run_quiet([cli_path, "auth", "status"])
        if result is None:
            return SignIn("unknown")
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return SignIn("unknown")
        if data.get("loggedIn"):
            return SignIn("signed_in", str(data.get("authMethod") or ""))
        return SignIn("signed_out", fix="claude auth login")
    if client_id == "codex":
        result = _run_quiet([cli_path, "login", "status"])
        if result is None:
            return SignIn("unknown")
        text = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0 and "logged in" in text.lower():
            return SignIn("signed_in", text.splitlines()[0] if text else "")
        return SignIn("signed_out", text.splitlines()[0] if text else "", fix="codex login")
    return None


def _runtime_assignment() -> tuple[str | None, dict[str, str]]:
    """Return the default runtime and explicit per-stage runtimes from config."""
    from ouroboros.config_tui.persistence import load_raw_config

    try:
        data = load_raw_config()
    except Exception:
        return None, {}
    orchestrator = data.get("orchestrator") if isinstance(data, dict) else None
    if not isinstance(orchestrator, dict):
        return None, {}
    default = orchestrator.get("runtime_backend")
    stages: dict[str, str] = {}
    profile = orchestrator.get("runtime_profile")
    if isinstance(profile, dict) and isinstance(profile.get("stages"), dict):
        for stage, backend in profile["stages"].items():
            if isinstance(backend, str) and backend:
                stages[str(stage)] = _canonical(backend)
    return (_canonical(default) if isinstance(default, str) else None), stages


def _canonical(name: str) -> str:
    from ouroboros.backends import resolve_backend_alias

    try:
        resolved = resolve_backend_alias(name)
    except ValueError:
        return name
    return "claude" if resolved.startswith("claude") else resolved


def _detect_installed() -> dict[str, str | None]:
    from ouroboros.cli.commands.setup import _detect_runtimes

    try:
        return _detect_runtimes()
    except Exception:
        return {}


def build_cards() -> list[ClientCard]:
    """Inspect every known tool and return its card (read-only)."""
    installed = _detect_installed()
    default, stages = _runtime_assignment()
    cards: list[ClientCard] = []
    for spec in CLIENTS:
        try:
            integration = _DETECTORS[spec.id]()
        except Exception as exc:  # one broken config must not blank the page
            integration = Integration("missing", f"Could not inspect: {exc}")
        cli_path = installed.get(spec.id)
        cards.append(
            ClientCard(
                id=spec.id,
                label=spec.label,
                vendor=spec.vendor,
                blurb=spec.blurb,
                docs=_DOCS_BASE + spec.docs,
                wiring=spec.wiring,
                role=spec.role,
                setup_note=spec.setup_note,
                installed=cli_path is not None,
                cli_path=cli_path,
                integration=integration,
                is_default=default == spec.id,
                stages=sorted(stage for stage, backend in stages.items() if backend == spec.id),
                sign_in=_sign_in(spec.id, cli_path),
            )
        )
    return cards


_cache_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


def list_clients(*, fresh: bool = False) -> list[dict[str, Any]]:
    """Serialized cards, cached briefly so page switches stay instant."""
    global _cache
    with _cache_lock:
        if not fresh and _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_SEC:
            return _cache[1]
    cards = [asdict(card) for card in build_cards()]
    with _cache_lock:
        _cache = (time.monotonic(), cards)
    return cards


def invalidate() -> None:
    """Drop the cache after a setup or settings change."""
    global _cache
    with _cache_lock:
        _cache = None


__all__ = ["CLIENTS", "CLIENT_IDS", "ClientCard", "Integration", "invalidate", "list_clients"]
