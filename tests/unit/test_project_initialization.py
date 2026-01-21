"""Test project initialization structure."""
from pathlib import Path


def test_project_structure_exists():
    """Test that uv init creates proper project structure."""
    root = Path(__file__).parent.parent.parent
    src_dir = root / "src" / "ouroboros"

    # Check src/ouroboros/ directory exists
    assert src_dir.is_dir(), f"src/ouroboros/ directory should exist at {src_dir}"

    # Check __init__.py exists
    init_py = src_dir / "__init__.py"
    assert init_py.is_file(), f"src/ouroboros/__init__.py should exist at {init_py}"

    # Check pyproject.toml exists
    pyproject = root / "pyproject.toml"
    assert pyproject.is_file(), f"pyproject.toml should exist at {pyproject}"


def test_python_version_file():
    """Test that .python-version is set to 3.14."""
    root = Path(__file__).parent.parent.parent
    python_version = root / ".python-version"

    assert python_version.is_file(), f".python-version should exist at {python_version}"
    content = python_version.read_text().strip()
    assert content == "3.14", f".python-version should contain '3.14', got '{content}'"


def test_package_entry_point():
    """Test that __main__.py entry point exists."""
    root = Path(__file__).parent.parent.parent
    main_py = root / "src" / "ouroboros" / "__main__.py"

    assert main_py.is_file(), f"src/ouroboros/__main__.py should exist at {main_py}"


def test_py_typed_marker():
    """Test that py.typed marker exists for PEP 561."""
    root = Path(__file__).parent.parent.parent
    py_typed = root / "src" / "ouroboros" / "py.typed"

    assert py_typed.is_file(), f"src/ouroboros/py.typed should exist at {py_typed}"
