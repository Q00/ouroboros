"""Test that dependencies are configured correctly."""

import json
from pathlib import Path
import tomllib

import pytest


def test_runtime_dependencies_configured():
    """Test that all required runtime dependencies are in pyproject.toml."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    deps = pyproject["project"]["dependencies"]
    # Extract dependency names, handling extras like sqlalchemy[asyncio]
    dep_names = {dep.split(">=")[0].split("==")[0].split("[")[0] for dep in deps}

    required_core_deps = [
        "typer",
        "pydantic",
        "structlog",
        "sqlalchemy",
        "aiosqlite",
        "rich",
        "pyyaml",
    ]

    for dep in required_core_deps:
        assert dep in dep_names, f"Required dependency '{dep}' not found in pyproject.toml"

    # Runtime-specific deps should be in optional extras, not core
    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})
    assert "claude" in optional_deps, "Missing 'claude' optional extra"
    assert "litellm" in optional_deps, "Missing 'litellm' optional extra"
    assert "dashboard" in optional_deps, "Missing 'dashboard' compatibility extra"
    assert "mcp" in optional_deps, "Missing 'mcp' optional extra"
    assert "tui" in optional_deps, "Missing 'tui' optional extra"
    assert "all" in optional_deps, "Missing 'all' optional extra"


def test_runtime_and_optional_dependencies_have_upper_bounds():
    """Dependencies must carry explicit upper bounds.

    Core runtime deps remain bounded *ranges* and must use ``<``. The optional
    AI/runtime extras are exact-pinned for supply-chain hardening (see the
    rationale in pyproject.toml); an exact pin (``==``) is the tightest
    possible upper bound, so ``==`` is accepted for optional extras only.
    """
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    # Core runtime deps must stay bounded ranges, never exact pins.
    runtime_deps = pyproject["project"]["dependencies"]
    for dep in runtime_deps:
        assert "<" in dep, f"Runtime dependency missing upper bound: {dep}"

    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})
    for extra_name in (
        "claude",
        "claude-cli",
        "claude-sdk",
        "litellm",
        "dashboard",
        "mcp",
        "tui",
    ):
        for dep in optional_deps[extra_name]:
            assert "<" in dep or "==" in dep, (
                f"Optional dependency '{extra_name}' missing upper bound: {dep}"
            )


def test_dev_dependencies_configured():
    """Test that dev dependencies are configured."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    # Check for dev dependencies in optional dependencies or dev group
    dev_deps = pyproject.get("dependency-groups", {}).get("dev", [])
    dep_names = {dep.split(">=")[0].split("==")[0].split("[")[0] for dep in dev_deps}

    required_dev_deps = ["pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy", "pre-commit"]

    for dep in required_dev_deps:
        assert dep in dep_names, f"Required dev dependency '{dep}' not found in pyproject.toml"


def test_dev_dependency_group_omits_litellm_extra():
    """Default dev installs do not pull LiteLLM."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    dev_deps = pyproject.get("dependency-groups", {}).get("dev", [])

    assert not any("ouroboros-ai[" in dep and "litellm" in dep for dep in dev_deps)


def test_litellm_test_dependency_group_uses_exact_pinned_public_extra():
    """LiteLLM test installs use a dedicated group wired through the public extra."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    dependency_groups = pyproject.get("dependency-groups", {})
    litellm_test_deps = dependency_groups.get("litellm-test", [])
    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})

    assert litellm_test_deps == ["ouroboros-ai[litellm]"]
    assert optional_deps["litellm"] == ["litellm==1.91.0; python_version < '3.14'"]


def test_litellm_test_dependency_group_excludes_python_314():
    """LiteLLM test group cannot be selected with Python 3.14."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    group_config = pyproject["tool"]["uv"]["dependency-groups"]["litellm-test"]

    assert group_config == {"requires-python": ">=3.12,<3.14"}


def test_litellm_public_extra_excludes_unsupported_python():
    """Public metadata must omit LiteLLM on Python 3.14 and newer."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})

    assert optional_deps["litellm"] == ["litellm==1.91.0; python_version < '3.14'"]
    assert any("litellm" in dep for dep in optional_deps["all"])
    assert all("python_version < '3.14'" in dep for dep in optional_deps["litellm"])


def test_mcp_claude_cli_and_sdk_profiles_have_explicit_contracts():
    """Claude defaults to SDK/MCP 1; only the CLI profile co-installs with MCP 2."""
    root = Path(__file__).parent.parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())

    optional_deps = pyproject["project"]["optional-dependencies"]
    groups = pyproject["dependency-groups"]
    conflicts = pyproject["tool"]["uv"]["conflicts"]

    assert optional_deps["mcp"] == ["mcp==2.0.0"]
    sdk_pins = [
        "claude-agent-sdk==0.2.139",
        "anthropic==0.122.0",
    ]
    assert optional_deps["claude"] == sdk_pins
    assert optional_deps["claude-cli"] == []
    assert optional_deps["claude-sdk"] == sdk_pins
    assert "mcp" not in optional_deps["all"][0]
    assert "claude-sdk" not in optional_deps["all"][0]
    assert "claude" in optional_deps["all"][0]
    assert groups["mcp-test"] == ["mcp==2.0.0"]
    assert groups["claude-sdk-test"] == ["ouroboros-ai[claude-sdk]"]
    assert not any("mcp" in dep or "claude" in dep for dep in groups["dev"])
    assert [
        {"extra": "claude"},
        {"extra": "mcp"},
    ] in conflicts
    assert [{"extra": "claude"}, {"group": "mcp-test"}] in conflicts
    assert [
        {"extra": "claude-sdk"},
        {"extra": "mcp"},
    ] in conflicts
    assert [{"extra": "claude-sdk"}, {"group": "mcp-test"}] in conflicts
    assert [{"extra": "all"}, {"extra": "mcp"}] in conflicts
    assert [{"extra": "all"}, {"group": "mcp-test"}] in conflicts
    assert [{"extra": "claude-cli"}, {"extra": "mcp"}] not in conflicts


def test_shipped_mcp_launchers_use_the_isolated_mcp_profile() -> None:
    """Repository and plugin launchers must never combine MCP 2 with Claude.

    Pin policy (#2066): the SHIPPED plugin descriptors pin the served
    package to their plugin manifest version so a plugin update changes
    the uvx cache key, while the repository-root development launchers
    deliberately stay unpinned and resolve the newest release on first
    use. scripts/sync-plugin-version.py keeps the pins in step with the
    manifests.
    """
    root = Path(__file__).parent.parent.parent

    def _isolated_args(requirement: str, runtime: str, backend: str) -> list[str]:
        return [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            requirement,
            "ouroboros",
            "mcp",
            "serve",
            "--runtime",
            runtime,
            "--llm-backend",
            backend,
        ]

    claude_plugin_version = json.loads(
        (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    codex_plugin_version = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]

    repository_entry = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "ouroboros"
    ]
    plugin_entry = json.loads((root / ".claude-plugin" / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["ouroboros"]
    codex_entry = tomllib.loads((root / ".codex" / "config.toml").read_text(encoding="utf-8"))[
        "mcp_servers"
    ]["ouroboros"]
    codex_plugin_entry = json.loads((root / ".mcp.codex.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["ouroboros"]

    launcher_contracts = [
        # (entry, requirement, runtime, backend)
        (repository_entry, "ouroboros-ai[mcp]", "claude-cli", "claude_code"),
        (
            plugin_entry,
            f"ouroboros-ai[mcp]=={claude_plugin_version}",
            "claude-cli",
            "claude_code",
        ),
        (codex_entry, "ouroboros-ai[mcp]", "codex", "codex"),
        (
            codex_plugin_entry,
            f"ouroboros-ai[mcp]=={codex_plugin_version}",
            "codex",
            "codex",
        ),
    ]
    for entry, requirement, runtime, backend in launcher_contracts:
        assert entry["command"] == "uvx"
        assert entry["args"] == _isolated_args(requirement, runtime, backend)


def test_runtime_guides_require_isolated_mcp_host_launchers() -> None:
    """Host guides must match setup's fail-closed uvx/pipx contract."""
    root = Path(__file__).parent.parent.parent
    guides = {
        "kiro": tuple(
            (root / "docs" / "runtime-guides" / filename).read_text(encoding="utf-8")
            for filename in ("kiro.md", "kiro.ko.md")
        ),
        "copilot": tuple(
            (root / "docs" / "runtime-guides" / filename).read_text(encoding="utf-8")
            for filename in ("copilot.md", "copilot.ko.md")
        ),
        "hermes": ((root / "docs" / "runtime-guides" / "hermes.md").read_text(encoding="utf-8"),),
    }

    exact_launcher_contracts = {
        "kiro": (
            '"command": "uvx"',
            '"args": ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
            '"command": "pipx"',
            '"args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
        ),
        "copilot": (
            '"command": "uvx"',
            '"args": ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
            '"command": "pipx"',
            '"args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
        ),
        "hermes": (
            "command: uvx",
            'args: [--isolated, --python, ">=3.12", --from, "ouroboros-ai[mcp]", '
            "ouroboros, mcp, serve]",
            "command: pipx",
            'args: [run, --spec, "ouroboros-ai[mcp]", ouroboros, mcp, serve]',
        ),
    }
    forbidden_host_commands = (
        '"command": "/path/to/ouroboros"',
        '"command": "ouroboros"',
        '"command": "python"',
        '"command": "python3"',
        "command: ouroboros",
        "command: python",
        "command: python3",
    )

    for runtime, translations in guides.items():
        for content in translations:
            assert "pipx install 'ouroboros-ai[mcp]'" in content
            assert "uv tool install 'ouroboros-ai[mcp]'" in content
            for snippet in exact_launcher_contracts[runtime]:
                assert snippet in content
            for forbidden in forbidden_host_commands:
                assert forbidden not in content

    assert "from the venv that owns" not in guides["kiro"][0]
    assert "`uv tool install` / `pip install`" not in guides["copilot"][0]
    assert "plain `pip install`" in guides["copilot"][0]
    assert "setup fails closed" in guides["copilot"][0]
    assert "never falls back to a direct `ouroboros` binary" in guides["hermes"][0]


@pytest.mark.parametrize(
    "skill_path",
    [
        "skills/setup/SKILL.md",
        "skills/update/SKILL.md",
        "skills/welcome/SKILL.md",
        "skills/pm/SKILL.md",
        ".claude-plugin/skills/setup/SKILL.md",
        ".claude-plugin/skills/update/SKILL.md",
        ".claude-plugin/skills/welcome/SKILL.md",
        ".claude-plugin/skills/pm/SKILL.md",
    ],
)
def test_claude_skills_never_combine_sdk_and_mcp_profiles(skill_path: str) -> None:
    """Shipped Claude guidance must preserve the MCP 1.x / MCP 2 boundary."""
    content = Path(skill_path).read_text(encoding="utf-8")

    assert "ouroboros-ai[mcp,claude-sdk]" not in content
    assert "ouroboros-ai[claude-sdk,mcp]" not in content


@pytest.mark.parametrize("skill_name", ["update", "pm", "unstuck"])
def test_claude_plugin_skill_mirrors_canonical_skill(skill_name: str) -> None:
    """Host-agnostic marketplace skills must mirror their canonical source."""
    root = Path(__file__).parent.parent.parent
    canonical = (root / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
    plugin = (root / ".claude-plugin" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert plugin == canonical


@pytest.mark.parametrize(
    "skill_path",
    ["skills/setup/SKILL.md", "skills/welcome/SKILL.md", "skills/pm/SKILL.md"],
)
def test_claude_skills_do_not_use_mcp_json_as_setup_health(skill_path: str) -> None:
    """Standalone Claude onboarding cannot treat a legacy MCP file as activation."""
    content = Path(skill_path).read_text(encoding="utf-8")

    assert "grep -q ouroboros" not in content
    assert "grep -q '\"ouroboros\"' ~/.claude/mcp.json" not in content


@pytest.mark.parametrize(
    "skill_path",
    ["skills/setup/SKILL.md", ".claude-plugin/skills/setup/SKILL.md"],
)
def test_claude_setup_surfaces_keep_default_sdk_distinct_from_cli_worker(
    skill_path: str,
) -> None:
    """Both shipped setup summaries must reflect the selected package contract."""
    content = Path(skill_path).read_text(encoding="utf-8")

    assert "default Claude Agent SDK runtime\non MCP 1.x" in content
    assert "Mode:                     Claude Agent SDK (MCP 1.x)" in content
    assert (
        "dependency-free Claude CLI worker remains a distinct, explicit `[claude-cli]`" in content
    )
    assert "saved Ouroboros config selects the Claude CLI runtime" not in content
    assert "Mode:                     Claude CLI\n" not in content


def test_mcp_serve_documentation_names_runtime_and_public_claude_aliases() -> None:
    """Shipped commands cannot silently inherit the SDK default in an MCP 2 process."""
    cli_reference = Path("docs/cli-reference.md").read_text(encoding="utf-8")

    assert "`claude`, `claude-sdk`, `claude-cli`, `codex`" in cli_reference
    assert "MCP 2 server rejects SDK-backed `claude`/`claude-sdk`" in cli_reference

    shipped_markdown = [
        *Path("docs").rglob("*.md"),
        *Path("skills").rglob("*.md"),
        *Path(".claude-plugin/skills").rglob("*.md"),
    ]
    for doc_path in dict.fromkeys(shipped_markdown):
        in_fenced_code = False
        for line in doc_path.read_text(encoding="utf-8").splitlines():
            command = line.strip()
            if command.startswith("```"):
                in_fenced_code = not in_fenced_code
                continue
            assert command != "ouroboros mcp serve", doc_path
            executable_command = command.removeprefix("$ ")
            if (
                in_fenced_code
                and executable_command.startswith("ouroboros mcp serve")
                and "[OPTIONS]" not in executable_command
            ):
                assert "--runtime" in executable_command, doc_path
            if command.startswith("ouroboros mcp serve") and "--llm-backend" in command:
                assert "--runtime" in command, doc_path


def test_cli_reference_isolated_mcp_launchers_have_bootable_runtime_contract() -> None:
    """Both documented isolated launchers pin the MCP 2 Claude worker explicitly."""
    cli_reference = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    integration_section = cli_reference.split("**MCP host integration:**", 1)[1].split(
        "**Runtime selection**",
        1,
    )[0]
    launcher_blocks = [
        json.loads(block.split("\n```", 1)[0])
        for block in integration_section.split("```json\n")[1:]
    ]
    launchers = [block["mcpServers"]["ouroboros"] for block in launcher_blocks]

    assert launchers == [
        {
            "command": "uvx",
            "args": [
                "--isolated",
                "--python",
                ">=3.12",
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                "--runtime",
                "claude-cli",
                "--llm-backend",
                "claude_code",
            ],
        },
        {
            "command": "pipx",
            "args": [
                "run",
                "--spec",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                "--runtime",
                "claude-cli",
                "--llm-backend",
                "claude_code",
            ],
        },
    ]


def test_python_version_constraint():
    """Test that Python version is set to >=3.12."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    python_version = pyproject["project"]["requires-python"]
    assert python_version == ">=3.12", f"Python version should be '>=3.12', got '{python_version}'"


def test_build_excludes_generated_artifacts():
    """Source distributions should not ship local build/cache artifacts."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    excludes = set(pyproject["tool"]["hatch"]["build"]["exclude"])
    required_excludes = {
        "**/target",
        "**/__pycache__",
        "/.mypy_cache",
        "/.pytest_cache",
        "/.ruff_cache",
        "/.venv",
        "/coverage.xml",
        "/.coverage",
    }

    missing = required_excludes - excludes
    assert not missing, f"Missing hatch build excludes for generated artifacts: {missing}"


def test_shipped_mcp_launcher_requests_only_the_servers_own_extra() -> None:
    """The launcher must request ``[mcp]`` and nothing else.

    The launcher used to request ``[mcp,claude]``, which is an *unsatisfiable*
    install rather than a degraded one — ``mcp==2.0.0`` against the SDK's
    transitive ``mcp<2.0.0`` — so the server never starts at all
    (Q00/ouroboros#1839).

    This asserts the extras list rather than trying to prove the extras
    co-resolve. That earlier phrasing could not decide the case it was written
    for: the conflict is transitive (``claude`` lists ``claude-agent-sdk``, and
    the ``mcp`` pin lives one level down), so comparing direct requirement names
    finds ``mcp`` and ``claude-agent-sdk``, sees two different names, and passes
    the exact combination it claims to reject. Proving co-resolution statically
    needs the whole dependency graph; asserting the one-line literal that gates
    whether the server boots needs nothing and cannot silently pass.

    What this gives up: a genuinely new, compatible extra added to the launcher
    fails here and has to be added deliberately. For the file that decides
    whether the MCP server starts, that is the intended cost.
    """
    import re

    root = Path(__file__).parent.parent.parent
    entry = json.loads((root / ".claude-plugin" / ".mcp.json").read_text(encoding="utf-8"))
    args = entry["mcpServers"]["ouroboros"]["args"]
    spec = args[args.index("--from") + 1]
    requested = re.findall(r"\[([^\]]*)\]", spec)
    extras = [e.strip() for e in (requested[0].split(",") if requested else [])]

    assert extras == ["mcp"], (
        f"shipped MCP launcher requests extras {extras!r}; it must request only "
        "['mcp']. Adding 'claude' here reintroduces the unsatisfiable "
        "mcp==2.0.0 vs claude-agent-sdk(mcp<2.0.0) install from #1839."
    )
