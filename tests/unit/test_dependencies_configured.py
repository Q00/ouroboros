"""Test that dependencies are configured correctly."""

from pathlib import Path
import tomllib


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
