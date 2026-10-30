"""Go manifest lexical whitespace remains visible in context facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.orchestrator.context_pack import build_context_pack


@pytest.mark.parametrize("separator", ["\t", "\t ", " \t"])
def test_go_manifest_reports_tab_separated_directives(tmp_path: Path, separator: str) -> None:
    """Go's lexical whitespace permits tabs between directive and argument."""
    (tmp_path / "go.mod").write_text(
        f"module{separator}example.com/fixture\n\ngo{separator}1.23.0\n", encoding="utf-8"
    )

    pack = build_context_pack(tmp_path)

    assert pack is not None
    assert pack.stack == ("go.mod: module example.com/fixture, go 1.23.0",)


def test_go_manifest_keyword_matching_keeps_spaces_and_ignores_lookalikes(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "modulex example.com/wrong\ngoose 99.0\nmodule example.com/fixture\ngo 1.23.0\n",
        encoding="utf-8",
    )

    pack = build_context_pack(tmp_path)

    assert pack is not None
    assert pack.stack == ("go.mod: module example.com/fixture, go 1.23.0",)
