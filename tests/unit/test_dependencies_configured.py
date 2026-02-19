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

    required_deps = [
        "typer",
        "httpx",
        "pydantic",
        "structlog",
        "litellm",
        "sqlalchemy",
        "aiosqlite",
        "stamina",
        "rich",
        "pyyaml",
    ]

    for dep in required_deps:
        assert dep in dep_names, f"Required dependency '{dep}' not found in pyproject.toml"


def test_dev_dependencies_configured():
    """Test that dev dependencies are configured."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    # Check for dev dependencies in optional dependencies or dev group
    dev_deps = pyproject.get("dependency-groups", {}).get("dev", [])
    dep_names = {dep.split(">=")[0].split("==")[0] for dep in dev_deps}

    required_dev_deps = ["pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy", "pre-commit"]

    for dep in required_dev_deps:
        assert dep in dep_names, f"Required dev dependency '{dep}' not found in pyproject.toml"


def test_python_version_constraint():
    """Test that Python version is set to >=3.14."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    python_version = pyproject["project"]["requires-python"]
    assert python_version == ">=3.14", f"Python version should be '>=3.14', got '{python_version}'"
