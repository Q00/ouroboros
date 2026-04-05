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
    """Core/runtime-facing dependencies should carry explicit upper bounds."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    runtime_deps = pyproject["project"]["dependencies"]
    for dep in runtime_deps:
        assert "<" in dep, f"Runtime dependency missing upper bound: {dep}"

    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})
    for extra_name in ("claude", "litellm", "dashboard", "mcp", "tui"):
        for dep in optional_deps[extra_name]:
            assert "<" in dep, f"Optional dependency '{extra_name}' missing upper bound: {dep}"


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


def test_python_version_constraint():
    """Test that Python version is set to >=3.12."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    python_version = pyproject["project"]["requires-python"]
    assert python_version == ">=3.12", f"Python version should be '>=3.12', got '{python_version}'"
