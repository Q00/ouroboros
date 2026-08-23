"""Unit tests for shared packaged skill resolution helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from ouroboros.skills.artifacts import resolve_packaged_skills_dir

# Current built-in and bundled commands from https://code.claude.com/docs/en/commands.
# Keep aliases in this inventory too: Claude resolves them through the same command
# palette, so a skill alias can shadow a host command just as a primary name can.
_CLAUDE_RESERVED_SKILL_NAMES = frozenset(
    {
        "add-dir",
        "agents",
        "allowed-tools",
        "android",
        "app",
        "background",
        "bashes",
        "batch",
        "bg",
        "branch",
        "btw",
        "bug",
        "cd",
        "checkpoint",
        "checkup",
        "chrome",
        "clear",
        "code-review",
        "compact",
        "config",
        "context",
        "continue",
        "cost",
        "debug",
        "design-login",
        "design-sync",
        "desktop",
        "diff",
        "doctor",
        "effort",
        "exit",
        "extra-usage",
        "fast",
        "feedback",
        "fewer-permission-prompts",
        "focus",
        "fork",
        "heapdump",
        "help",
        "hooks",
        "ide",
        "init",
        "insights",
        "install-github-app",
        "install-slack-app",
        "ios",
        "keybindings",
        "login",
        "logout",
        "mcp",
        "memory",
        "mobile",
        "model",
        "new",
        "passes",
        "permissions",
        "plan",
        "powerup",
        "privacy-settings",
        "proactive",
        "quit",
        "radio",
        "rc",
        "recap",
        "release-notes",
        "reload-plugins",
        "reload-skills",
        "remote-control",
        "remote-env",
        "reset",
        "resume",
        "review",
        "rewind",
        "routines",
        "run",
        "run-skill-generator",
        "sandbox",
        "schedule",
        "scroll-speed",
        "security-review",
        "settings",
        "setup-bedrock",
        "setup-vertex",
        "share",
        "simplify",
        "skills",
        "stats",
        "status",
        "statusline",
        "stickers",
        "stop",
        "subtask",
        "tasks",
        "team-onboarding",
        "teleport",
        "terminal-setup",
        "theme",
        "tp",
        "ultrareview",
        "undo",
        "upgrade",
        "usage",
        "usage-credits",
        "verify",
        "vim",
        "web-setup",
        "workflows",
    }
)
_SKILL_ALIAS_FIELDS = ("alias", "aliases", "command_aliases", "skill_aliases", "commands")
_PYTHON_SKILL_PATHS = tuple(
    Path(root) / skill / "SKILL.md"
    for root in ("skills", ".claude-plugin/skills")
    for skill in ("welcome", "setup", "seed")
)
_PYTHON_RESOLVER_START = "<!-- ouroboros-python-resolver:start -->"
_PYTHON_RESOLVER_END = "<!-- ouroboros-python-resolver:end -->"
_PYTHON_PATH_SHAPING_ENV = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
)


def _python_resolver_code(skill_path: Path | None = None) -> str:
    """Extract the executable resolver contract from one packaged skill."""
    if skill_path is None:
        skill_path = Path(__file__).resolve().parents[3] / _PYTHON_SKILL_PATHS[0]
    contents = skill_path.read_text(encoding="utf-8")
    start = contents.index(_PYTHON_RESOLVER_START) + len(_PYTHON_RESOLVER_START)
    end = contents.index(_PYTHON_RESOLVER_END, start)
    fenced = contents[start:end].strip()
    assert fenced.startswith("```bash\n") and fenced.endswith("\n```")
    return fenced.removeprefix("```bash\n").removesuffix("\n```")


def _bash_block_containing(skill_path: Path, needle: str) -> str:
    contents = skill_path.read_text(encoding="utf-8")
    needle_at = contents.index(needle)
    start = contents.rfind("```bash\n", 0, needle_at)
    assert start >= 0
    start += len("```bash\n")
    closing_fence = re.search(r"\n[ \t]*```(?:\n|$)", contents[needle_at:])
    assert closing_fence is not None
    end = needle_at + closing_fence.start()
    return contents[start:end]


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _install_direct_python(path: Path, label: str) -> None:
    _write_executable(
        path,
        f'#!/bin/sh\nprintf \'%s\\n\' \'{label}\' >> "$CALL_LOG"\nexec "$REAL_PYTHON" "$@"\n',
    )


def _install_old_python(path: Path, label: str) -> None:
    _write_executable(
        path,
        f"#!/bin/sh\nprintf '%s\\n' '{label}' >> \"$CALL_LOG\"\nexit 1\n",
    )


def _install_uv_fallback(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
printf '%s\n' "$@" >> "$UV_LOG"
[ "$1" = run ] || exit 91
[ "$2" = --no-project ] || exit 92
[ "$3" = --quiet ] || exit 93
[ "$4" = --python ] || exit 94
[ "$5" = '>=3.12' ] || exit 95
[ "$6" = python ] || exit 96
shift 6
exec "$REAL_PYTHON" "$@"
""",
    )


def _run_resolver(
    script: str,
    *,
    path: Path,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(path.parent),
        "PATH": str(path),
        "REAL_PYTHON": sys.executable,
        "CALL_LOG": str(path.parent / "python-calls.log"),
        "UV_LOG": str(path.parent / "uv-calls.log"),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["/bin/bash", "-c", f"{_python_resolver_code()}\n{script}"],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _poisoned_python_env(tmp_path: Path) -> dict[str, str]:
    """Return inherited values that each make a real CPython child unusable."""
    stale_stdlib = tmp_path / "python3.11-stdlib"
    encodings = stale_stdlib / "encodings"
    encodings.mkdir(parents=True, exist_ok=True)
    (encodings / "__init__.py").write_text(
        "raise RuntimeError('stale Python 3.11 stdlib loaded')\n",
        encoding="utf-8",
    )
    return {
        "PYTHONHOME": str(tmp_path / "missing-python-home"),
        "PYTHONPATH": str(stale_stdlib),
        "PYTHONPLATLIBDIR": "lib64",
        "PYTHONEXECUTABLE": str(tmp_path / "poison-python-executable"),
        "__PYVENV_LAUNCHER__": str(tmp_path / "poison-venv-launcher"),
    }


def _skill_frontmatter(skill_path: Path) -> dict[str, object]:
    """Read the YAML metadata Claude Code uses to register one skill."""
    contents = skill_path.read_text(encoding="utf-8")
    _, frontmatter, _ = contents.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict), f"invalid skill frontmatter: {skill_path}"
    return parsed


def _claimed_skill_names(frontmatter: dict[str, object]) -> set[str]:
    """Return primary and alias names that can enter Claude's command palette."""
    claimed = {str(frontmatter.get("name", "")).strip().lower()}
    for field in _SKILL_ALIAS_FIELDS:
        raw = frontmatter.get(field)
        if isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, list | tuple):
            values = raw
        else:
            values = ()
        claimed.update(str(value).strip().lower() for value in values)
    return {name for name in claimed if name}


def test_codex_plugin_manifest_starts_a_codex_composed_mcp_server() -> None:
    """A fresh Codex plugin session must not compose its handlers as Claude.

    First-use setup runs *inside* this MCP process, so it cannot retrofit the
    runtime selected while the server was composed.  The packaged descriptor
    therefore owns the Codex runtime and LLM backend from process startup.
    """
    repo_root = Path(__file__).resolve().parents[3]
    marketplace_path = repo_root / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    manifest_path = repo_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert marketplace["name"] == "ouroboros"
    assert marketplace["plugins"] == [
        {
            "name": "ouroboros",
            "source": {"source": "local", "path": "."},
            "policy": {"installation": "AVAILABLE"},
            "category": "Developer Tools",
        }
    ]
    assert manifest["name"] == "ouroboros"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.codex.json"
    assert manifest["interface"]["displayName"] == "Ouroboros"
    codex_mcp = json.loads((repo_root / ".mcp.codex.json").read_text(encoding="utf-8"))
    # The shipped descriptor pins the served package to the plugin manifest
    # version (#2066) so a plugin update changes the uvx cache key.
    assert codex_mcp["mcpServers"]["ouroboros"] == {
        "command": "uvx",
        "args": [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            f"ouroboros-ai[mcp]=={manifest['version']}",
            "ouroboros",
            "mcp",
            "serve",
            "--runtime",
            "codex",
            "--llm-backend",
            "codex",
        ],
    }
    assert (repo_root / "skills" / "config" / "SKILL.md").is_file()
    assert (repo_root / "skills" / "ooo" / "SKILL.md").is_file()
    assert (repo_root / ".claude-plugin" / "skills" / "config" / "SKILL.md").is_file()


def test_fanout_synthesis_fetch_contract_is_shipped_to_every_host() -> None:
    """Every MCP-only host must be able to consume a completed fan-out."""
    repo_root = Path(__file__).resolve().parents[3]

    for skill_root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        skill_path = skill_root / "interview" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        assert "A complete set returns a bounded artifact envelope" in content, skill_path
        assert "`ouroboros_fetch_artifact` with its `contract_id`" in content, skill_path
        assert "correlated synthesis in the fetched `body`" in content, skill_path
        assert "Continue the interview from the fetched synthesis" in content, skill_path
        assert "Continue the interview from the returned synthesis" not in content, skill_path


def test_shipped_skill_metadata_never_claims_claude_reserved_command_names() -> None:
    """Primary names and aliases must leave Claude's built-in commands reachable."""
    repo_root = Path(__file__).resolve().parents[3]
    expected_primary_names = {
        "config": "ouroboros-config",
        "help": "ouroboros-help",
        "run": "ouroboros-run",
        "status": "ouroboros-status",
    }

    for skill_root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill_path in sorted(skill_root.glob("*/SKILL.md")):
            frontmatter = _skill_frontmatter(skill_path)
            claimed = _claimed_skill_names(frontmatter)
            collisions = claimed & _CLAUDE_RESERVED_SKILL_NAMES
            assert not collisions, (
                f"{skill_path} claims Claude Code reserved names: {sorted(collisions)}"
            )

        for directory, expected_name in expected_primary_names.items():
            frontmatter = _skill_frontmatter(skill_root / directory / "SKILL.md")
            assert frontmatter["name"] == expected_name

        status_frontmatter = _skill_frontmatter(skill_root / "status" / "SKILL.md")
        assert status_frontmatter["aliases"] == ["drift"]


def test_first_use_onboarding_has_host_specific_model_settings_handoffs() -> None:
    """Codex and Claude package only the first-use wording for their own host."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_required_phrases = (
        "Setup Gate: First Use",
        "CODEX_SETUP_REQUIRED",
        "mcp_servers\\.ouroboros",
        "ouroboros setup --runtime codex",
        "uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' "
        "ouroboros setup --runtime codex",
        "설정하고 시작하기",
        "직접 모델 설정하기",
        "모델은 언제든 나중에 바꿀 수 있어요",
        "Use Codex default model",
        "Enter another model ID",
        "../config/SKILL.md",
        "temporary `localhost` address",
        "A previously completed welcome must never hide the setup gate",
        "LEGACY_CODEX_MODEL_MIGRATION_REQUIRED",
        "Codex 선택으로 전환하기 (권장)",
        "gpt-5 고정 유지하기",
        "codexModelMigration",
    )
    codex_welcome = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in codex_required_phrases:
        assert phrase in codex_welcome, f"Codex skills: missing first-use handoff `{phrase}`"

    codex_entry = (repo_root / "skills" / "ooo" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: ooo" in codex_entry
    assert "../welcome/SKILL.md" in codex_entry

    claude_welcome = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Setup Gate: First Use" in claude_welcome
    assert "../setup/SKILL.md" in claude_welcome
    assert "previously completed welcome must never hide the setup gate" in claude_welcome
    assert "runtime_backend: claude" in claude_welcome
    assert "marketplace plugin owns its MCP capability" in claude_welcome
    assert 'python3 - "$HOME/.ouroboros/config.yaml" "$HOME/.claude/mcp.json"' not in claude_welcome
    for codex_only_phrase in (
        "CODEX_SETUP_REQUIRED",
        "LEGACY_CODEX_MODEL_MIGRATION_REQUIRED",
        "Use Codex default model",
        "Codex 선택으로 전환하기",
        "gpt-5 고정 유지하기",
    ):
        assert codex_only_phrase not in claude_welcome

    claude_setup = (repo_root / ".claude-plugin" / "skills" / "setup" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    claude_config = (repo_root / ".claude-plugin" / "skills" / "config" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Codex" not in claude_setup
    assert "Codex" not in claude_config
    assert "execution.default_model" in claude_config

    codex_rules = (repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md").read_text(
        encoding="utf-8"
    )
    assert "### First use in Codex" in codex_rules
    assert "직접 모델 설정하기" in codex_rules
    assert "mere existence" in codex_rules

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        setup = (root / "setup" / "SKILL.md").read_text(encoding="utf-8")
        assert "### Step 5.1: Model Choice (Claude Code)" in setup
        assert "직접 모델 설정하기" in setup
        assert "ooo config" in setup

    assert "execution.default_model" in (repo_root / "skills" / "config" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _run_setup_gate(
    script: str,
    *,
    home: Path,
    codex_home: Path | str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run the packaged setup gate exactly as a host executes its Markdown snippet."""
    python_bin = home / ".test-python-bin"
    python_bin.mkdir(exist_ok=True)
    python3 = python_bin / "python3"
    if not python3.exists():
        python3.symlink_to(sys.executable)
    env = {
        "HOME": str(home),
        "PATH": str(python_bin),
        "OUROBOROS_CODEX_APP_CLI_PATH": str(home / "missing-app-codex"),
    }
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    if extra_env is not None:
        if "PATH" in extra_env:
            extra_env = {**extra_env, "PATH": f"{python_bin}:{extra_env['PATH']}"}
        env.update(extra_env)
    return subprocess.run(
        ["/bin/bash", "-c", f"{_python_resolver_code()}\n{script}"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def test_codex_setup_gate_accepts_reordered_yaml_and_quoted_toml_mcp_key(tmp_path: Path) -> None:
    """The first-use gate reads structures, not nearby lines or one TOML spelling."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        f"""orchestrator:\n  retries: 3\n  timeout: 20\n  runtime_backend: codex\n  codex_cli_path: {sys.executable}\nllm:\n  qa_model: gpt-5\n  backend: codex\n""",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        f"""[mcp_servers]\n\"ouroboros\" = {{ command = \"{sys.executable}\" }}\n""",
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_READY"


def test_codex_setup_gate_accepts_yaml_flow_mappings(tmp_path: Path) -> None:
    """Valid YAML flow mappings should not force completed Codex users through setup."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        f"orchestrator: {{runtime_backend: codex, codex_cli_path: {sys.executable}}}\nllm: {{backend: codex}}\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ouroboros]\ncommand = "{sys.executable}"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_READY"


def test_codex_setup_gate_rejects_unavailable_mcp_command(tmp_path: Path) -> None:
    """A configured but missing MCP launcher must force setup repair."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        f"orchestrator: {{runtime_backend: codex, codex_cli_path: {sys.executable}}}\n"
        "llm: {backend: codex}\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[mcp_servers.ouroboros]\ncommand = "/definitely/missing/uvx"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == ("CODEX_SETUP_REQUIRED")


def test_codex_setup_gate_expands_tilde_codex_home(tmp_path: Path) -> None:
    """CODEX_HOME may use shell-style ~/ paths and still point at the active config."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "codex-alt"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        f"orchestrator: {{runtime_backend: codex, codex_cli_path: {sys.executable}}}\nllm: {{backend: codex}}\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ouroboros]\ncommand = "{sys.executable}"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home="~/codex-alt") == "CODEX_READY"


def test_codex_setup_gate_rejects_stale_configured_cli_path(tmp_path: Path) -> None:
    """A stale explicit Codex CLI override must not be reported as ready."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        "orchestrator:\n"
        "  runtime_backend: codex\n"
        "  codex_cli_path: /definitely/missing/codex\n"
        "llm:\n"
        "  backend: codex\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ouroboros]\ncommand = "{sys.executable}"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_SETUP_REQUIRED"


def test_codex_setup_gate_rejects_missing_default_cli_candidate(tmp_path: Path) -> None:
    """Config plus MCP is not ready when no Codex executable candidate exists."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_SETUP_REQUIRED"


def test_codex_setup_gate_rejects_app_only_default_cli_candidate(tmp_path: Path) -> None:
    """The gate must match runtime launch semantics: no implicit App fallback."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    app_cli = tmp_path / "ChatGPT.app" / "Contents" / "Resources" / "codex"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    app_cli.parent.mkdir(parents=True)
    app_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    app_cli.chmod(0o755)
    config_path.write_text(
        "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[mcp_servers.ouroboros]\ncommand = "ouroboros"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert (
        _run_setup_gate(
            gate,
            home=tmp_path,
            codex_home=codex_home,
            extra_env={
                "PATH": str(tmp_path / "empty-bin"),
                "OUROBOROS_CODEX_APP_CLI_PATH": str(app_cli),
            },
        )
        == "CODEX_SETUP_REQUIRED"
    )


def test_codex_completed_welcome_precheck_accepts_yaml_flow_mappings(
    tmp_path: Path,
) -> None:
    """The completed-welcome shortcut must use the same YAML shape as the setup gate."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    (tmp_path / ".ouroboros" / "prefs.json").write_text(
        '{"welcomeCompleted": "2026-07-27"}\n',
        encoding="utf-8",
    )
    codex_home.mkdir()
    config_path.write_text(
        f"orchestrator: {{runtime_backend: codex, codex_cli_path: {sys.executable}}}\nllm: {{backend: codex}}\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ouroboros]\ncommand = "{sys.executable}"\n',
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    precheck_context = skill.index("Before honoring that completion marker")
    start = skill.index("CODEX_HOME_DIR=", precheck_context)
    precheck = skill[start : skill.index("\n```", start)] + '\nprintf "$CODEX_READY"\n'

    assert _run_setup_gate(precheck, home=tmp_path, codex_home=codex_home) == "true"


def test_codex_setup_gate_rejects_empty_mcp_server_mapping(tmp_path: Path) -> None:
    """The gate must not bypass setup for an unusable empty MCP entry."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text("[mcp_servers.ouroboros]\n", encoding="utf-8")
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_SETUP_REQUIRED"


def test_codex_setup_gate_rejects_blank_mcp_endpoint_values(tmp_path: Path) -> None:
    """A present but blank command/url is not a usable Codex MCP endpoint."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index("CODEX_HOME_DIR=", setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    for toml in (
        '[mcp_servers.ouroboros]\ncommand = ""\n',
        '[mcp_servers.ouroboros]\nurl = "   "\n',
        '[mcp_servers]\n"ouroboros" = { command = "" }\n',
    ):
        (codex_home / "config.toml").write_text(toml, encoding="utf-8")
        assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_SETUP_REQUIRED"


def test_python_resolver_contract_is_identical_in_all_six_packaged_skill_sources() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    contracts = {
        relative_path: _python_resolver_code(repo_root / relative_path)
        for relative_path in _PYTHON_SKILL_PATHS
    }

    assert len(set(contracts.values())) == 1, contracts
    contract = next(iter(contracts.values()))
    assert "sys.version_info < (3, 12)" in contract
    assert 'command python3 "$@"' in contract
    assert 'command python "$@"' in contract
    assert "uv run --no-project --quiet --python '>=3.12' python \"$@\"" in contract
    clean_env = "unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__"
    assert contract.count(clean_env) == 5
    assert f"({clean_env}; command python3 -c" in contract
    assert f'({clean_env}; command python3 "$@")' in contract
    assert f"({clean_env}; command python -c" in contract
    assert f'({clean_env}; command python "$@")' in contract
    assert f"({clean_env}; command uv run" in contract
    assert "PYTHONUSERBASE" not in contract

    for relative_path in _PYTHON_SKILL_PATHS:
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        without_contract = contents.replace(
            f"{_PYTHON_RESOLVER_START}\n```bash\n{contract}\n```\n{_PYTHON_RESOLVER_END}",
            "",
        )
        assert "OUROBOROS_WELCOME_PYTHON" not in contents
        assert not re.search(r"(?m)^\s*(?:if\s+)?python3(?:\s|$)", without_contract), (
            f"{relative_path}: direct python3 invocation bypasses the shared resolver"
        )

    plugin_manifest = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin_manifest["skills"] == "./skills/"
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"skills" = "ouroboros/skills"' in pyproject


def test_python_resolver_prefers_compatible_python3_and_preserves_arguments_and_stdin(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_direct_python(bin_dir / "python3", "python3")
    _install_direct_python(bin_dir / "python", "python")
    _install_uv_fallback(bin_dir / "uv")

    result = _run_resolver(
        """ouroboros_python -c 'import json, sys; print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))' 'argument with spaces' 'quote"value' '*' <<'DATA'
stdin payload
DATA
""",
        path=bin_dir,
    )

    assert json.loads(result.stdout) == {
        "argv": ["argument with spaces", 'quote"value', "*"],
        "stdin": "stdin payload\n",
    }
    assert (tmp_path / "python-calls.log").read_text().splitlines() == ["python3", "python3"]
    assert not (tmp_path / "uv-calls.log").exists()


@pytest.mark.parametrize(
    ("selected_runtime", "expected_calls"),
    [
        ("python3", ["python3", "python3"]),
        ("python", ["old-python3", "python", "python"]),
        ("uv", ["old-python3", "old-python"]),
    ],
)
def test_python_resolver_sanitizes_path_shaping_env_for_every_runtime_path(
    tmp_path: Path,
    selected_runtime: str,
    expected_calls: list[str],
) -> None:
    """Probes and children are isolated while the caller keeps its environment."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if selected_runtime == "python3":
        _install_direct_python(bin_dir / "python3", "python3")
    elif selected_runtime == "python":
        _install_old_python(bin_dir / "python3", "old-python3")
        _install_direct_python(bin_dir / "python", "python")
    else:
        _install_old_python(bin_dir / "python3", "old-python3")
        _install_old_python(bin_dir / "python", "old-python")
        _install_uv_fallback(bin_dir / "uv")

    poisoned_env = _poisoned_python_env(tmp_path)

    result = _run_resolver(
        """ouroboros_python -c 'import json, os; print(json.dumps({key: os.environ.get(key) for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")}))'
printf 'caller-pythonhome=%s\n' "$PYTHONHOME"
printf 'caller-pythonpath=%s\n' "$PYTHONPATH"
printf 'caller-pythonplatlibdir=%s\n' "$PYTHONPLATLIBDIR"
printf 'caller-pythonexecutable=%s\n' "$PYTHONEXECUTABLE"
printf 'caller-pyvenv-launcher=%s\n' "$__PYVENV_LAUNCHER__"
""",
        path=bin_dir,
        extra_env=poisoned_env,
    )

    output_lines = result.stdout.splitlines()
    assert json.loads(output_lines[0]) == dict.fromkeys(_PYTHON_PATH_SHAPING_ENV)
    assert output_lines[1:] == [
        f"caller-pythonhome={poisoned_env['PYTHONHOME']}",
        f"caller-pythonpath={poisoned_env['PYTHONPATH']}",
        f"caller-pythonplatlibdir={poisoned_env['PYTHONPLATLIBDIR']}",
        f"caller-pythonexecutable={poisoned_env['PYTHONEXECUTABLE']}",
        f"caller-pyvenv-launcher={poisoned_env['__PYVENV_LAUNCHER__']}",
    ]
    assert (tmp_path / "python-calls.log").read_text().splitlines() == expected_calls
    if selected_runtime == "uv":
        assert "--python\n>=3.12\npython" in (tmp_path / "uv-calls.log").read_text()
    else:
        assert not (tmp_path / "uv-calls.log").exists()


@pytest.mark.parametrize(
    "variable",
    [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONPLATLIBDIR",
        pytest.param(
            "PYTHONEXECUTABLE",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS CPython variable"),
        ),
        pytest.param(
            "__PYVENV_LAUNCHER__",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS CPython variable"),
        ),
    ],
)
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available")
def test_path_shaping_poison_really_breaks_an_unsanitized_python(
    tmp_path: Path,
    variable: str,
) -> None:
    """Guard the regression matrix against harmless placeholder poison values."""
    poison = _poisoned_python_env(tmp_path)
    env = os.environ.copy()
    for key in _PYTHON_PATH_SHAPING_ENV:
        env.pop(key, None)
    env[variable] = poison[variable]
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--quiet",
            "--python",
            ">=3.12",
            "python",
            "-c",
            "print('unexpected-success')",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0, f"{variable} poison no longer exercises CPython startup"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available")
def test_python_resolver_real_uv_path_survives_all_path_shaping_poison(tmp_path: Path) -> None:
    """Exercise uv's managed interpreter, not the lightweight test double."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = shutil.which("uv")
    assert uv is not None
    (bin_dir / "uv").symlink_to(uv)

    result = _run_resolver(
        """ouroboros_python -c 'import json, os, sys; print(json.dumps({"env": {key: os.environ.get(key) for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")}, "version": list(sys.version_info[:2])}))'""",
        path=bin_dir,
        extra_env=_poisoned_python_env(tmp_path),
    )

    payload = json.loads(result.stdout)
    assert payload["env"] == dict.fromkeys(_PYTHON_PATH_SHAPING_ENV)
    assert tuple(payload["version"]) >= (3, 12)


def test_python_resolver_uses_compatible_python_when_python3_is_old(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_old_python(bin_dir / "python3", "old-python3")
    _install_direct_python(bin_dir / "python", "python")
    _install_uv_fallback(bin_dir / "uv")

    result = _run_resolver(
        "ouroboros_python -c 'import sys; print(sys.version_info[:2])'",
        path=bin_dir,
        extra_env={"PYTHONHOME": str(tmp_path / "missing-python-home")},
    )

    assert result.stdout.strip().startswith("(3, ")
    assert (tmp_path / "python-calls.log").read_text().splitlines() == [
        "old-python3",
        "python",
        "python",
    ]
    assert not (tmp_path / "uv-calls.log").exists()


def test_python_resolver_uses_uv_on_a_host_without_global_python(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_uv_fallback(bin_dir / "uv")

    result = _run_resolver(
        """ouroboros_python - 'space value' <<'PY'
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "pythonhome": os.environ.get("PYTHONHOME"), "value": "from-heredoc"}))
PY
""",
        path=bin_dir,
        extra_env={"PYTHONHOME": str(tmp_path / "missing-python-home")},
    )

    assert json.loads(result.stdout) == {
        "argv": ["space value"],
        "pythonhome": None,
        "value": "from-heredoc",
    }
    assert (tmp_path / "uv-calls.log").read_text().splitlines() == [
        "run",
        "--no-project",
        "--quiet",
        "--python",
        ">=3.12",
        "python",
        "-",
        "space value",
    ]


def test_python_resolver_rejects_old_global_interpreters_before_uv_fallback(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_old_python(bin_dir / "python3", "old-python3")
    _install_old_python(bin_dir / "python", "old-python")
    _install_uv_fallback(bin_dir / "uv")

    result = _run_resolver(
        "ouroboros_python -c 'import os; print(os.environ.get(\"PYTHONHOME\") is None)'",
        path=bin_dir,
        extra_env={"PYTHONHOME": str(tmp_path / "missing-python-home")},
    )

    assert result.stdout.strip() == "True"
    assert (tmp_path / "python-calls.log").read_text().splitlines() == [
        "old-python3",
        "old-python",
    ]
    assert "--python\n>=3.12\npython" in (tmp_path / "uv-calls.log").read_text()


def test_python_resolver_fails_cleanly_without_python_or_uv(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    result = _run_resolver(
        "ouroboros_python -c 'print(1)'",
        path=bin_dir,
        extra_env={"PYTHONHOME": str(tmp_path / "missing-python-home")},
        check=False,
    )

    assert result.returncode == 127
    assert result.stdout == ""
    assert "require Python >= 3.12 or uv on PATH" in result.stderr
    assert "Fatal Python error" not in result.stderr


@pytest.mark.parametrize("relative_path", _PYTHON_SKILL_PATHS)
def test_first_run_preference_write_works_through_uv_only_path(
    tmp_path: Path, relative_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    skill_path = repo_root / relative_path
    needle = (
        "    'welcomeShown': True,"
        if relative_path.parts[-2] == "welcome"
        else "prefs['star_asked'] = True"
    )
    snippet = _bash_block_containing(skill_path, needle)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_uv_fallback(bin_dir / "uv")

    _run_resolver(
        snippet,
        path=bin_dir,
        extra_env={
            "HOME": str(tmp_path),
            "PYTHONHOME": str(tmp_path / "missing-python-home"),
        },
    )

    prefs = json.loads((tmp_path / ".ouroboros" / "prefs.json").read_text(encoding="utf-8"))
    if relative_path.parts[-2] == "welcome":
        assert prefs["welcomeShown"] is True
        assert prefs["welcomeCompleted"]
        assert prefs["welcomeVersion"]
    else:
        assert prefs["star_asked"] is True


def test_codex_legacy_gpt5_migration_gate_targets_legacy_and_partial_migration(
    tmp_path: Path,
) -> None:
    """The gate should recover interrupted automatic migration, but not custom pins."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """clarification:\n  default_model: gpt-5\nexecution:\n  default_model: gpt-5\nevaluation:\n  semantic_model: gpt-5\nresilience:\n  reflect_model: gpt-5\n""",
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    migration_start = skill.index("### Legacy Codex Model Migration")
    start = skill.index('if ouroboros_python - "$HOME/.ouroboros/config.yaml"', migration_start)
    gate = skill[start : skill.index("\n```", start)]

    assert (
        _run_setup_gate(
            f'{gate}\nprintf "%s" "${{LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}}"', home=tmp_path
        )
        == "true"
    )

    config_path.write_text(
        """clarification:\n  default_model: default\nexecution:\n  default_model: gpt-5\nevaluation:\n  semantic_model: default\nresilience:\n  reflect_model: gpt-5\n""",
        encoding="utf-8",
    )
    assert (
        _run_setup_gate(
            f'{gate}\nprintf "%s" "${{LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}}"', home=tmp_path
        )
        == "true"
    )

    config_path.write_text(
        """clarification:\n  default_model: default\nexecution:\n  default_model: gpt-5\nevaluation:\n  semantic_model: custom-model\nresilience:\n  reflect_model: gpt-5\n""",
        encoding="utf-8",
    )
    assert (
        _run_setup_gate(
            f'{gate}\nprintf "%s" "${{LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}}"', home=tmp_path
        )
        == ""
    )

    prefs_path = tmp_path / ".ouroboros" / "prefs.json"
    prefs_path.write_text('{"codexModelMigration": "automatic-v1"}', encoding="utf-8")
    config_path.write_text(
        """clarification:\n  default_model: gpt-5\nexecution:\n  default_model: gpt-5\nevaluation:\n  semantic_model: gpt-5\nresilience:\n  reflect_model: gpt-5\n""",
        encoding="utf-8",
    )
    assert (
        _run_setup_gate(
            f'{gate}\nprintf "%s" "${{LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}}"', home=tmp_path
        )
        == ""
    )


def test_claude_setup_gate_accepts_default_sdk_without_host_mcp_file(tmp_path: Path) -> None:
    """Default SDK setup is ready without the host-owned Claude MCP file."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """llm:\n  qa_model: claude\n  backend: claude\norchestrator:\n  retries: 3\n  timeout: 20\n  runtime_backend: claude\n""",
        encoding="utf-8",
    )
    skill = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index('if ouroboros_python - "$HOME/.ouroboros/config.yaml"', setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path) == "SETUP_READY"


@pytest.mark.parametrize("runtime_backend", ["claude", "claude_mcp"])
def test_claude_setup_gate_accepts_yaml_flow_mappings_without_host_mcp_file(
    tmp_path: Path, runtime_backend: str
) -> None:
    """Both shipped Claude profiles are ready from durable config alone."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        f"orchestrator: {{runtime_backend: {runtime_backend}}}\nllm: {{backend: claude}}\n",
        encoding="utf-8",
    )
    skill = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index('if ouroboros_python - "$HOME/.ouroboros/config.yaml"', setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path) == "SETUP_READY"


def test_claude_completed_welcome_precheck_is_idempotent_without_host_mcp_file(
    tmp_path: Path,
) -> None:
    """A completed SDK setup does not reopen onboarding when mcp.json is absent."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    (tmp_path / ".ouroboros" / "prefs.json").write_text(
        '{"welcomeCompleted": "2026-08-09"}\n', encoding="utf-8"
    )
    config_path.write_text(
        "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
        encoding="utf-8",
    )
    skill = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    precheck_context = skill.index("Before honoring that completion marker")
    start = skill.index('if ouroboros_python - "$HOME/.ouroboros/config.yaml"', precheck_context)
    gate = skill[start : skill.index("\n```", start)] + '\nprintf "%s" "${SETUP_READY:-}"\n'

    assert _run_setup_gate(gate, home=tmp_path) == "true"


@pytest.mark.parametrize(
    "config",
    [
        "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: claude\n",
        "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: codex\n",
        "orchestrator:\n  runtime_backend: claude\n",
    ],
)
def test_claude_setup_gate_rejects_incomplete_runtime_config(tmp_path: Path, config: str) -> None:
    """Removing the MCP-file requirement does not accept unrelated config."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(config, encoding="utf-8")
    skill = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index('if ouroboros_python - "$HOME/.ouroboros/config.yaml"', setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path) == "SETUP_REQUIRED"


def test_welcome_surfaces_describe_default_claude_sdk_profile() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    expected = "Ordinary Claude setup uses the default `[claude]` Agent SDK profile on MCP 1.x."

    for relative_path in (
        Path("skills/welcome/SKILL.md"),
        Path(".claude-plugin/skills/welcome/SKILL.md"),
    ):
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert expected in content
        assert "Ordinary Claude setup uses the dependency-free CLI profile" not in content

    plugin_welcome = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert 'python3 - "$HOME/.ouroboros/config.yaml" "$HOME/.claude/mcp.json"' not in plugin_welcome


def test_resolve_packaged_skills_dir_falls_back_to_repo_root_bundle_when_package_is_stub(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Editable installs should skip package stubs that do not contain skill bundles."""
    package_stub_dir = tmp_path / "repo" / "src" / "ouroboros" / "skills"
    package_stub_dir.mkdir(parents=True)
    package_stub_dir.joinpath("__init__.py").write_text("# package stub\n", encoding="utf-8")

    repo_skills_dir = tmp_path / "repo" / "skills"
    run_skill_dir = repo_skills_dir / "run"
    run_skill_dir.mkdir(parents=True)
    run_skill_dir.joinpath("SKILL.md").write_text("---\nname: run\n---\n", encoding="utf-8")

    anchor_file = tmp_path / "repo" / "src" / "ouroboros" / "codex" / "artifacts.py"
    anchor_file.parent.mkdir(parents=True)
    anchor_file.write_text("# anchor\n", encoding="utf-8")

    monkeypatch.setattr(
        "ouroboros.skills.artifacts.importlib.resources.files",
        lambda _package: package_stub_dir,
    )

    with resolve_packaged_skills_dir(anchor_file=anchor_file) as resolved_dir:
        assert resolved_dir == repo_skills_dir


def test_multitool_deferred_schema_guards_name_each_discovery_query() -> None:
    """Multi-tool skill guards must not reuse the wrong deferred schema query."""
    repo_root = Path(__file__).resolve().parents[3]
    expected = {
        "brownfield": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "setup": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "seed": [
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros qa"', "ouroboros_qa"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "interview": [
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "evaluate": [
            ('"+ouroboros evaluate"', "ouroboros_start_evaluate"),
        ],
        "evolve": [
            ('"+ouroboros evolve"', "ouroboros_evolve_step"),
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "pm": [
            ('"+ouroboros pm_interview"', "ouroboros_pm_interview"),
        ],
        "run": [
            ('"+ouroboros execute"', "ouroboros_start_execute_seed"),
            ('"+ouroboros execute"', "ouroboros_job_wait"),
            ('"+ouroboros execute"', "ouroboros_job_result"),
            ('"+ouroboros execute"', "ouroboros_ac_tree_hud"),
            ('"+ouroboros session signal"', "ouroboros_session_signal_targets"),
            ('"+ouroboros session signal"', "ouroboros_session_signal"),
        ],
    }

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        assert "the same tool-discovery load query you used above" not in "\n".join(
            skill_path.read_text(encoding="utf-8") for skill_path in root.glob("*/SKILL.md")
        )
        for skill, pairs in expected.items():
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            assert "deferred-schema guard" in text
            for query, tool in pairs:
                assert query in text
                assert tool in text


def test_packaged_skills_gate_fallback_on_callability_not_empty_discovery() -> None:
    """No packaged skill may route to fallback/Path B on empty discovery alone.

    ``render_mcp_server_instructions()`` declares that an empty discovery result
    for an already-exposed tool is a no-op (not unavailability). Skill bodies
    must therefore gate fallback on whether the MCP tool is *callable*, not on
    whether discovery returned a match — otherwise direct-exposure or
    already-loaded runtimes skip a callable tool. This guards that contract for
    every packaged skill in both trees.
    """
    repo_root = Path(__file__).resolve().parents[3]
    # Bare "empty discovery -> fallback" routing that predated the server contract.
    forbidden = (
        "If not → proceed to **Path B**",
        "If not → skip to **Fallback**",
        "returns no matching tools → proceed to **Path B**",
    )
    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill_path in root.glob("*/SKILL.md"):
            text = skill_path.read_text(encoding="utf-8")
            for phrase in forbidden:
                assert phrase not in text, (
                    f"{skill_path}: bare empty-discovery fallback `{phrase}` — "
                    "gate on tool callability instead"
                )
            # Every deferred-schema-guard "no matching tool" fallback must carry
            # a callability qualifier (an empty load for an exposed tool is fine).
            if "no matching tool" in text:
                assert "not already callable" in text, (
                    f"{skill_path}: `no matching tool` fallback not gated on callability"
                )


def test_brownfield_default_selection_ends_turn_in_every_skill_bundle() -> None:
    """All shipped host bundles must render the repo list before selection."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("brownfield", "setup"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            compact = " ".join(text.split())
            assert "Do NOT use `AskUserQuestion` for this selection" in text
            assert "end the turn with the repo grid as the final message" in compact
            assert '"keep" to keep the current defaults' in compact
            assert "IMMEDIATELY after showing the list" not in text
            assert "Default repo selection — IMMEDIATELY" not in text


def test_brownfield_scan_contract_matches_runtime_in_every_skill_bundle() -> None:
    """All shipped host bundles must describe the depth-bounded, self-only scan.

    ``scan_home_for_repos()`` walks ``scan_root`` at most two levels deep and
    registers each candidate self-only — it never expands Git worktree families
    via ``git worktree list``. Skill instructions in both ``skills/`` and
    ``.claude-plugin/skills/`` must not advertise the retired outside-root
    worktree-expansion behavior.
    """
    repo_root = Path(__file__).resolve().parents[3]
    stale_phrases = (
        "even outside the scan root directory",
        "even when they live outside `scan_root`",
        "even if their paths are outside `scan_root`",
        "git worktree list --porcelain",
    )

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("brownfield", "setup"):
            skill_path = root / skill / "SKILL.md"
            text = skill_path.read_text(encoding="utf-8")
            compact = " ".join(text.split())
            for phrase in stale_phrases:
                assert phrase not in compact, (
                    f"{skill_path}: stale worktree-expansion scan contract `{phrase}` — "
                    "the runtime scan is depth-bounded and registers repos self-only"
                )
            assert "Git worktree families are not expanded" in compact.replace(
                "families are NOT expanded", "families are not expanded"
            ), f"{skill_path}: missing self-only scan contract statement"
            assert "two" in compact and "level" in compact, (
                f"{skill_path}: missing depth-bounded (two-level) scan description"
            )


def test_background_skills_delegate_one_exclusive_job_observer() -> None:
    """Background skills delegate polling while the parent relays child events."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto", "ralph"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(text.split())
            compact = " ".join(normalized.split())
            assert "response.meta.job_observer" in normalized
            assert "exactly one" in normalized
            assert "read-only" in normalized
            assert "main session must not poll the same job" in compact
            assert "attention_required" in normalized
            assert "isolated worktree" in compact
            assert "tui open" in normalized
            assert "conversation" in normalized and "available" in normalized
            assert "wait_agent" in text
            assert "cannot revive" in compact or "cannot wake" in compact
            assert "must not poll" in normalized
            assert "stop live observation" in compact
            assert "keep the durable job running" in compact
            assert "instead of waiting indefinitely" in compact

    for skill in ("run", "auto", "ralph"):
        text = (repo_root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        normalized = text.lower()
        assert "spawn_agent" in text
        assert "run_observer" in text
        assert "wait" in normalized and "not a spawn" in normalized
        assert "stdio" in normalized
        assert "detached worker" in normalized
        assert "survive" in normalized or "continues" in normalized

    codex_instructions = (repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md").read_text(
        encoding="utf-8"
    )
    assert "wait_agent" in codex_instructions
    assert "cannot revive a parent turn" in codex_instructions
    assert "stop live observation" in codex_instructions
    assert "without cancelling the durable job" in codex_instructions


def test_run_and_auto_route_human_intent_without_exposing_internal_ids() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(text.split())
            assert "ouroboros_session_signal_targets" in text
            assert "ouroboros_session_signal" in text
            assert "+ouroboros session signal" in text
            assert "never ask" in normalized and "id" in normalized
            assert "genuine" in normalized and ("tie" in normalized or "tied" in normalized)
            assert "omit `fallback_mode`" in compact


def test_active_conductor_skill_copies_cover_start_and_progress_briefing() -> None:
    """Every supported host gets the same user-facing control-surface facts."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(normalized.split())
            assert "efficient execution" in compact
            assert "quality-first execution" in compact
            assert "adaptive" in normalized and "quality_first" in normalized
            assert "observe" in normalized and "off" in normalized
            assert "strict" in normalized and "explicit" in normalized
            assert "run_configuration" in normalized
            assert "execution_plan" in normalized
            assert "discovery_summary" in normalized
            assert "parallel" in normalized
            assert "first scheduled ac" in compact
            assert "runtime" in normalized and "harness" in normalized


def test_run_skill_copies_preserve_automatic_model_tier_omission() -> None:
    """Normal run guidance must not silently turn omission into a medium-tier pin."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        text = (root / "run" / "SKILL.md").read_text(encoding="utf-8")
        compact = " ".join(text.lower().split())
        assert 'model_tier: "medium"' not in text
        assert "omit `model_tier` by default" in compact
        assert "only when the user explicitly requested a tier" in compact


def test_active_conductor_skill_copies_cover_synapse_and_audited_action_order() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto", "ralph"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            assert "ouroboros_session_signal_targets" in text
            assert "ouroboros_session_signal" in text
            assert 'mode="inform"' in text
            assert "queued" in normalized and "applied" in normalized
            assert "verify" in normalized
            assert "decide" in normalized
            assert "log" in normalized
            assert "act" in normalized
            assert "recommended_host_actions" in text
            assert "ouroboros_record_conductor_decision" in text


def test_active_conductor_guidance_is_english_canonical_without_locale_catalogs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    paths = [
        root / skill / "SKILL.md"
        for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills")
        for skill in ("run", "auto", "ralph")
    ] + [repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md"]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        normalized = text.lower()
        assert "english" in normalized and "canonical" in normalized
        assert "conversation language" in normalized
        assert "centralized" not in normalized
        assert "locale catalog" not in normalized
        assert "translation catalog" not in normalized
