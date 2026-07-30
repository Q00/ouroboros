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
    for extra_name in ("claude", "litellm", "dashboard", "mcp", "tui"):
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


def test_mcp_and_claude_profiles_are_isolated():
    """MCP 2 and the MCP 1.x-based Claude SDK never share an environment."""
    root = Path(__file__).parent.parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())

    optional_deps = pyproject["project"]["optional-dependencies"]
    groups = pyproject["dependency-groups"]
    conflicts = pyproject["tool"]["uv"]["conflicts"]

    assert optional_deps["mcp"] == ["mcp==2.0.0"]
    assert "mcp" not in optional_deps["all"][0]
    assert groups["mcp-test"] == ["mcp==2.0.0"]
    assert groups["claude-test"] == ["ouroboros-ai[claude]"]
    assert not any("mcp" in dep or "claude" in dep for dep in groups["dev"])
    assert [
        {"extra": "claude"},
        {"extra": "mcp"},
    ] in conflicts
    assert [{"extra": "claude"}, {"group": "mcp-test"}] in conflicts
    assert [{"extra": "all"}, {"group": "mcp-test"}] in conflicts


def test_shipped_mcp_launchers_use_the_isolated_mcp_profile() -> None:
    """Repository and plugin launchers must never combine MCP 2 with Claude."""
    root = Path(__file__).parent.parent.parent
    expected_args = [
        "--from",
        "ouroboros-ai[mcp]",
        "ouroboros",
        "mcp",
        "serve",
    ]

    repository_entry = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "ouroboros"
    ]
    plugin_entry = json.loads((root / ".claude-plugin" / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["ouroboros"]
    codex_entry = tomllib.loads((root / ".codex" / "config.toml").read_text(encoding="utf-8"))[
        "mcp_servers"
    ]["ouroboros"]

    for entry in (repository_entry, plugin_entry, codex_entry):
        assert entry["command"] == "uvx"
        assert entry["args"] == expected_args


def test_runtime_guides_require_isolated_mcp_host_launchers() -> None:
    """Host guides must match setup's fail-closed uvx/pipx contract."""
    root = Path(__file__).parent.parent.parent
    guides = {
        runtime: (root / "docs" / "runtime-guides" / f"{runtime}.md").read_text(encoding="utf-8")
        for runtime in ("kiro", "copilot", "hermes")
    }

    exact_launcher_contracts = {
        "kiro": (
            '"command": "uvx"',
            '"args": ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
            '"command": "pipx"',
            '"args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
        ),
        "copilot": (
            '"command": "uvx"',
            '"args": ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
            '"command": "pipx"',
            '"args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
        ),
        "hermes": (
            "command: uvx",
            'args: [--from, "ouroboros-ai[mcp]", ouroboros, mcp, serve]',
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

    for runtime, content in guides.items():
        assert "pipx install 'ouroboros-ai[mcp]'" in content
        assert "uv tool install 'ouroboros-ai[mcp]'" in content
        for snippet in exact_launcher_contracts[runtime]:
            assert snippet in content
        for forbidden in forbidden_host_commands:
            assert forbidden not in content

    assert "from the venv that owns" not in guides["kiro"]
    assert "`uv tool install` / `pip install`" not in guides["copilot"]
    assert "plain `pip install`" in guides["copilot"]
    assert "setup fails closed" in guides["copilot"]
    assert "never falls back to a direct `ouroboros` binary" in guides["hermes"]


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
def test_claude_skills_never_recommend_combined_mcp_profile(skill_path: str) -> None:
    """Shipped Claude guidance must preserve the MCP 1.x / MCP 2 boundary."""
    content = Path(skill_path).read_text(encoding="utf-8")

    assert "ouroboros-ai[mcp,claude]" not in content
    assert "ouroboros-ai[claude,mcp]" not in content


@pytest.mark.parametrize("skill_name", ["setup", "update", "welcome", "pm", "unstuck"])
def test_claude_plugin_skill_mirrors_canonical_skill(skill_name: str) -> None:
    """The marketplace mirror must ship the same runtime contract as source."""
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
