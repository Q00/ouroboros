"""Test that tooling is configured correctly."""
from pathlib import Path
import tomllib


def test_ruff_config_exists():
    """Test that ruff is configured in pyproject.toml."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    assert "tool" in pyproject, "tool section should exist in pyproject.toml"
    assert "ruff" in pyproject["tool"], "ruff should be configured in pyproject.toml"


def test_ruff_line_length():
    """Test that ruff line length is set to 100."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    ruff_config = pyproject["tool"]["ruff"]
    assert ruff_config["line-length"] == 100, "ruff should have line-length = 100"


def test_pre_commit_config_exists():
    """Test that .pre-commit-config.yaml exists."""
    root = Path(__file__).parent.parent.parent
    pre_commit_config = root / ".pre-commit-config.yaml"
    assert pre_commit_config.is_file(), f".pre-commit-config.yaml should exist at {pre_commit_config}"


def test_mypy_configured():
    """Test that mypy is configured in pyproject.toml."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    assert "tool" in pyproject, "tool section should exist in pyproject.toml"
    assert "mypy" in pyproject["tool"], "mypy should be configured in pyproject.toml"
    mypy_config = pyproject["tool"]["mypy"]
    assert mypy_config["python_version"] == "3.14"
    assert mypy_config["strict"] is True


def test_pytest_asyncio_mode():
    """Test that pytest asyncio_mode is set to 'auto'."""
    root = Path(__file__).parent.parent.parent
    pyproject_path = root / "pyproject.toml"

    content = pyproject_path.read_text()
    pyproject = tomllib.loads(content)

    pytest_config = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_config["asyncio_mode"] == "auto", "pytest asyncio_mode should be 'auto'"
