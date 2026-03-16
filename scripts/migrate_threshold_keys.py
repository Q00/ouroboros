#!/usr/bin/env python3
"""
accuracy_threshold Consumer-Role Key Migration Script — Sub-AC 8-2 of AC 8
═══════════════════════════════════════════════════════════════════════════

Traverses claim records (and doc-topology accuracy_threshold blocks) in the
Ouroboros documentation registries and renames the deprecated consumer-role
key aliases introduced in v1.9 to their canonical v3.4 names:

  OLD key (v1.9–v3.3)  →  NEW canonical key (v3.4+)
  ─────────────────────────────────────────────────────
  human                →  human_reader
  agent                →  ai_agent

No value changes are made — only the key names are updated.

Background
──────────
Schema v3.4 (Sub-AC 8a of AC 8) renamed the two reserved consumer-role keys
inside ``accuracy_threshold`` objects from ``human`` / ``agent`` to the more
descriptive ``human_reader`` / ``ai_agent``.  Old keys remain accepted as
aliases with a WARNING (validation rule: consumer_role_old_key_deprecated)
to provide a backward-compatibility migration window.

This script closes that migration window by:
  1. Finding all claim records (record_type: claim) and doc-topology
     accuracy_threshold blocks that still use old key names.
  2. Renaming them in place using line-level text substitution (not YAML
     round-trip) so that comments and formatting are preserved.
  3. Reporting the results so authors can verify completeness.

Files scanned (actual data records only)
─────────────────────────────────────────
  docs/entity-registry.yaml         — record_type: claim entries (CLM-NNN)
  docs/multi-entity-registry.yaml   — record_type: claim entries (CLM-NNN)
  docs/claim-registry.yaml          — legacy claim entries
  docs/doc-topology.yaml            — genre accuracy_threshold blocks

Files explicitly excluded (intentional deprecated-key examples)
───────────────────────────────────────────────────────────────
  docs/multi-entity-registry-spec.yaml   — schema spec examples section
  docs/entity-registry-spec.yaml         — spec documentation
  docs/tests/accuracy-threshold-validation-tests.yaml — TEST-AT-012 deliberately
    uses old keys to test the consumer_role_old_key_deprecated WARNING rule

Usage
─────
  # Dry-run (shows what WOULD change, writes nothing):
  python scripts/migrate_threshold_keys.py --dry-run

  # Verify only (exit 1 if any old-format records found):
  python scripts/migrate_threshold_keys.py --verify

  # Apply migration in place:
  python scripts/migrate_threshold_keys.py

  # Specific file only:
  python scripts/migrate_threshold_keys.py --file docs/entity-registry.yaml

  # JSON report:
  python scripts/migrate_threshold_keys.py --dry-run --format json

Exit codes
──────────
  0  — migration completed / no old-format records found (verify mode OK)
  1  — old-format records found (non-zero in --verify or --dry-run mode)
  2  — internal error (YAML parse failure, missing file, etc.)

Backward compatibility
──────────────────────
  This script uses line-level text substitution (not full YAML round-trip)
  to avoid rewriting comment blocks.  Substitution rules:

    Within an ``accuracy_threshold:`` block only:
      Replace ``human:``  with ``human_reader:``
      Replace ``agent:``  with ``ai_agent:``

  A line is considered "within an accuracy_threshold block" if it appears
  after an ``accuracy_threshold:`` line and before the next same-indent or
  lower-indent non-empty, non-comment line.

  NOTE: The rename applies only when:
    - The key ``human`` or ``agent`` is the sole key on the line (not part
      of a longer word like ``human_reader`` or ``ai_agent``).
    - The line is indented more deeply than the ``accuracy_threshold:`` line.

Related documents
─────────────────
  docs/multi-entity-registry-spec.yaml — canonical accuracy_threshold schema (v3.4+)
  docs/multi-entity-migration-guide.md — §accuracy_threshold_v3b key migration guide
  docs/entity-registry-migration-guide.md — §13 accuracy_threshold key migration guide
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Try to import PyYAML — needed for analysis pass
# ---------------------------------------------------------------------------
try:
    import yaml  # type: ignore[import]  # noqa: F401
except ImportError:
    print(
        "ERROR: PyYAML is required.  Install it with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

# Files that contain actual claim/threshold records to migrate
DEFAULT_REGISTRY_FILES: list[Path] = [
    REPO_ROOT / "docs" / "entity-registry.yaml",
    REPO_ROOT / "docs" / "multi-entity-registry.yaml",
    REPO_ROOT / "docs" / "claim-registry.yaml",
    REPO_ROOT / "docs" / "doc-topology.yaml",
]

# Files that intentionally retain old keys (spec examples, test fixtures)
EXCLUDED_FILES: frozenset[str] = frozenset(
    [
        "multi-entity-registry-spec.yaml",
        "entity-registry-spec.yaml",
        "accuracy-threshold-validation-tests.yaml",
    ]
)

# Old key names → new canonical key names
KEY_RENAMES: dict[str, str] = {
    "human": "human_reader",
    "agent": "ai_agent",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ChangeRecord:
    """Records a single line-level change applied to a threshold block."""

    file: str
    line_number: int  # 1-based original line number
    context_id: str  # claim_id or doc_id providing context
    old_content: str  # original line text (stripped)
    new_content: str  # replacement line text (stripped)
    description: str  # human-readable explanation


@dataclass
class FileMigrationResult:
    """Aggregates all changes for a single file."""

    file_path: str
    total_threshold_blocks: int = 0
    changes: list[ChangeRecord] = dc_field(default_factory=list)
    parse_error: str | None = None

    @property
    def changed_blocks(self) -> int:
        """Number of distinct threshold blocks (by context_id) modified."""
        return len({c.context_id for c in self.changes})

    @property
    def blocks_already_compliant(self) -> int:
        return max(0, self.total_threshold_blocks - self.changed_blocks)


@dataclass
class MigrationReport:
    """Top-level migration report across all files."""

    run_date: str
    dry_run: bool
    verify_only: bool
    files: list[FileMigrationResult] = dc_field(default_factory=list)

    @property
    def total_threshold_blocks(self) -> int:
        return sum(f.total_threshold_blocks for f in self.files)

    @property
    def total_changes(self) -> int:
        return sum(len(f.changes) for f in self.files)

    @property
    def total_blocks_updated(self) -> int:
        return sum(f.changed_blocks for f in self.files)

    @property
    def total_already_compliant(self) -> int:
        return sum(f.blocks_already_compliant for f in self.files)

    @property
    def has_errors(self) -> bool:
        return any(f.parse_error for f in self.files)

    @property
    def old_format_found(self) -> bool:
        return self.total_changes > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date,
            "dry_run": self.dry_run,
            "verify_only": self.verify_only,
            "summary": {
                "total_files": len(self.files),
                "total_threshold_blocks": self.total_threshold_blocks,
                "total_blocks_updated": self.total_blocks_updated,
                "total_already_compliant": self.total_already_compliant,
                "total_line_changes": self.total_changes,
            },
            "old_format_found": self.old_format_found,
            "files": [
                {
                    "file": f.file_path,
                    "total_threshold_blocks": f.total_threshold_blocks,
                    "changed_blocks": f.changed_blocks,
                    "already_compliant": f.blocks_already_compliant,
                    "parse_error": f.parse_error,
                    "changes": [
                        {
                            "line": c.line_number,
                            "context_id": c.context_id,
                            "old": c.old_content,
                            "new": c.new_content,
                            "description": c.description,
                        }
                        for c in f.changes
                    ],
                }
                for f in self.files
            ],
        }


# ---------------------------------------------------------------------------
# Line-level scanning and transformation
# ---------------------------------------------------------------------------

# Matches an accuracy_threshold key line: captures leading whitespace
_RE_ACCURACY_THRESHOLD = re.compile(r"^(?P<indent>\s*)accuracy_threshold\s*:")

# Matches a sub-key of accuracy_threshold using old key name.
# Captures: indent, old_key (human|agent), rest of line
_RE_OLD_KEY = re.compile(r"^(?P<indent>\s+)(?P<old_key>human|agent)(?P<tail>\s*:.*)$")

# Matches a claim_id or doc_id line for context tracking
_RE_ID_LINE = re.compile(r'^\s*(?:-\s+)?(?:claim_id|doc_id)\s*:\s*["\']?(?P<id>[^\s"\'#]+)["\']?')

# Matches a non-empty, non-comment line for indent-level detection
_RE_CONTENT_LINE = re.compile(r"^(\s*)\S")


def _get_indent_level(line: str) -> int:
    """Return the number of leading spaces in a line."""
    m = _RE_CONTENT_LINE.match(line)
    return len(m.group(1)) if m else -1


def scan_and_transform_file(
    file_path: Path,
    dry_run: bool = True,
) -> FileMigrationResult:
    """
    Scan file_path for accuracy_threshold blocks using old key names.

    When dry_run=False, rewrite the file with the renamed keys.
    Returns a FileMigrationResult describing all changes.
    """
    result = FileMigrationResult(file_path=str(file_path))

    try:
        original_text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        result.parse_error = f"Read error: {exc}"
        return result

    lines = original_text.splitlines(keepends=True)
    new_lines: list[str] = []

    # State tracking
    in_threshold_block = False
    threshold_indent = -1  # indent level of the accuracy_threshold: line
    current_id = "unknown"  # most recently seen claim_id / doc_id

    for i, raw_line in enumerate(lines):
        line_no = i + 1
        # Strip trailing newline for analysis but preserve for output
        line = raw_line.rstrip("\n").rstrip("\r")

        # Track the most recent context identifier
        id_match = _RE_ID_LINE.match(line)
        if id_match:
            current_id = id_match.group("id")

        # Detect accuracy_threshold: line
        at_match = _RE_ACCURACY_THRESHOLD.match(line)
        if at_match:
            threshold_indent = len(at_match.group("indent"))
            in_threshold_block = True
            result.total_threshold_blocks += 1
            new_lines.append(raw_line)
            continue

        if in_threshold_block:
            # Check if we've exited the threshold block (same or lower indent)
            if line.strip() and not line.strip().startswith("#"):
                current_indent = _get_indent_level(line)
                if current_indent != -1 and current_indent <= threshold_indent:
                    in_threshold_block = False
                    threshold_indent = -1
                    # Fall through to normal processing

            if in_threshold_block:
                # Check if this line has an old key name
                old_key_match = _RE_OLD_KEY.match(line)
                if old_key_match:
                    old_key = old_key_match.group("old_key")
                    new_key = KEY_RENAMES.get(old_key)
                    if new_key:
                        indent_str = old_key_match.group("indent")
                        tail = old_key_match.group("tail")
                        new_line = f"{indent_str}{new_key}{tail}"

                        # Preserve original line ending
                        eol = ""
                        if raw_line.endswith("\r\n"):
                            eol = "\r\n"
                        elif raw_line.endswith("\n"):
                            eol = "\n"
                        elif raw_line.endswith("\r"):
                            eol = "\r"

                        result.changes.append(
                            ChangeRecord(
                                file=str(file_path),
                                line_number=line_no,
                                context_id=current_id,
                                old_content=line.strip(),
                                new_content=new_line.strip(),
                                description=(
                                    f"Renamed '{old_key}:' → '{new_key}:' "
                                    f"in accuracy_threshold block "
                                    f"(context: {current_id})"
                                ),
                            )
                        )

                        new_lines.append(new_line + eol)
                        continue

        new_lines.append(raw_line)

    # Write back if not dry_run and there are changes
    if not dry_run and result.changes:
        new_text = "".join(new_lines)
        file_path.write_text(new_text, encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _format_text_report(report: MigrationReport) -> str:
    """Format a human-readable text report."""
    lines: list[str] = []
    mode = "VERIFY" if report.verify_only else "DRY-RUN" if report.dry_run else "MIGRATE"
    lines.append(f"accuracy_threshold Key Migration Report — {mode}")
    lines.append(f"Run date : {report.run_date}")
    lines.append(f"Mode     : {mode}")
    lines.append("")
    lines.append("Summary")
    lines.append("───────")
    lines.append(f"  Files scanned          : {len(report.files)}")
    lines.append(f"  Threshold blocks found : {report.total_threshold_blocks}")
    lines.append(f"  Blocks needing rename  : {report.total_blocks_updated}")
    lines.append(f"  Already compliant      : {report.total_already_compliant}")
    lines.append(f"  Line-level changes     : {report.total_changes}")
    lines.append("")

    for f in report.files:
        lines.append(f"File: {f.file_path}")
        if f.parse_error:
            lines.append(f"  ERROR: {f.parse_error}")
            continue
        lines.append(f"  Threshold blocks : {f.total_threshold_blocks}")
        lines.append(f"  Already OK       : {f.blocks_already_compliant}")
        lines.append(f"  Blocks renamed   : {f.changed_blocks}")
        if f.changes:
            for c in f.changes:
                lines.append(
                    f"  L{c.line_number:4d} [{c.context_id}]: {c.old_content!r} → {c.new_content!r}"
                )
        lines.append("")

    if report.old_format_found:
        action = (
            "detected (no write in verify/dry-run mode)"
            if report.dry_run or report.verify_only
            else "renamed in place"
        )
        lines.append(f"RESULT: {report.total_changes} old-format key(s) {action}.")
    else:
        lines.append(
            "RESULT: No old-format accuracy_threshold keys found — "
            "all records are compliant with v3.4+ canonical key names."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rename deprecated accuracy_threshold consumer-role keys "
            "(human → human_reader, agent → ai_agent) across registry files."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would change without writing files.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help=(
            "Verify mode: exit 1 if any old-format keys are found, "
            "exit 0 if all records are compliant.  Implies --dry-run."
        ),
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Scan/migrate a specific file only (overrides default file list).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output report format (default: text).",
    )
    parser.add_argument(
        "--report",
        metavar="OUTPUT_FILE",
        help="Write report to this file (default: stdout).",
    )
    args = parser.parse_args(argv)

    verify_only: bool = args.verify
    dry_run: bool = args.dry_run or verify_only  # verify implies dry-run

    # Determine files to scan
    if args.file:
        files_to_scan = [Path(args.file)]
    else:
        files_to_scan = DEFAULT_REGISTRY_FILES

    # Check for explicitly excluded files
    effective_files: list[Path] = []
    for fp in files_to_scan:
        if fp.name in EXCLUDED_FILES:
            print(
                f"INFO: Skipping {fp.name} (intentional deprecated-key examples file).",
                file=sys.stderr,
            )
        else:
            effective_files.append(fp)

    report = MigrationReport(
        run_date=str(date.today()),
        dry_run=dry_run,
        verify_only=verify_only,
    )

    for file_path in effective_files:
        if not file_path.exists():
            r = FileMigrationResult(file_path=str(file_path))
            r.parse_error = "File not found"
            report.files.append(r)
            continue
        result = scan_and_transform_file(file_path, dry_run=dry_run)
        report.files.append(result)

    # Format report
    if args.format == "json":
        output = json.dumps(report.as_dict(), indent=2)
    else:
        output = _format_text_report(report)

    if args.report:
        Path(args.report).write_text(output, encoding="utf-8")
        print(f"Report written to {args.report}", file=sys.stderr)
    else:
        print(output)

    # Exit code
    if report.has_errors:
        return 2
    if verify_only and report.old_format_found:
        return 1
    if dry_run and report.old_format_found:
        # Dry-run found items that need migration → exit 1 to signal action needed
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
