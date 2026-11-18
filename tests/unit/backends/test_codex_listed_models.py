"""Codex model suggestions come from Codex's own model cache, read safely."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from ouroboros.backends import model_catalog as mc

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "codex_models_cache.json"
_LISTED = [
    ("gpt-6.2-sol", "GPT-6.2-Sol"),
    ("gpt-6.2-astra", "GPT-6.2-Astra"),
    ("gpt-6.1-terra", "GPT-6.1 Terra"),
    ("gpt-6.1-sol", "GPT-6.1-Sol"),
    ("gpt-6-astra", "GPT-6-Astra"),
    ("gpt-6-sol", "GPT-6-Sol"),
    ("gpt-6-luna", "GPT-6-Luna"),
    ("gpt-5.6-sol", "GPT-5.6-Sol"),
]


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(mc, "_codex_listed_memo", None)
    return home


def _pairs(listed: tuple[mc.ListedModel, ...] | None) -> list[tuple[str, str | None]] | None:
    return None if listed is None else [(item.slug, item.display_name) for item in listed]


def _write(home: Path, payload: object) -> Path:
    path = home / "models_cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_listed_models_are_read_in_file_order_with_display_names(codex_home: Path) -> None:
    shutil.copy(_FIXTURE, codex_home / "models_cache.json")

    # Hidden ("hide") and unlisted ("none") records are left out.
    assert _pairs(mc.codex_listed_models()) == _LISTED


def test_the_cache_is_found_through_the_resolved_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mc, "_codex_listed_memo", None)
    (tmp_path / ".codex").mkdir()
    shutil.copy(_FIXTURE, tmp_path / ".codex" / "models_cache.json")

    assert _pairs(mc.codex_listed_models()) == _LISTED
    assert mc.codex_listed_models(tmp_path / "elsewhere") is None


def test_a_missing_cache_means_the_static_catalog(codex_home: Path) -> None:
    assert mc.codex_listed_models() is None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"\xff\xfe\x00garbage",
        b"[]",
        b'{"models": {"slug": "gpt-6-sol"}}',
        b'{"data": [{"slug": "gpt-6-sol", "visibility": "list"}]}',
        b'{"models": [{"slug": "gpt-6-sol", "visibility": "hide"}]}',
        b'{"models": [{"slug": "gpt-6-sol"}]}',
        b'{"models": [{"slug": "has space", "visibility": "list"}]}',
        b'{"models": ["gpt-6-sol"]}',
    ],
)
def test_an_unusable_cache_means_the_static_catalog(codex_home: Path, content: bytes) -> None:
    (codex_home / "models_cache.json").write_bytes(content)

    assert mc.codex_listed_models() is None


def test_records_that_are_not_safe_model_ids_are_skipped(codex_home: Path) -> None:
    _write(
        codex_home,
        {
            "models": [
                {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list"},
                {"slug": "--config=evil", "visibility": "list"},
                {"slug": "x" * 129, "visibility": "list"},
                {"slug": 7, "visibility": "list"},
                {"slug": "default", "visibility": "list"},
                {"slug": "gpt-6-sol", "display_name": "Duplicate", "visibility": "list"},
                {"slug": "gpt-6-luna", "display_name": "bad\nname", "visibility": "list"},
                {"slug": "gpt-6-astra", "display_name": 3, "visibility": "list"},
                {"slug": "gpt-6-terra", "display_name": "N" * 121, "visibility": "list"},
                "not a record",
            ]
        },
    )

    assert _pairs(mc.codex_listed_models()) == [
        ("gpt-6-sol", "GPT-6-Sol"),
        ("gpt-6-luna", None),
        ("gpt-6-astra", None),
        ("gpt-6-terra", None),
    ]


def test_at_most_a_hundred_models_are_listed(codex_home: Path) -> None:
    _write(
        codex_home,
        {"models": [{"slug": f"m-{index}", "visibility": "list"} for index in range(150)]},
    )

    listed = mc.codex_listed_models()

    assert listed is not None and len(listed) == 100
    assert listed[-1].slug == "m-99"


def test_an_oversized_cache_is_not_read(codex_home: Path) -> None:
    path = _write(codex_home, {"models": [{"slug": "gpt-6-sol", "visibility": "list"}]})
    with path.open("ab") as handle:
        handle.write(b" " * (4 * 1024 * 1024))

    assert mc.codex_listed_models() is None


def test_a_cache_that_is_not_a_regular_file_is_not_read(codex_home: Path) -> None:
    (codex_home / "models_cache.json").mkdir()

    assert mc.codex_listed_models() is None


def test_the_file_is_read_again_only_when_it_changes(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(codex_home, {"models": [{"slug": "gpt-6-sol", "visibility": "list"}]})
    assert _pairs(mc.codex_listed_models()) == [("gpt-6-sol", None)]

    reads: list[Path] = []
    real_open = Path.open

    def counting_open(self: Path, *args: object, **kwargs: object) -> object:
        reads.append(self)
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", counting_open)
    assert _pairs(mc.codex_listed_models()) == [("gpt-6-sol", None)]
    assert reads == []

    _write(codex_home, {"models": [{"slug": "gpt-6.2-sol", "visibility": "list"}]})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    reads.clear()  # the write itself
    assert _pairs(mc.codex_listed_models()) == [("gpt-6.2-sol", None)]
    assert reads == [path]


def test_reading_never_raises(codex_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(codex_home, {"models": [{"slug": "gpt-6-sol", "visibility": "list"}]})

    def broken(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", broken)

    assert mc.codex_listed_models() is None


def test_the_static_codex_catalog_is_unchanged() -> None:
    """Routing and the terminal settings app still see the static catalog."""
    choices = mc.model_choices("codex")

    assert choices[0] == mc.DEFAULT_MODEL_SENTINEL
    assert "gpt-6.2-sol" not in choices
