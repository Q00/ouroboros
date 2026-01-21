"""Test initial module structure."""
from pathlib import Path


def test_init_py_has_version():
    """Test that __init__.py has __version__ attribute."""
    root = Path(__file__).parent.parent.parent
    init_py = root / "src" / "ouroboros" / "__init__.py"

    content = init_py.read_text()
    assert "__version__" in content, "__init__.py should define __version__"


def test_main_py_entry_point():
    """Test that __main__.py exists and imports main."""
    root = Path(__file__).parent.parent.parent
    main_py = root / "src" / "ouroboros" / "__main__.py"

    assert main_py.is_file(), f"__main__.py should exist at {main_py}"
    content = main_py.read_text()
    assert "from ouroboros import main" in content, "__main__.py should import main from ouroboros"


def test_py_typed_exists():
    """Test that py.typed marker exists."""
    root = Path(__file__).parent.parent.parent
    py_typed = root / "src" / "ouroboros" / "py.typed"

    assert py_typed.is_file(), f"py.typed should exist at {py_typed}"


def test_module_can_be_imported():
    """Test that ouroboros module can be imported."""
    # This test requires the package to be installed
    import ouroboros
    assert hasattr(ouroboros, "__version__")
    assert hasattr(ouroboros, "main")
