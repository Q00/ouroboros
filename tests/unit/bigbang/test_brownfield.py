"""Unit tests for brownfield registry schema, validation, and file I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.bigbang.brownfield import (
    BrownfieldEntry,
    load_brownfield_repos,
    load_brownfield_repos_as_dicts,
    register_brownfield_repo,
    save_brownfield_repos,
    validate_entries,
)

# ── BrownfieldEntry ────────────────────────────────────────────────


class TestBrownfieldEntry:
    """Tests for the BrownfieldEntry frozen dataclass."""

    def test_create_with_defaults(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="my-repo")
        assert entry.path == "/repo"
        assert entry.name == "my-repo"
        assert entry.desc == ""

    def test_create_with_desc(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="my-repo", desc="A description")
        assert entry.desc == "A description"

    def test_frozen(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="my-repo")
        with pytest.raises(AttributeError):
            entry.path = "/other"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        entry = BrownfieldEntry(path="/repo", name="proj", desc="desc")
        assert entry.to_dict() == {"path": "/repo", "name": "proj", "desc": "desc"}

    def test_from_dict_valid(self) -> None:
        data = {"path": "/repo", "name": "proj", "desc": "hello"}
        entry = BrownfieldEntry.from_dict(data)
        assert entry.path == "/repo"
        assert entry.name == "proj"
        assert entry.desc == "hello"

    def test_from_dict_missing_desc_defaults_empty(self) -> None:
        data = {"path": "/repo", "name": "proj"}
        entry = BrownfieldEntry.from_dict(data)
        assert entry.desc == ""

    def test_from_dict_missing_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path"):
            BrownfieldEntry.from_dict({"name": "proj"})

    def test_from_dict_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            BrownfieldEntry.from_dict({"path": "/repo"})

    def test_from_dict_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path.*empty"):
            BrownfieldEntry.from_dict({"path": "", "name": "proj"})

    def test_from_dict_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name.*empty"):
            BrownfieldEntry.from_dict({"path": "/repo", "name": "  "})

    def test_from_dict_strips_whitespace(self) -> None:
        entry = BrownfieldEntry.from_dict({"path": "  /repo  ", "name": "  proj  "})
        assert entry.path == "/repo"
        assert entry.name == "proj"

    def test_roundtrip(self) -> None:
        original = BrownfieldEntry(path="/repo", name="proj", desc="desc")
        restored = BrownfieldEntry.from_dict(original.to_dict())
        assert restored == original


# ── validate_entries ────────────────────────────────────────────────


class TestValidateEntries:
    """Tests for validate_entries()."""

    def test_valid_list(self) -> None:
        raw = [
            {"path": "/a", "name": "A", "desc": "first"},
            {"path": "/b", "name": "B"},
        ]
        entries = validate_entries(raw)
        assert len(entries) == 2
        assert entries[0].path == "/a"
        assert entries[1].desc == ""

    def test_empty_list(self) -> None:
        assert validate_entries([]) == []

    def test_not_a_list_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            validate_entries({"path": "/a", "name": "A"})

    def test_skips_non_dict_items(self) -> None:
        raw = [{"path": "/a", "name": "A"}, "not-a-dict", 42]
        entries = validate_entries(raw)
        assert len(entries) == 1

    def test_skips_invalid_entries(self) -> None:
        raw = [
            {"path": "/a", "name": "A"},
            {"path": "", "name": "bad"},  # empty path
            {"name": "no-path"},  # missing path
        ]
        entries = validate_entries(raw)
        assert len(entries) == 1
        assert entries[0].path == "/a"


# ── File I/O ────────────────────────────────────────────────────────


class TestLoadBrownfieldRepos:
    """Tests for load_brownfield_repos()."""

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        result = load_brownfield_repos(tmp_path / "nonexistent.json")
        assert result == []

    def test_loads_valid_file(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        fp.write_text(json.dumps([{"path": "/r", "name": "r"}]))
        entries = load_brownfield_repos(fp)
        assert len(entries) == 1
        assert entries[0].path == "/r"

    def test_returns_empty_on_malformed_json(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        fp.write_text("{not valid json")
        assert load_brownfield_repos(fp) == []

    def test_returns_empty_on_non_array_json(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        fp.write_text(json.dumps({"path": "/r", "name": "r"}))
        assert load_brownfield_repos(fp) == []

    def test_skips_invalid_entries_in_file(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        data = [{"path": "/good", "name": "good"}, {"bad": True}]
        fp.write_text(json.dumps(data))
        entries = load_brownfield_repos(fp)
        assert len(entries) == 1


class TestSaveBrownfieldRepos:
    """Tests for save_brownfield_repos()."""

    def test_creates_file_and_dirs(self, tmp_path: Path) -> None:
        fp = tmp_path / "sub" / "dir" / "brownfield.json"
        entries = [BrownfieldEntry(path="/r", name="r", desc="d")]
        save_brownfield_repos(entries, fp)

        assert fp.exists()
        data = json.loads(fp.read_text())
        assert len(data) == 1
        assert data[0] == {"path": "/r", "name": "r", "desc": "d"}

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        fp.write_text(json.dumps([{"path": "/old", "name": "old", "desc": ""}]))

        save_brownfield_repos(
            [BrownfieldEntry(path="/new", name="new")], fp
        )
        data = json.loads(fp.read_text())
        assert len(data) == 1
        assert data[0]["path"] == "/new"

    def test_saves_empty_list(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        save_brownfield_repos([], fp)
        assert json.loads(fp.read_text()) == []


class TestRegisterBrownfieldRepo:
    """Tests for register_brownfield_repo()."""

    def test_registers_new_repo(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        result = register_brownfield_repo("/repo", "repo", filepath=fp)
        assert len(result) == 1
        assert result[0].path == "/repo"

    def test_deduplicates_by_path(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        register_brownfield_repo("/repo", "old-name", filepath=fp)
        result = register_brownfield_repo("/repo", "new-name", filepath=fp)
        assert len(result) == 1
        assert result[0].name == "new-name"

    def test_preserves_other_repos(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        register_brownfield_repo("/a", "A", filepath=fp)
        result = register_brownfield_repo("/b", "B", filepath=fp)
        assert len(result) == 2

    def test_roundtrip_via_file(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        register_brownfield_repo("/repo", "proj", desc="my desc", filepath=fp)

        loaded = load_brownfield_repos(fp)
        assert len(loaded) == 1
        assert loaded[0] == BrownfieldEntry(
            path="/repo", name="proj", desc="my desc"
        )


class TestLoadBrownfieldReposAsDicts:
    """Tests for the dict-returning convenience wrapper."""

    def test_returns_dicts(self, tmp_path: Path) -> None:
        fp = tmp_path / "brownfield.json"
        fp.write_text(json.dumps([{"path": "/r", "name": "r", "desc": "d"}]))
        result = load_brownfield_repos_as_dicts(fp)
        assert result == [{"path": "/r", "name": "r", "desc": "d"}]

    def test_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_brownfield_repos_as_dicts(tmp_path / "no.json") == []
