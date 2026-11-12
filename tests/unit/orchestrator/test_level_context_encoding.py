"""Source signatures survive non-UTF-8 system locales."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from ouroboros.orchestrator import level_context


@pytest.mark.parametrize(
    ("suffix", "source", "signature"),
    [
        (".py", "# 파일 시스템\ndef public():\n    pass\n", "def public()"),
        (".ts", "// 파일 시스템\nexport function public() {}\n", "function public()"),
        (".go", "// 파일 시스템\nfunc Public() {}\n", "func Public()"),
    ],
)
def test_utf8_source_with_non_ascii_comment_under_legacy_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, source: str, signature: str
) -> None:
    path = tmp_path / f"sample{suffix}"
    path.write_text(source, encoding="utf-8")

    def legacy_open(file: object, *args: object, **kwargs: object):
        if "encoding" not in kwargs:
            kwargs["encoding"] = "cp949"
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(level_context, "open", legacy_open, raising=False)
    summary = level_context._build_public_api_summary((str(path),), workspace_root=str(tmp_path))
    assert signature in summary


def test_python_declared_non_utf8_encoding_is_honored(tmp_path: Path) -> None:
    path = tmp_path / "legacy.py"
    path.write_bytes("# coding: latin-1\n# café\ndef public():\n    pass\n".encode("latin-1"))
    assert level_context._extract_public_api(str(path), str(tmp_path)) == ["def public()"]


@pytest.mark.parametrize("encoding", ["unknown-encoding", "base64_codec"])
def test_invalid_python_encoding_cookie_is_skipped(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "broken.py"
    path.write_bytes(f"# coding: {encoding}\ndef public():\n    pass\n".encode())
    assert level_context._extract_public_api(str(path), str(tmp_path)) == []
