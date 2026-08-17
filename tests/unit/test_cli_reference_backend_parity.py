"""Parity between documented backend lists and the enums the commands accept.

Each command declares its own `LLMBackend` enum, and the persisted config
declares its own literal set; none of them is the union in
`llm_backend_choices()`. Documenting from that union is how `hermes`, `gjc`,
and `ourocode` came to be advertised for commands that exit with code 2 on
them, and how `dsh` and `zcode` stayed missing from `mcp serve` after they
shipped.

A reader cannot tell a documented value from an accepted one, so the drift is
invisible until someone types the command. These tests read the shipped rows
and compare them to the enum the command is actually built from.
"""

from enum import StrEnum
from pathlib import Path
import re

import pytest

from ouroboros.cli.commands.init import LLMBackend as InitLLMBackend
from ouroboros.cli.commands.mcp import LLMBackend as McpLLMBackend
from ouroboros.config.models import LLMConfig

CLI_REFERENCE = Path("docs/cli-reference.md")
CONFIG_REFERENCE = Path("docs/config-reference.md")

# Heading each documented `--llm-backend` row belongs to -> the enum that row
# describes. A row under a heading not listed here is an unreviewed surface and
# fails the coverage test below rather than passing silently.
DOCUMENTED_SURFACES: dict[str, type[StrEnum]] = {
    "### `init start`": InitLLMBackend,
    "### `mcp serve`": McpLLMBackend,
    "### `mcp info`": McpLLMBackend,
}


def _rows_by_heading(document: Path) -> dict[str, list[str]]:
    """Group `--llm-backend` option rows under the nearest preceding heading."""
    grouped: dict[str, list[str]] = {}
    heading = ""
    for line in document.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            heading = line.strip()
        elif line.startswith("| `--llm-backend"):
            grouped.setdefault(heading, []).append(line)
    return grouped


def _documented_values(row: str) -> set[str]:
    """The backend names a row advertises, i.e. the backticked parenthesized list."""
    listed = re.search(r"\(((?:`[a-z_]+`(?:, )?)+)\)", row)
    assert listed is not None, f"no backend list found in row: {row}"
    return set(re.findall(r"`([a-z_]+)`", listed.group(1)))


@pytest.mark.parametrize("heading", sorted(DOCUMENTED_SURFACES))
def test_documented_llm_backends_match_the_command_enum(heading: str) -> None:
    rows = _rows_by_heading(CLI_REFERENCE).get(heading, [])
    assert rows, f"no `--llm-backend` row documented under {heading}"

    accepted = {member.value for member in DOCUMENTED_SURFACES[heading]}
    for row in rows:
        documented = _documented_values(row)
        assert documented == accepted, (
            f"{heading}: documented {sorted(documented)} but the command accepts "
            f"{sorted(accepted)} (advertised-but-rejected: "
            f"{sorted(documented - accepted)}; accepted-but-undocumented: "
            f"{sorted(accepted - documented)})"
        )


def test_every_documented_llm_backend_row_is_covered() -> None:
    # A new command documenting `--llm-backend` must be added to
    # DOCUMENTED_SURFACES, otherwise its list is never checked against anything.
    undocumented = set(_rows_by_heading(CLI_REFERENCE)) - set(DOCUMENTED_SURFACES)

    assert not undocumented, f"`--llm-backend` rows under unmapped headings: {sorted(undocumented)}"


def test_config_reference_llm_backend_matches_the_persisted_literals() -> None:
    # The persisted field is a superset of every CLI enum (it also carries
    # LLM-only backends no command flag exposes), so it needs its own check.
    row = next(
        line
        for line in CONFIG_REFERENCE.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `backend` |")
    )
    documented = set(re.findall(r'`"([a-z_]+)"`', row))

    accepted = set(LLMConfig.model_fields["backend"].annotation.__args__)  # type: ignore[union-attr]

    assert documented == accepted, (
        f"config reference documents {sorted(documented)} but `llm.backend` accepts "
        f"{sorted(accepted)}"
    )
