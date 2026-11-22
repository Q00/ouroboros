"""Copies of an app's files that the completion pass can put back."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ouroboros.orchestrator import app_versions
from ouroboros.orchestrator.app_versions import keep, put_back


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "index.html").write_text("<canvas></canvas>")
    (app / "src" / "game.js").write_text("drive()")
    (app / "run.sh").write_text("#!/bin/sh\n")
    (app / "run.sh").chmod(0o755)
    return app


def test_a_version_is_the_apps_own_regular_files(tmp_path) -> None:
    app = _app(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    for skipped in (".ouroboros/playtest", ".git", "node_modules/three"):
        (app / skipped).mkdir(parents=True)
        (app / skipped / "f").write_text("x")
    (app / "link.txt").symlink_to(outside)
    (app / "linked").symlink_to(tmp_path, target_is_directory=True)

    version = keep(app)

    assert version is not None
    assert sorted(version.files) == ["index.html", "run.sh", "src/game.js"]
    assert version.files["run.sh"] == (b"#!/bin/sh\n", 0o755)


@pytest.mark.parametrize(("limit", "value"), [("MAX_FILES", 2), ("MAX_BYTES", 20)])
def test_an_app_too_large_to_keep_whole_is_not_kept(tmp_path, monkeypatch, limit, value) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr(app_versions, limit, value)

    assert keep(app) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_app_with_a_file_that_cannot_be_read_is_not_kept(tmp_path) -> None:
    app = _app(tmp_path)
    (app / "src" / "game.js").chmod(0)
    try:
        assert keep(app) is None
    finally:
        (app / "src" / "game.js").chmod(0o644)


def test_a_version_put_back_restores_what_changed_and_deletes_nothing(tmp_path) -> None:
    app = _app(tmp_path)
    version = keep(app)
    assert version is not None
    (app / "index.html").write_text("<p>flat</p>")
    (app / "src" / "game.js").unlink()
    (app / "src").rmdir()
    (app / "run.sh").chmod(0o644)
    (app / "extra.js").write_text("x")

    written = put_back(app, version)

    assert written == 3
    assert (app / "index.html").read_text() == "<canvas></canvas>"
    assert (app / "src" / "game.js").read_text() == "drive()"
    assert (app / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (app / "extra.js").read_text() == "x"
    assert put_back(app, version) == 0


def test_a_put_back_that_fails_part_way_leaves_the_app_as_it_was(tmp_path) -> None:
    app = _app(tmp_path)
    version = keep(app)
    assert version is not None
    (app / "index.html").write_text("<p>new</p>")
    (app / "src" / "game.js").unlink()
    (app / "src").rmdir()
    (app / "src").write_text("now a file")  # src/game.js cannot be written back

    with pytest.raises(OSError):
        put_back(app, version)

    assert (app / "index.html").read_text() == "<p>new</p>"
    assert (app / "src").read_text() == "now a file"


def test_a_put_back_that_fails_part_way_removes_what_it_made(tmp_path) -> None:
    app = _app(tmp_path)
    (app / "lib" / "extra").mkdir(parents=True)
    (app / "lib" / "extra" / "util.js").write_text("help()")
    (app / "lib" / "kept.js").write_text("kept()")
    version = keep(app)
    assert version is not None
    (app / "lib" / "extra" / "util.js").unlink()
    (app / "lib" / "extra").rmdir()
    (app / "lib" / "kept.js").unlink()
    (app / "lib").rmdir()
    (app / "index.html").write_text("<p>new</p>")
    (app / "src" / "game.js").unlink()
    (app / "src").rmdir()
    (app / "src").write_text("now a file")  # written after lib/, and cannot be

    with pytest.raises(OSError):
        put_back(app, version)

    assert not (app / "lib").exists()
    assert (app / "index.html").read_text() == "<p>new</p>"
    assert (app / "src").read_text() == "now a file"
    assert sorted(p.name for p in app.iterdir()) == ["index.html", "run.sh", "src"]


def test_a_failed_put_back_leaves_a_directory_that_held_something_else(tmp_path) -> None:
    app = _app(tmp_path)
    (app / "lib").mkdir()
    (app / "lib" / "util.js").write_text("help()")
    version = keep(app)
    assert version is not None
    (app / "lib" / "util.js").unlink()
    (app / "lib" / "other.js").write_text("other()")
    (app / "src" / "game.js").unlink()
    (app / "src").rmdir()
    (app / "src").write_text("now a file")

    with pytest.raises(OSError):
        put_back(app, version)

    assert sorted(p.name for p in (app / "lib").iterdir()) == ["other.js"]


def test_a_put_back_never_writes_through_a_planted_link(tmp_path) -> None:
    app = _app(tmp_path)
    version = keep(app)
    assert version is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    (app / "src" / "game.js").unlink()
    (app / "src").rmdir()
    (app / "src").symlink_to(outside, target_is_directory=True)
    (app / "index.html").write_text("<p>new</p>")

    with pytest.raises(OSError):
        put_back(app, version)

    assert list(outside.iterdir()) == []
    assert (app / "index.html").read_text() == "<p>new</p>"


def test_nothing_is_put_back_when_the_app_now_cannot_be_kept(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    version = keep(app)
    assert version is not None
    (app / "index.html").write_text("<p>new</p>")
    monkeypatch.setattr(app_versions, "MAX_FILES", 1)

    with pytest.raises(OSError):
        put_back(app, version)

    assert (app / "index.html").read_text() == "<p>new</p>"
