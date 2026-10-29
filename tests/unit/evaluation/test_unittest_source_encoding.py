"""Unittest discovery follows the Python loader's source encoding rules."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys

import pytest

from ouroboros.evaluation.detector import ensure_mechanical_toml, has_mechanical_toml
from ouroboros.evaluation.languages import build_mechanical_config


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("encoding", "cookie"),
    [
        ("latin-1", "# coding: latin-1\n"),
        ("cp1252", "# coding: cp1252\n"),
        ("utf-8-sig", ""),
    ],
)
def test_unittest_source_decoding_matches_the_python_loader(
    tmp_path: Path, encoding: str, cookie: str
) -> None:
    """A valid Python source encoding must not hide an otherwise ordinary suite."""
    tests = tmp_path / "tests"
    tests.mkdir()
    source = (
        cookie + "import unittest\n"
        "class TestText(unittest.TestCase):\n"
        "    def test_text(self):\n"
        "        self.assertEqual('café', 'caf\\u00e9')\n"
    )
    (tests / "test_text.py").write_bytes(source.encode(encoding))

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Ran 1 test" in result.stderr
    assert _run(ensure_mechanical_toml(tmp_path, None)) is True
    assert build_mechanical_config(tmp_path).test_command == (
        "python3",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
    )


@pytest.mark.parametrize(
    "prefix", [b"# coding: no-such-codec\n", b"\xef\xbb\xbf# coding: latin-1\n"]
)
def test_invalid_python_source_encoding_still_skips_discovery(
    tmp_path: Path, prefix: bytes
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_text.py").write_bytes(
        prefix + b"import unittest\n"
        b"class TestText(unittest.TestCase):\n"
        b"    def test_text(self):\n"
        b"        pass\n"
    )

    assert _run(ensure_mechanical_toml(tmp_path, None)) is False
    assert not has_mechanical_toml(tmp_path)
