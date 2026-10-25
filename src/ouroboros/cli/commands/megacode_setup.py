"""Megacode harness setup for ``ouroboros setup --runtime megacode``.

Split out of :mod:`ouroboros.cli.commands.setup` to respect the module-size
ratchet. Everything this setup writes lives in the harness's own config
folder (``harness paths`` → ``CONFIG``):

- ``config.toml`` gets one managed block holding the Ouroboros MCP server, the
  copied skills root, and the two modes the megacode runtime and LLM adapter
  run in. Text outside the block is never touched.
- ``ouroboros/skills/`` holds a copy of the packaged skills, each in a folder
  named by its frontmatter ``name`` (the harness skips a skill whose folder
  and ``name`` differ).
- ``AGENTS.md`` gets one managed block that routes ``ooo <command>`` to a skill,
  standing in for the keyword hook the harness does not have.

The MCP server is launched with ``--runtime megacode --llm-backend megacode``,
so harness sessions run Ouroboros on the harness while ``~/.ouroboros``
config — shared with every other host — is left as it is.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

_BLOCK_BEGIN = "# >>> ouroboros: managed by `ouroboros setup --runtime megacode` >>>"
_BLOCK_END = "# <<< ouroboros <<<"
_AGENTS_BEGIN = "<!-- >>> ouroboros: managed by `ouroboros setup --runtime megacode` >>> -->"
_AGENTS_END = "<!-- <<< ouroboros <<< -->"
_SKILLS_SUBDIR = Path("ouroboros") / "skills"
_MCP_STARTUP_TIMEOUT_SECONDS = 120

# Worker turns may edit and run commands but must not re-enter Ouroboros
# (``mcp``/``skill``) or spawn harness sub-agents; LLM completions only read.
_WORKER_TOOLS = ("read_file", "write_file", "edit_file", "bash", "glob", "search", "exec")
_LLM_TOOLS = ("read_file", "glob", "search")

_ROUTES = (
    ("`ooo` (bare)", "ooo"),
    ("`ooo auto ...`", "auto"),
    ("`ooo interview ...`", "interview"),
    ("`ooo seed ...`", "seed"),
    ("`ooo run ...` / `ooo execute ...`", "ouroboros-run"),
    ("`ooo eval ...` / `ooo evaluate ...`", "evaluate"),
    ("`ooo evolve ...`", "evolve"),
    ("`ooo ralph ...`", "ralph"),
    ("`ooo status ...` / `ooo drift ...`", "ouroboros-status"),
    ("`ooo unstuck` / `ooo stuck` / `ooo lateral`", "unstuck"),
    ("`ooo qa ...`", "qa"),
    ("`ooo pm ...` / `ooo prd ...`", "pm"),
    ("`ooo cancel ...`", "cancel"),
    ("`ooo brownfield ...`", "brownfield"),
    ("`ooo publish ...`", "publish"),
    ("`ooo resume-session`", "resume-session"),
    ("`ooo update`", "update"),
    ("`ooo help`", "ouroboros-help"),
    ("`ooo config`", "ouroboros-config"),
)


def detect_megacode_runtime() -> str | None:
    """Resolve the harness CLI: explicit env/config path first, then PATH."""
    from ouroboros.config import get_megacode_cli_path

    return get_megacode_cli_path() or shutil.which("harness")


def resolve_harness_config_dir(harness_path: str) -> Path | None:
    """Return the harness ``CONFIG`` folder as ``harness paths`` reports it."""
    try:
        completed = subprocess.run(
            [harness_path, "paths"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "CONFIG":
            return Path(parts[1])
    return None


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _mode_names() -> tuple[str, str]:
    """Return the worker and LLM mode names (imported lazily: heavy modules)."""
    from ouroboros.orchestrator.megacode_runtime import MEGACODE_WORKER_MODE
    from ouroboros.providers.megacode_llm_adapter import MEGACODE_LLM_MODE

    return MEGACODE_WORKER_MODE, MEGACODE_LLM_MODE


def render_harness_block(*, skills_root: Path, mcp_command: list[str]) -> str:
    """Render the managed ``config.toml`` block."""
    worker_mode, llm_mode = _mode_names()
    return "\n".join(
        (
            _BLOCK_BEGIN,
            "[skills]",
            f"extra_roots = {_toml_array([str(skills_root)])}",
            "",
            f"[modes.{worker_mode}]",
            f"tools = {_toml_array(_WORKER_TOOLS)}",
            "ask = []",
            "",
            f"[modes.{llm_mode}]",
            f"tools = {_toml_array(_LLM_TOOLS)}",
            "ask = []",
            "",
            "[mcp.servers.ouroboros]",
            f"command = {_toml_array(mcp_command)}",
            "sandbox = false",
            "network = true",
            f"startup_timeout = {_MCP_STARTUP_TIMEOUT_SECONDS}",
            _BLOCK_END,
        )
    )


def render_agents_block() -> str:
    """Render the managed ``AGENTS.md`` routing block."""
    rows = "\n".join(f"| {command} | load the `{skill}` skill |" for command, skill in _ROUTES)
    return "\n".join(
        (
            _AGENTS_BEGIN,
            "## Ouroboros",
            "",
            "A message that starts with `ooo` (or `/ooo`) is an Ouroboros command.",
            "Do not answer it yourself and do not implement anything first: load the",
            "skill below and follow it.",
            "",
            "| Input | Action |",
            "|-------|--------|",
            rows,
            "",
            "Skills that mention `ask_user`, `run_shell` or `inspect_code` mean this",
            "harness's own question, shell and file tools. Every `ouroboros_*` tool",
            "comes from the `ouroboros` MCP server.",
            _AGENTS_END,
        )
    )


def strip_managed_block(text: str, begin: str, end: str) -> str:
    """Return ``text`` without its managed block (and the block's separating blank line)."""
    start = text.find(begin)
    if start == -1:
        return text
    stop = text.find(end, start)
    if stop == -1:
        return text
    stop += len(end)
    head = text[:start].rstrip("\n")
    tail = text[stop:].lstrip("\n")
    if head and tail:
        return f"{head}\n\n{tail}"
    return f"{head}\n" if head else tail


def merge_managed_block(text: str, block: str, begin: str, end: str) -> str:
    """Replace the managed block in place, or append it after the user's text."""
    start = text.find(begin)
    if start != -1:
        stop = text.find(end, start)
        if stop != -1:
            return text[:start] + block + text[stop + len(end) :]
    head = text.rstrip("\n")
    return f"{head}\n\n{block}\n" if head else f"{block}\n"


def find_config_conflicts(user_text: str) -> list[str]:
    """Return the tables the managed block would redefine in the user's own config."""
    data = tomllib.loads(user_text)
    conflicts: list[str] = []
    if "skills" in data:
        conflicts.append("[skills]")
    modes = data.get("modes")
    if isinstance(modes, dict):
        conflicts.extend(f"[modes.{name}]" for name in _mode_names() if name in modes)
    servers = (data.get("mcp") or {}).get("servers") if isinstance(data.get("mcp"), dict) else None
    if isinstance(servers, dict) and "ouroboros" in servers:
        conflicts.append("[mcp.servers.ouroboros]")
    return conflicts


def install_megacode_skills(config_dir: Path, *, skills_dir: str | Path | None = None) -> Path:
    """Copy the packaged skills under ``<config_dir>/ouroboros/skills`` and return that root.

    Each skill lands in a folder named by its frontmatter ``name``, and
    ``../<old folder>/`` links between skills follow the rename. The managed
    root is replaced wholesale, so a skill removed upstream disappears too.
    """
    from ouroboros.router.dispatch import load_skill_frontmatter
    from ouroboros.skills.artifacts import (
        SKILL_ENTRYPOINT,
        collect_skill_bundle_dirs,
        resolve_packaged_skills_dir,
    )

    target_root = config_dir / _SKILLS_SUBDIR
    target_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".skills-", dir=target_root.parent))
    try:
        renamed: dict[str, str] = {}
        with resolve_packaged_skills_dir(skills_dir=skills_dir, anchor_file=__file__) as source:
            for bundle in collect_skill_bundle_dirs(source):
                name = load_skill_frontmatter(bundle / SKILL_ENTRYPOINT).get("name")
                folder = name.strip() if isinstance(name, str) and name.strip() else bundle.name
                shutil.copytree(bundle, staging_root / folder)
                if folder != bundle.name:
                    renamed[bundle.name] = folder
        for skill_md in staging_root.glob(f"*/{SKILL_ENTRYPOINT}"):
            text = skill_md.read_text(encoding="utf-8")
            updated = text
            for old, new in renamed.items():
                updated = updated.replace(f"../{old}/", f"../{new}/")
            if updated != text:
                skill_md.write_text(updated, encoding="utf-8")
        if target_root.exists():
            shutil.rmtree(target_root)
        staging_root.rename(target_root)
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return target_root


def setup_megacode(
    harness_path: str,
    *,
    mcp_entry: dict[str, object] | None = None,
    skills_dir: str | Path | None = None,
) -> bool:
    """Wire Ouroboros into the megacode harness. Returns False on failure."""
    # Call-scoped seam: setup.py imports this module at import time.
    from ouroboros.cli.commands.setup import _atomic_write_text, _detect_mcp_entry

    config_dir = resolve_harness_config_dir(harness_path)
    if config_dir is None:
        print_error(f"Could not read the harness config folder from `{harness_path} paths`.")
        return False

    entry = mcp_entry if mcp_entry is not None else _detect_mcp_entry()
    if entry is None:
        print_error(
            "Neither uvx nor pipx was found; install uv to launch the Ouroboros MCP server."
        )
        return False
    raw_args = entry.get("args")
    args = [str(arg) for arg in raw_args] if isinstance(raw_args, list) else []
    mcp_command = [str(entry["command"]), *args, "--runtime", "megacode"]
    mcp_command += ["--llm-backend", "megacode"]

    config_path = config_dir / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    user_text = strip_managed_block(existing, _BLOCK_BEGIN, _BLOCK_END)
    try:
        conflicts = find_config_conflicts(user_text)
    except tomllib.TOMLDecodeError as exc:
        print_error(f"{config_path} is not valid TOML ({exc}); fix it and rerun setup.")
        return False
    if conflicts:
        print_error(
            f"{config_path} already defines {', '.join(conflicts)}. Remove "
            "those tables (setup manages them in one block) and rerun setup."
        )
        return False

    skills_root = install_megacode_skills(config_dir, skills_dir=skills_dir)
    block = render_harness_block(skills_root=skills_root, mcp_command=mcp_command)
    new_config = merge_managed_block(existing, block, _BLOCK_BEGIN, _BLOCK_END)
    tomllib.loads(new_config)
    _atomic_write_text(config_path, new_config, mode=0o600)

    agents_path = config_dir / "AGENTS.md"
    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    agents_block = render_agents_block()
    _atomic_write_text(
        agents_path, merge_managed_block(agents_text, agents_block, _AGENTS_BEGIN, _AGENTS_END)
    )

    print_success(f"Configured the megacode harness (CLI: {harness_path})")
    print_info(f"Harness config: {config_path}")
    print_info(f"Skills: {skills_root}")
    print_info(f"ooo routing: {agents_path}")
    print_info(
        "Harness sessions run Ouroboros on the harness itself; ~/.ouroboros/config.yaml "
        "is unchanged. Rerun this setup after upgrading Ouroboros to refresh the skills."
    )
    return True


def select_megacode_runtime(available: dict[str, str | None]) -> None:
    """Handle ``ouroboros setup --runtime megacode``."""
    harness_path = available.get("megacode")
    if not harness_path:
        print_error(
            "Megacode harness CLI (harness) not found.\n"
            "Install it from https://megacode-harness.vercel.app/docs/, set "
            "OUROBOROS_MEGACODE_CLI_PATH, or configure orchestrator.megacode_cli_path."
        )
        raise typer.Exit(1)
    if not setup_megacode(harness_path):
        raise typer.Exit(1)


__all__ = [
    "detect_megacode_runtime",
    "find_config_conflicts",
    "install_megacode_skills",
    "merge_managed_block",
    "render_agents_block",
    "render_harness_block",
    "resolve_harness_config_dir",
    "select_megacode_runtime",
    "setup_megacode",
    "strip_managed_block",
]
