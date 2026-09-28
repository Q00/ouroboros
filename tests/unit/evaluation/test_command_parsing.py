"""The detector and reader must agree on argv without losing Windows paths."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from ouroboros.evaluation import command_parsing
from ouroboros.evaluation.detector import (
    DetectedCommands,
    _command_is_valid,
    _render_toml,
)
from ouroboros.evaluation.languages import _parse_command, build_mechanical_config


@pytest.fixture(params=[False, True], ids=["posix", "windows"])
def platform_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    # Patch only the tokenizer, not os.name (which also changes pathlib).
    monkeypatch.setattr(command_parsing, "_WINDOWS", request.param)
    return request.param


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('pytest -k "slow"', ("pytest", "-k", "slow")),
        ('pytest -k "slow or fast"', ("pytest", "-k", "slow or fast")),
        ("pytest -k 'slow or fast'", ("pytest", "-k", "slow or fast")),
        ('pytest --filter="two words"', ("pytest", "--filter=two words")),
        ('pytest -k"slow or fast"', ("pytest", "-kslow or fast")),
        ("pytest \"\" ''", ("pytest", "", "")),
        ('"pytest" -q', ("pytest", "-q")),
        ('pytest "tests/한글 경로"', ("pytest", "tests/한글 경로")),
        ('pytest "a\'b"', ("pytest", "a'b")),
        ("pytest 'a\"b'", ("pytest", 'a"b')),
        ("pytest test#name", ("pytest", "test#name")),
    ],
)
def test_grouping_and_reader_agree(
    platform_mode: bool, command: str, expected: tuple[str, ...]
) -> None:
    assert tuple(command_parsing.split_command(command)) == expected
    assert _parse_command(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r"pytest tests\unit\test_example.py", ("pytest", r"tests\unit\test_example.py")),
        (
            r'pytest "tests\한글 경로\test_example.py"',
            ("pytest", r"tests\한글 경로\test_example.py"),
        ),
        (r'pytest --path="tests\two words"', ("pytest", r"--path=tests\two words")),
        ("pytest tests\\unit\\", ("pytest", "tests\\unit\\")),
    ],
)
def test_windows_preserves_backslashes(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: tuple[str, ...]
) -> None:
    monkeypatch.setattr(command_parsing, "_WINDOWS", True)
    assert _parse_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [r"pytest tests\unit", r'pytest "a \"quoted\" label"', r"pytest two\ words", "pytest ''"],
)
def test_posix_escaping_is_unchanged(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    monkeypatch.setattr(command_parsing, "_WINDOWS", False)
    assert command_parsing.split_command(command) == shlex.split(command)


@pytest.mark.parametrize(
    "command", ['pytest "unterminated', "pytest 'unterminated", 'pytest -k="bad']
)
def test_malformed_quotes_are_rejected(platform_mode: bool, tmp_path: Path, command: str) -> None:
    with pytest.raises(ValueError):
        command_parsing.split_command(command)
    assert _parse_command(command) is None
    assert not _command_is_valid(tmp_path, command)


@pytest.mark.parametrize(
    "command",
    [r'pytest "a \"quoted\" label"', r"pytest 'a \'quoted\' label'", 'pytest "tests\\unit\\"'],
)
def test_windows_ambiguous_backslash_quotes_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    monkeypatch.setattr(command_parsing, "_WINDOWS", True)
    with pytest.raises(ValueError, match="ambiguous"):
        command_parsing.split_command(command)
    assert _parse_command(command) is None
    assert not _command_is_valid(tmp_path, command)


@pytest.mark.parametrize("operator", ["&&", "||", "|", ";", ">", "<", "`", "$("])
def test_even_quoted_shell_operators_stay_blocked(
    platform_mode: bool, tmp_path: Path, operator: str
) -> None:
    command = f'pytest "slow {operator} fast"'
    assert _parse_command(command) is None
    assert not _command_is_valid(tmp_path, command)


@pytest.mark.parametrize(
    "argument",
    [
        '"../outside/test.py"',
        '"..\\outside\\test.py"',
        '--path="../outside"',
        '"C:/outside/test.py"',
    ],
)
def test_quoted_external_arguments_stay_blocked(
    platform_mode: bool, tmp_path: Path, argument: str
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    command = f"pytest {argument}"
    assert not _command_is_valid(tmp_path, command)
    assert _parse_command(command, working_dir=tmp_path) is None


@pytest.mark.parametrize(
    "head",
    [
        r"C:\outside\pytest",
        "C:pytest",
        r"\outside\pytest",
        r"\\server\share\pytest",
        "/outside/pytest",
    ],
)
@pytest.mark.parametrize("quoted", [False, True])
def test_windows_external_executable_heads_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, head: str, quoted: bool
) -> None:
    monkeypatch.setattr(command_parsing, "_WINDOWS", True)
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    command = f'"{head}" -q' if quoted else f"{head} -q"
    assert _parse_command(command) is None
    assert not _command_is_valid(tmp_path, command)


def test_quoted_targets_keep_entrypoint_and_mutation_checks(
    platform_mode: bool, tmp_path: Path
) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    (tmp_path / "Makefile").write_text("test:\n\tpytest\ndeploy:\n\tdeploy\n")
    assert _parse_command('npm run "test"', working_dir=tmp_path) == ("npm", "run", "test")
    for command in ['npm run "missing"', 'npm "install"', 'make "deploy"', '"curl" example.com']:
        assert not _command_is_valid(tmp_path, command)
        assert _parse_command(command, working_dir=tmp_path) is None


def test_native_toml_to_subprocess_preserves_filter_and_spaced_path(tmp_path: Path) -> None:
    """Exercise the real platform through TOML decoding and a child pytest."""
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    test_dir = tmp_path / "tests" / "한글 two words"
    test_dir.mkdir(parents=True)
    (test_dir / "test_example.py").write_text(
        "def test_slow(): pass\ndef test_fast(): pass\ndef test_other(): assert False\n"
    )
    path_arg = str(test_dir.relative_to(tmp_path))
    command = f'python -m pytest "{path_arg}" -k "slow or fast" -q -p no:cacheprovider'
    assert _command_is_valid(tmp_path, command)
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "mechanical.toml").write_text(
        _render_toml(DetectedCommands(test=command)), encoding="utf-8"
    )
    config = build_mechanical_config(tmp_path)
    assert config.test_command == (
        "python",
        "-m",
        "pytest",
        path_arg,
        "-k",
        "slow or fast",
        "-q",
        "-p",
        "no:cacheprovider",
    )
    # Use the test interpreter explicitly; do not depend on another python on PATH.
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTHONUTF8": "1",
    }
    result = subprocess.run(
        [sys.executable, *config.test_command[1:]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed, 1 deselected" in result.stdout


def test_quoted_subproject_flag_preserves_repo_validation(
    platform_mode: bool, tmp_path: Path
) -> None:
    project = tmp_path / "sub project"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    command = 'npm --prefix="sub project" run "test"'
    assert _command_is_valid(tmp_path, command)
    assert _parse_command(command, working_dir=tmp_path) == (
        "npm",
        "--prefix=sub project",
        "run",
        "test",
    )
    outside = 'npm --prefix="../sub project" run "test"'
    assert not _command_is_valid(tmp_path, outside)
    assert _parse_command(outside, working_dir=tmp_path) is None
