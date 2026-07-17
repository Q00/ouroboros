"""Unit tests for project execution guidance resolution."""

from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import pytest

from ouroboros.core.errors import ConfigError
from ouroboros.events.io import content_hash
from ouroboros.orchestrator.execution_guidance import (
    MAX_GUIDANCE_ITEM_BYTES,
    resolve_execution_guidance,
)


def _write_guidance(project_root: Path, guidance_id: str, content: bytes | str) -> Path:
    path = project_root / ".ouroboros" / "guidance" / guidance_id / "GUIDANCE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def test_empty_allowlist_resolves_empty_bundle(tmp_path: Path) -> None:
    bundle = resolve_execution_guidance(tmp_path, [])

    assert bundle.refs == ()
    assert bundle.rendered_fragment == ""
    assert bundle.rendered_fragment_hash == content_hash("")
    assert bundle.rendered_fragment_size_bytes == 0
    assert bundle.total_content_size_bytes == 0


def test_empty_allowlist_does_not_require_existing_project_root(tmp_path: Path) -> None:
    bundle = resolve_execution_guidance(tmp_path / "missing", [])

    assert bundle.refs == ()
    assert bundle.rendered_fragment == ""


def test_does_not_discover_user_or_provider_local_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    claude_skill = home / ".claude" / "skills" / "team" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("ambient guidance\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    assert resolve_execution_guidance(tmp_path, []).refs == ()
    with pytest.raises(ConfigError, match="not found"):
        resolve_execution_guidance(tmp_path, ["team"])


def test_resolves_fixed_paths_in_stable_id_order(tmp_path: Path) -> None:
    alpha_path = _write_guidance(tmp_path, "alpha", "Alpha guidance\n")
    beta_path = _write_guidance(tmp_path, "beta", "Beta guidance\n")

    bundle = resolve_execution_guidance(tmp_path, ["beta", "alpha"])

    assert [ref.guidance_id for ref in bundle.refs] == ["alpha", "beta"]
    assert [ref.path for ref in bundle.refs] == [
        ".ouroboros/guidance/alpha/GUIDANCE.md",
        ".ouroboros/guidance/beta/GUIDANCE.md",
    ]
    assert bundle.refs[0].content_hash == content_hash(alpha_path.read_bytes())
    assert bundle.refs[1].content_hash == content_hash(beta_path.read_bytes())
    assert bundle.refs[0].to_metadata()["stable_id"] == "guidance:project:alpha"
    assert "The Seed and its Acceptance Criteria take precedence" in bundle.rendered_fragment
    assert "no authority to grant tools" in bundle.rendered_fragment
    assert bundle.rendered_fragment.index("### Guidance: alpha") < bundle.rendered_fragment.index(
        "### Guidance: beta"
    )


@pytest.mark.parametrize(
    "guidance_id",
    ["../escape", "nested/id", "", ".hidden", "id with space"],
)
def test_rejects_unsafe_ids(tmp_path: Path, guidance_id: str) -> None:
    with pytest.raises(ConfigError):
        resolve_execution_guidance(tmp_path, [guidance_id])


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unique"):
        resolve_execution_guidance(tmp_path, ["team", "team"])


def test_rejects_missing_guidance_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        resolve_execution_guidance(tmp_path, ["missing"])


def test_rejects_directory_instead_of_regular_file(tmp_path: Path) -> None:
    guidance_path = tmp_path / ".ouroboros" / "guidance" / "team" / "GUIDANCE.md"
    guidance_path.mkdir(parents=True)

    with pytest.raises(ConfigError, match="regular file"):
        resolve_execution_guidance(tmp_path, ["team"])


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-guidance.md"
    outside.write_text("outside\n", encoding="utf-8")
    guidance_path = tmp_path / ".ouroboros" / "guidance" / "team" / "GUIDANCE.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.symlink_to(outside)

    with pytest.raises(ConfigError, match="escapes"):
        resolve_execution_guidance(tmp_path, ["team"])


def test_rejects_non_utf8_content(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", b"\xff\xfe")

    with pytest.raises(ConfigError, match="UTF-8"):
        resolve_execution_guidance(tmp_path, ["team"])


@pytest.mark.parametrize("content", ["", "  \n\t"])
def test_rejects_empty_content(tmp_path: Path, content: str) -> None:
    _write_guidance(tmp_path, "team", content)

    with pytest.raises(ConfigError, match="empty|non-whitespace"):
        resolve_execution_guidance(tmp_path, ["team"])


def test_rejects_oversized_item(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", b"x" * (MAX_GUIDANCE_ITEM_BYTES + 1))

    with pytest.raises(ConfigError, match="per-item size limit"):
        resolve_execution_guidance(tmp_path, ["team"])


def test_oversized_item_is_rejected_before_content_read(tmp_path: Path) -> None:
    path = _write_guidance(tmp_path, "team", b"")
    with path.open("wb") as handle:
        handle.truncate(MAX_GUIDANCE_ITEM_BYTES + 1)

    original_open = Path.open

    class _ReadGuard:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Self:
            self._handle.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._handle.__exit__(*args)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, _size: int = -1) -> bytes:
            raise AssertionError("oversized guidance content must not be read")

    def guarded_open(target: Path, *args: Any, **kwargs: Any) -> _ReadGuard:
        return _ReadGuard(original_open(target, *args, **kwargs))

    with patch.object(Path, "open", new=guarded_open):
        with pytest.raises(ConfigError, match="per-item size limit"):
            resolve_execution_guidance(tmp_path, ["team"])


def test_guidance_content_read_is_bounded(tmp_path: Path) -> None:
    _write_guidance(tmp_path, "team", "bounded\n")
    original_open = Path.open
    read_sizes: list[int] = []

    class _ReadRecorder:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Self:
            self._handle.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._handle.__exit__(*args)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self._handle.read(size)

    def recording_open(target: Path, *args: Any, **kwargs: Any) -> _ReadRecorder:
        return _ReadRecorder(original_open(target, *args, **kwargs))

    with patch.object(Path, "open", new=recording_open):
        bundle = resolve_execution_guidance(tmp_path, ["team"])

    assert bundle.refs[0].guidance_id == "team"
    assert read_sizes == [MAX_GUIDANCE_ITEM_BYTES + 1]


def test_rejects_oversized_total(tmp_path: Path) -> None:
    content = b"x" * MAX_GUIDANCE_ITEM_BYTES
    for guidance_id in ("one", "two", "three"):
        _write_guidance(tmp_path, guidance_id, content)

    with pytest.raises(ConfigError, match="total size limit"):
        resolve_execution_guidance(tmp_path, ["one", "two", "three"])
