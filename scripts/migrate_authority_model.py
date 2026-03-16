#!/usr/bin/env python3
"""
authority_model Enum Migration Script — Sub-AC 2 of AC 1
═════════════════════════════════════════════════════════

Traverses all claim records in the Ouroboros documentation registries and
normalises the ``authority_model`` field to the canonical three-value enum
introduced in Sub-AC 1 of AC 1 (multi-entity registry strengthening):

  authored_descriptive  — human author describes code behaviour
  authored_derived      — human author curates code-derived content
  generated             — pipeline fully produces document content

Transformations applied
───────────────────────
  OLD field / value                         → NEW value
  ─────────────────────────────────────────────────────
  authority_model: authored                 → authored_descriptive
  authority_model: descriptive              → authored_descriptive
  code_deps_relationship: descriptive       → authority_model: authored_descriptive
                                              (code_deps_relationship field REMOVED)
  code_dep_direction: descriptive           → authority_model: authored_descriptive
                                              (code_dep_direction field REMOVED)
  authority_model: generative               → generated
  code_deps_relationship: generative        → authority_model: generated
                                              (code_deps_relationship field REMOVED)
  code_dep_direction: generative            → authority_model: generated
                                              (code_dep_direction field REMOVED)
  authority_model absent (claim record)     → authority_model: authored_descriptive
                                              (added explicitly)
  authority_model: authored_descriptive     → no change
  authority_model: authored_derived         → no change
  authority_model: generated                → no change

Files scanned (claim records only)
───────────────────────────────────
  docs/claim-registry.yaml        — legacy CR-NNN format (claim_id field)
  docs/multi-entity-registry.yaml — record_type: claim entries (CLM-NNN)
  docs/entity-registry.yaml       — record_type: claim entries (CLM-NNN)

Usage
─────
  # Dry-run (shows what WOULD change, writes nothing):
  python scripts/migrate_authority_model.py --dry-run

  # Apply migration:
  python scripts/migrate_authority_model.py

  # Specific file only:
  python scripts/migrate_authority_model.py --file docs/claim-registry.yaml

  # Output report to file:
  python scripts/migrate_authority_model.py --report report.txt

  # JSON report:
  python scripts/migrate_authority_model.py --dry-run --format json

Exit codes
──────────
  0  — migration completed (or dry-run showed no issues); all records valid
  1  — migration required changes (non-zero records updated in --dry-run mode)
  2  — internal error (YAML parse failure, missing file, etc.)

Backward compatibility
──────────────────────
  This script PRESERVES all other fields and comments.  It uses line-level
  text substitution (not full YAML round-trip) to avoid rewriting comment
  blocks.  The substitution rules are:
    - Replace ``authority_model: <old>`` with ``authority_model: <new>``
    - Replace ``code_deps_relationship: <value>`` with ``authority_model: <new>``
    - Add ``authority_model: authored_descriptive`` after the ``claim_id:`` line
      for entries that have a claim_id but no authority_model.

Related documents
─────────────────
  docs/multi-entity-registry-spec.yaml  — canonical field definitions
  docs/multi-entity-migration-guide.md  — §8: Sub-AC 2 data migration guide
  docs/claim-registry-spec.yaml         — claim record schema
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
    import yaml  # type: ignore[import]
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

# Files that contain claim records
DEFAULT_REGISTRY_FILES: list[Path] = [
    REPO_ROOT / "docs" / "claim-registry.yaml",
    REPO_ROOT / "docs" / "multi-entity-registry.yaml",
    REPO_ROOT / "docs" / "entity-registry.yaml",
]

# Canonical enum values — no migration needed
CANONICAL_VALUES: frozenset[str] = frozenset(
    ["authored_descriptive", "authored_derived", "generated"]
)

# Old authority_model values that map to authored_descriptive
ALIAS_TO_AUTHORED_DESCRIPTIVE: frozenset[str] = frozenset(["authored", "descriptive"])

# Old authority_model values that map to generated
ALIAS_TO_GENERATED: frozenset[str] = frozenset(["generative"])

# Deprecated field names whose VALUE implies an authority_model
DEPRECATED_FIELDS: dict[str, dict[str, str]] = {
    # field_name → { old_value → new_authority_model_value }
    "code_deps_relationship": {
        "descriptive": "authored_descriptive",
        "generative": "generated",
        "authored": "authored_descriptive",
    },
    "code_dep_direction": {
        "descriptive": "authored_descriptive",
        "generative": "generated",
        "authored": "authored_descriptive",
    },
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ChangeRecord:
    """Records a single field-level change applied to a claim record."""

    file: str
    line_number: int  # 1-based original line number
    claim_id: str
    change_type: str  # "updated_value" | "added_field" | "replaced_deprecated"
    old_content: str  # original line text (stripped)
    new_content: str  # replacement line text (stripped); "(injected)" if added
    description: str  # human-readable explanation


@dataclass
class FileMigrationResult:
    """Aggregates all changes for a single file."""

    file_path: str
    total_claim_records: int = 0
    changes: list[ChangeRecord] = dc_field(default_factory=list)
    parse_error: str | None = None

    @property
    def changed_records(self) -> int:
        """Number of distinct claim IDs that were modified."""
        return len({c.claim_id for c in self.changes})

    @property
    def records_already_compliant(self) -> int:
        return max(0, self.total_claim_records - self.changed_records)


@dataclass
class MigrationReport:
    """Top-level migration report across all files."""

    run_date: str
    dry_run: bool
    files: list[FileMigrationResult] = dc_field(default_factory=list)

    @property
    def total_claim_records(self) -> int:
        return sum(f.total_claim_records for f in self.files)

    @property
    def total_changes(self) -> int:
        return sum(len(f.changes) for f in self.files)

    @property
    def total_records_updated(self) -> int:
        return sum(f.changed_records for f in self.files)

    @property
    def total_already_compliant(self) -> int:
        return sum(f.records_already_compliant for f in self.files)

    @property
    def has_errors(self) -> bool:
        return any(f.parse_error for f in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date,
            "dry_run": self.dry_run,
            "summary": {
                "total_files": len(self.files),
                "total_claim_records": self.total_claim_records,
                "total_records_updated": self.total_records_updated,
                "total_already_compliant": self.total_already_compliant,
                "total_line_changes": self.total_changes,
            },
            "files": [
                {
                    "file": f.file_path,
                    "total_claim_records": f.total_claim_records,
                    "changed_records": f.changed_records,
                    "already_compliant": f.records_already_compliant,
                    "parse_error": f.parse_error,
                    "changes": [
                        {
                            "line": c.line_number,
                            "claim_id": c.claim_id,
                            "change_type": c.change_type,
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
# YAML analysis pass — identify claim records
# ---------------------------------------------------------------------------


def _collect_claim_records_yaml(
    text: str,
    is_legacy: bool,
) -> list[dict[str, Any]]:
    """
    Parse the YAML text and return a list of claim record dicts.

    For legacy claim-registry.yaml (is_legacy=True): every entry in the
    top-level 'entries' list is a claim record (identified by 'claim_id').

    For multi-entity registries (is_legacy=False): only entries where
    record_type == 'claim' are returned.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []

    if not isinstance(doc, dict):
        return []

    # Legacy format: top-level 'entries' list
    if is_legacy:
        entries = doc.get("entries", [])
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and "claim_id" in e]

    # Multi-entity format: single top-level list
    if isinstance(doc.get("entries"), list):
        entries = doc["entries"]
    elif isinstance(doc, list):
        entries = doc
    else:
        # Try to collect all lists at any key
        entries = []
        for v in doc.values():
            if isinstance(v, list):
                entries.extend(v)

    return [e for e in entries if isinstance(e, dict) and e.get("record_type") == "claim"]


def _needs_migration(record: dict[str, Any]) -> dict[str, str]:
    """
    Analyse a single claim record dict and return a dict describing the
    migration needed.  Empty dict means no migration required.

    Keys in the returned dict:
      'authority_model_old'   — old value (or 'absent')
      'authority_model_new'   — new canonical value
      'deprecated_field'      — name of deprecated field to remove (if any)
      'deprecated_value'      — value of the deprecated field (if any)
    """
    result: dict[str, str] = {}

    existing_am = record.get("authority_model")
    # Check deprecated fields (code_deps_relationship, code_dep_direction)
    deprecated_field = None
    deprecated_value = None
    for df in DEPRECATED_FIELDS:
        if df in record:
            deprecated_field = df
            deprecated_value = str(record[df])
            break

    if existing_am is not None:
        am_str = str(existing_am)
        if am_str in CANONICAL_VALUES:
            # Already canonical — only remove deprecated field if present
            if deprecated_field:
                result["authority_model_old"] = am_str
                result["authority_model_new"] = am_str  # no value change
                result["deprecated_field"] = deprecated_field
                result["deprecated_value"] = deprecated_value or ""
        elif am_str in ALIAS_TO_AUTHORED_DESCRIPTIVE:
            result["authority_model_old"] = am_str
            result["authority_model_new"] = "authored_descriptive"
            if deprecated_field:
                result["deprecated_field"] = deprecated_field
                result["deprecated_value"] = deprecated_value or ""
        elif am_str in ALIAS_TO_GENERATED:
            result["authority_model_old"] = am_str
            result["authority_model_new"] = "generated"
            if deprecated_field:
                result["deprecated_field"] = deprecated_field
                result["deprecated_value"] = deprecated_value or ""
        # else: unknown value — leave as-is
    else:
        # authority_model absent
        if deprecated_field and deprecated_value is not None:
            # Derive from deprecated field value
            canonical = DEPRECATED_FIELDS[deprecated_field].get(deprecated_value)
            if canonical:
                result["authority_model_old"] = "absent"
                result["authority_model_new"] = canonical
                result["deprecated_field"] = deprecated_field
                result["deprecated_value"] = deprecated_value
        else:
            # Absent with no deprecated field — default to authored_descriptive
            result["authority_model_old"] = "absent"
            result["authority_model_new"] = "authored_descriptive"

    return result


# ---------------------------------------------------------------------------
# Text transformation pass
# ---------------------------------------------------------------------------

# Regex patterns (handle both "  - field: val" and "    field: val" forms)
_RE_AUTHORITY_MODEL_LINE = re.compile(
    r'^(?P<prefix>\s*(?:-\s+)?)authority_model:\s*(?P<value>[^\s#"\']+)["\']?'
    r"(?P<tail>.*)$"
)
_RE_CLAIM_ID_LINE = re.compile(
    r'^(?P<prefix>\s*(?:-\s+)?)claim_id:\s*["\']?(?P<id>[A-Z]{2,5}-\d+)["\']?'
)
_RE_DEPRECATED_FIELD_LINE = re.compile(
    r"^(?P<prefix>\s*)(?P<field>code_deps_relationship|code_dep_direction):\s*"
    r'["\']?(?P<value>[^\s#"\']+)["\']?(?P<tail>.*)$'
)


def _find_claim_id_line_index(
    lines: list[str],
    claim_id: str,
    start: int = 0,
) -> int:
    """
    Return the 0-based index of the line containing ``claim_id: <claim_id>``
    in ``lines``, starting the search at ``start``.  Returns -1 if not found.
    """
    pattern = re.compile(r'(?:\s*-\s+|^\s+)claim_id:\s*["\']?' + re.escape(claim_id) + r'["\']?')
    for i in range(start, len(lines)):
        if pattern.search(lines[i]):
            return i
    return -1


def _find_authority_model_line_index(
    lines: list[str],
    start: int,
    end: int,
) -> int:
    """
    Return the 0-based index of the ``authority_model:`` line in lines[start:end].
    Returns -1 if not found.
    """
    for i in range(start, end):
        if _RE_AUTHORITY_MODEL_LINE.match(lines[i]):
            return i
    return -1


def _find_deprecated_field_line_index(
    lines: list[str],
    field_name: str,
    start: int,
    end: int,
) -> int:
    """Find a deprecated field line in lines[start:end]. Returns -1 if absent."""
    pat = re.compile(r"^\s*(?:-\s+)?" + re.escape(field_name) + r':\s*["\']?')
    for i in range(start, end):
        if pat.match(lines[i]):
            return i
    return -1


def _next_record_start(lines: list[str], after: int) -> int:
    """
    Return the index of the NEXT top-level YAML list item after index ``after``.
    Top-level list items match: ``^  - `` or ``^ - `` (1-3 space indent + dash + space).
    Returns len(lines) if none found.
    """
    # A record boundary is a line that starts a new YAML list entry at
    # indent level 0-3 spaces (the "  - " prefix is 2 spaces in these files)
    rec_pat = re.compile(r"^\s{0,3}-\s")
    for i in range(after + 1, len(lines)):
        if rec_pat.match(lines[i]) and not re.match(r"^\s{4,}", lines[i]):
            return i
    return len(lines)


def _get_field_indent(claim_id_line: str) -> str:
    """
    Derive the indent for sibling fields from the claim_id line.
    For "  - claim_id: ..." → indent is "    " (4 spaces).
    For "    claim_id: ..."  → indent is "    " (the existing indent).
    """
    stripped = claim_id_line.lstrip()
    total_indent = len(claim_id_line) - len(stripped)
    if stripped.startswith("- "):
        # List item: fields are at indent + 2 (for "- ")
        return " " * (total_indent + 2)
    return " " * total_indent


def apply_migration_to_lines(
    lines: list[str],
    claim_id: str,
    migration_info: dict[str, str],
    result: FileMigrationResult,
    start_hint: int = 0,
) -> list[str]:
    """
    Apply the migration described by ``migration_info`` to ``lines`` for
    the record identified by ``claim_id``.

    Returns the (possibly modified) lines list.
    """
    # Find the claim_id line
    cid_idx = _find_claim_id_line_index(lines, claim_id, start_hint)
    if cid_idx == -1:
        return lines  # not found — skip

    # Find the end of this record (next top-level list item or EOF)
    rec_end = _next_record_start(lines, cid_idx)

    am_old = migration_info.get("authority_model_old", "")
    am_new = migration_info.get("authority_model_new", "")
    dep_field = migration_info.get("deprecated_field", "")
    dep_value = migration_info.get("deprecated_value", "")

    new_lines = list(lines)  # mutable copy
    offset = 0  # cumulative insertion offset

    # ── Case 1: deprecated field present → replace it with authority_model ──
    if dep_field:
        dep_idx = _find_deprecated_field_line_index(
            new_lines, dep_field, cid_idx + offset, rec_end + offset
        )
        if dep_idx != -1:
            old_line = new_lines[dep_idx]
            indent = _get_field_indent(new_lines[cid_idx + offset])
            new_line = f"{indent}authority_model: {am_new}\n"
            change = ChangeRecord(
                file=result.file_path,
                line_number=dep_idx + 1,
                claim_id=claim_id,
                change_type="replaced_deprecated",
                old_content=old_line.rstrip(),
                new_content=new_line.rstrip(),
                description=(f"{dep_field}: {dep_value} → authority_model: {am_new}"),
            )
            result.changes.append(change)
            new_lines[dep_idx] = new_line
            # If authority_model also existed with a non-canonical value, fix it
            am_idx = _find_authority_model_line_index(new_lines, cid_idx + offset, rec_end + offset)
            if am_idx != -1:
                old_am_line = new_lines[am_idx]
                am_match = _RE_AUTHORITY_MODEL_LINE.match(old_am_line)
                if am_match and am_match.group("value") not in CANONICAL_VALUES:
                    new_am_line = f"{indent}authority_model: {am_new}\n"
                    chg2 = ChangeRecord(
                        file=result.file_path,
                        line_number=am_idx + 1,
                        claim_id=claim_id,
                        change_type="updated_value",
                        old_content=old_am_line.rstrip(),
                        new_content=new_am_line.rstrip(),
                        description=(
                            f"authority_model: {am_match.group('value')} → {am_new} "
                            f"(consolidated with {dep_field} removal)"
                        ),
                    )
                    result.changes.append(chg2)
                    new_lines[am_idx] = new_am_line
            return new_lines

    # ── Case 2: authority_model present with non-canonical value → update ───
    if am_old not in ("absent", "") and am_old != am_new:
        am_idx = _find_authority_model_line_index(new_lines, cid_idx + offset, rec_end + offset)
        if am_idx != -1:
            old_line = new_lines[am_idx]
            am_match = _RE_AUTHORITY_MODEL_LINE.match(old_line)
            if am_match:
                prefix = am_match.group("prefix")
                tail = am_match.group("tail") or ""
                inline_comment = ""
                if "#" in tail:
                    ci = tail.index("#")
                    inline_comment = "  " + tail[ci:].rstrip()
                new_line = f"{prefix}authority_model: {am_new}{inline_comment}\n"
                change = ChangeRecord(
                    file=result.file_path,
                    line_number=am_idx + 1,
                    claim_id=claim_id,
                    change_type="updated_value",
                    old_content=old_line.rstrip(),
                    new_content=new_line.rstrip(),
                    description=(f"authority_model: {am_old} → {am_new}"),
                )
                result.changes.append(change)
                new_lines[am_idx] = new_line
        return new_lines

    # ── Case 3: authority_model absent → inject after claim_id line ─────────
    if am_old == "absent":
        indent = _get_field_indent(new_lines[cid_idx + offset])
        injection = f"{indent}authority_model: {am_new}\n"
        insert_pos = cid_idx + offset + 1
        change = ChangeRecord(
            file=result.file_path,
            line_number=cid_idx + 2,  # line after claim_id (1-based approx)
            claim_id=claim_id,
            change_type="added_field",
            old_content="(absent)",
            new_content=injection.rstrip(),
            description=(
                f"authority_model absent → added authority_model: {am_new} "
                f"(default for claims without explicit authority_model)"
            ),
        )
        result.changes.append(change)
        new_lines.insert(insert_pos, injection)
        return new_lines

    return new_lines


# ---------------------------------------------------------------------------
# Main migration function
# ---------------------------------------------------------------------------


def migrate_file(
    file_path: Path,
    dry_run: bool = True,
) -> FileMigrationResult:
    """
    Migrate a single registry YAML file.

    Two-pass strategy:
    1. YAML parse to identify all claim records and their migration needs.
    2. Line-level text substitution to apply changes (preserves comments).

    For *claim-registry.yaml* (is_legacy=True): every entry in 'entries'
    is a claim record (no record_type discriminator).

    For *multi-entity-registry.yaml* and *entity-registry.yaml*: only
    entries with ``record_type: claim`` are processed.
    """
    result = FileMigrationResult(file_path=str(file_path))

    if not file_path.exists():
        result.parse_error = f"File not found: {file_path}"
        return result

    try:
        original_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.parse_error = str(exc)
        return result

    is_legacy = file_path.name == "claim-registry.yaml"

    # ── Pass 1: YAML analysis ──────────────────────────────────────────────
    claim_records = _collect_claim_records_yaml(original_text, is_legacy)
    result.total_claim_records = len(claim_records)

    # Build migration plan: claim_id → migration_info
    migration_plan: dict[str, dict[str, str]] = {}
    for rec in claim_records:
        cid = str(rec.get("claim_id", ""))
        if not cid:
            continue
        info = _needs_migration(rec)
        if info:
            migration_plan[cid] = info

    if not migration_plan:
        return result  # nothing to do

    # ── Pass 2: Text substitution ──────────────────────────────────────────
    lines = original_text.splitlines(keepends=True)

    if dry_run:
        # In dry-run mode: still collect change records for reporting,
        # but work on a scratch copy so we don't lose the offset book-keeping
        # (insertions shift line indices).
        scratch_lines = list(lines)
        for cid, info in migration_plan.items():
            # Find approximate start of this claim record in scratch_lines
            start_pos = 0
            scratch_lines = apply_migration_to_lines(scratch_lines, cid, info, result, start_pos)
        # Restore result.changes to have correct descriptions but NOT write
    else:
        # Apply in-place
        working_lines = list(lines)
        for cid, info in migration_plan.items():
            working_lines = apply_migration_to_lines(working_lines, cid, info, result, 0)

        if result.changes:
            new_text = "".join(working_lines)
            file_path.write_text(new_text, encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_text_report(report: MigrationReport, verbose: bool = False) -> str:
    lines: list[str] = []
    mode = "DRY-RUN (no files written)" if report.dry_run else "APPLIED"
    lines.append("=" * 72)
    lines.append(f"  authority_model Migration Report  [{mode}]")
    lines.append(f"  Date: {report.run_date}")
    lines.append("=" * 72)
    lines.append("")

    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Files scanned:              {len(report.files)}")
    lines.append(f"  Total claim records:        {report.total_claim_records}")
    lines.append(f"  Records already compliant:  {report.total_already_compliant}")
    lines.append(f"  Records requiring changes:  {report.total_records_updated}")
    lines.append(f"  Total line-level changes:   {report.total_changes}")
    if report.dry_run and report.total_records_updated > 0:
        lines.append("")
        lines.append("  NOTE: Dry-run mode — no files were modified.")
        lines.append("        Re-run without --dry-run to apply changes.")
    elif not report.dry_run and report.total_records_updated > 0:
        lines.append("")
        lines.append("  Files UPDATED. Validate with:")
        lines.append("    python scripts/validate_multi_entity_registry.py")
    elif report.total_records_updated == 0:
        lines.append("")
        lines.append("  All claim records are already compliant. No changes needed.")
    lines.append("")

    for fres in report.files:
        lines.append(f"FILE: {fres.file_path}")
        lines.append("-" * 60)
        if fres.parse_error:
            lines.append(f"  ERROR: {fres.parse_error}")
            lines.append("")
            continue
        lines.append(f"  Claim records found:    {fres.total_claim_records}")
        lines.append(f"  Already compliant:      {fres.records_already_compliant}")
        lines.append(f"  Records with changes:   {fres.changed_records}")
        lines.append(f"  Line changes:           {len(fres.changes)}")

        if fres.changes:
            shown = fres.changes if verbose else fres.changes[:10]
            lines.append("")
            header = "  All changes:" if verbose else "  Changes (first 10; use --verbose for all):"
            lines.append(header)
            for chg in shown:
                type_label = {
                    "added_field": "ADD",
                    "updated_value": "UPD",
                    "replaced_deprecated": "REP",
                }.get(chg.change_type, chg.change_type.upper())
                lines.append(f"    [{type_label}] {chg.claim_id} (line ~{chg.line_number})")
                lines.append(f"         {chg.description}")
                if verbose:
                    if chg.change_type == "added_field":
                        lines.append(f"         + {chg.new_content}")
                    else:
                        lines.append(f"         - {chg.old_content}")
                        if chg.new_content:
                            lines.append(f"         + {chg.new_content}")
            if not verbose and len(fres.changes) > 10:
                lines.append(f"    ... and {len(fres.changes) - 10} more (use --verbose)")
        lines.append("")

    lines.append("VERIFICATION CHECKLIST")
    lines.append("-" * 40)
    lines.append("  After applying the migration, verify compliance:")
    lines.append("")
    lines.append("  [ ] python scripts/validate_multi_entity_registry.py")
    lines.append("        → Zero code_deps_relationship_deprecated WARNINGs")
    lines.append("        → Zero authority_model_deprecated_authored_alias WARNINGs")
    lines.append("")
    lines.append("  Manual spot-checks (should all return zero matches):")
    lines.append("  [ ] grep -n 'authority_model: authored\\b' docs/*.yaml")
    lines.append("  [ ] grep -n 'authority_model: descriptive' docs/*.yaml")
    lines.append("  [ ] grep -n 'authority_model: generative' docs/*.yaml")
    lines.append("  [ ] grep -Pn '^\\s+code_deps_relationship:' docs/claim-registry.yaml")
    lines.append("        (comments with # are acceptable)")
    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_json_report(report: MigrationReport) -> str:
    return json.dumps(report.as_dict(), indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate authority_model field values in Ouroboros claim records "
            "(Sub-AC 2, AC 1: multi-entity registry strengthening)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview changes without writing (safe):
  python scripts/migrate_authority_model.py --dry-run

  # Apply migration to all registry files:
  python scripts/migrate_authority_model.py

  # Migrate a single file:
  python scripts/migrate_authority_model.py --file docs/claim-registry.yaml

  # JSON report:
  python scripts/migrate_authority_model.py --dry-run --format json

  # Verbose text report to file:
  python scripts/migrate_authority_model.py --dry-run --verbose --report report.txt
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would change without writing any files.",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        action="append",
        dest="files",
        help=(
            "Registry YAML file to process.  May be repeated.  Default: all three registry files."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Report output format (default: text).",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Also write report to this file.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show all individual changes in the text report.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress stdout; only write to --report (if given).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    registry_files = [Path(p) for p in args.files] if args.files else DEFAULT_REGISTRY_FILES

    report = MigrationReport(run_date=str(date.today()), dry_run=args.dry_run)

    for fp in registry_files:
        res = migrate_file(fp, dry_run=args.dry_run)
        report.files.append(res)
        if res.parse_error:
            print(f"ERROR: {res.parse_error}", file=sys.stderr)

    report_text = (
        format_json_report(report)
        if args.format == "json"
        else format_text_report(report, verbose=args.verbose)
    )

    if not args.quiet:
        print(report_text)

    if args.report:
        rp = Path(args.report)
        rp.write_text(report_text, encoding="utf-8")
        if not args.quiet:
            print(f"\nReport written to: {rp}")

    if report.has_errors:
        return 2
    if args.dry_run and report.total_records_updated > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
