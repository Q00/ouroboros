"""CLI Seed readers use the same encoding as persisted Seed files."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
import typer

from ouroboros.cli.commands import init, run


@pytest.mark.parametrize("codepage", ["cp949", "cp1252"])
def test_run_loads_utf8_seed_independent_of_default_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codepage: str
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("goal: 파일 시스템 만들기\n", encoding="utf-8")

    def legacy_open(file: object, *args: object, **kwargs: object):
        if "encoding" not in kwargs:
            kwargs["encoding"] = codepage
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(run, "open", legacy_open, raising=False)
    assert run._load_seed_from_yaml(seed)["goal"] == "파일 시스템 만들기"


@pytest.mark.parametrize("codepage", ["cp949", "cp1252"])
def test_init_reads_utf8_pm_seed_summary_independent_of_default_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codepage: str
) -> None:
    seed = tmp_path / "pm_seed_sample.yaml"
    seed.write_text(
        "product_name: 파일 도구\ngoal: 한글 문서 관리\npm_id: pm-1\n", encoding="utf-8"
    )

    def legacy_open(file: object, *args: object, **kwargs: object):
        if "encoding" not in kwargs:
            kwargs["encoding"] = codepage
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(init, "open", legacy_open, raising=False)
    assert init._display_pm_seed_info(seed) == {
        "name": "파일 도구",
        "goal": "한글 문서 관리",
        "pm_id": "pm-1",
    }


def test_invalid_utf8_seed_remains_a_cli_error(tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_bytes(b"goal: \xff\n")
    with pytest.raises(typer.Exit):
        run._load_seed_from_yaml(seed)
