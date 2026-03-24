#!/usr/bin/env python3
"""Sync .claude-plugin/ version fields with hatch-vcs (git tag) version.

Usage:
    python scripts/sync-plugin-version.py          # dry-run
    python scripts/sync-plugin-version.py --write  # actually update files

Called by CI (dev-publish.yml) before build to keep plugin metadata in sync.
"""

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
SETUP_SKILL_MD = ROOT / "skills" / "setup" / "SKILL.md"


def get_version() -> str:
    """Get version from hatch-vcs (same source as the Python package)."""
    # Try hatch first
    try:
        result = subprocess.run(
            ["hatch", "version"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Fallback: parse git describe like hatch-vcs does
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        desc = result.stdout.strip()  # e.g. v0.26.0b4-3-gabcdef
        # Strip leading 'v'
        desc = desc.lstrip("v")
        # For exact tag match, return as-is
        if "-" not in desc:
            return desc
        # For dev versions, extract base version
        base = desc.split("-")[0]
        return base
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    sys.exit("Error: cannot determine version (no hatch, no git tags)")


def normalize_version(v: str) -> str:
    """Normalize version for plugin metadata.

    Keeps pre-release tags (alpha/beta/rc) but strips dev suffixes.
    e.g. 0.26.0b4 -> 0.26.0b4, 0.26.0.dev3 -> 0.26.0, 0.26.0b4.dev1 -> 0.26.0b4
    """
    # Match semver + optional pre-release (a/alpha/b/beta/rc + number)
    m = re.match(r"(\d+\.\d+\.\d+(?:(?:a|alpha|b|beta|rc)\d*)?)", v)
    return m.group(1) if m else v


def update_version_marker(path: Path, version: str) -> bool:
    """Update <!-- ooo:VERSION:X.Y.Z --> marker in a text file."""
    text = path.read_text()
    new_text = re.sub(
        r"<!-- ooo:VERSION:[\d\w.]+ -->",
        f"<!-- ooo:VERSION:{version} -->",
        text,
    )
    if text == new_text:
        return False
    path.write_text(new_text)
    return True


def update_json(path: Path, version: str, *, nested_key: str | None = None) -> bool:
    """Update version in a JSON file. Returns True if changed."""
    data = json.loads(path.read_text())

    if nested_key:
        # marketplace.json: plugins[0].version
        target = data
        for key in nested_key.split("."):
            if key.isdigit():
                target = target[int(key)]
            else:
                target = target[key]
        old = target.get("version")
        target["version"] = version
    else:
        old = data.get("version")
        data["version"] = version

    if old == version:
        return False

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def main() -> None:
    write = "--write" in sys.argv

    # Allow explicit version override (e.g. --version 0.26.0b6)
    # Used by the release process to sync BEFORE tagging.
    explicit_version = None
    for i, arg in enumerate(sys.argv):
        if arg == "--version" and i + 1 < len(sys.argv):
            explicit_version = sys.argv[i + 1]

    raw_version = explicit_version or get_version()
    version = normalize_version(raw_version)

    print(f"Source version: {raw_version}")
    print(f"Plugin version: {version}")
    print()

    targets = [
        (PLUGIN_JSON, None),
        (MARKETPLACE_JSON, "plugins.0"),
    ]

    changed = False
    for path, nested in targets:
        if not path.exists():
            print(f"  SKIP  {path.relative_to(ROOT)} (not found)")
            continue

        data = json.loads(path.read_text())
        if nested:
            target = data
            for key in nested.split("."):
                target = target[int(key)] if key.isdigit() else target[key]
            old = target.get("version", "?")
        else:
            old = data.get("version", "?")

        if old == version:
            print(f"  OK    {path.relative_to(ROOT)} ({old})")
        elif write:
            update_json(path, version, nested_key=nested)
            print(f"  WRITE {path.relative_to(ROOT)} ({old} -> {version})")
            changed = True
        else:
            print(f"  DRIFT {path.relative_to(ROOT)} ({old} != {version})")
            changed = True

    # Sync setup SKILL.md template version marker
    if SETUP_SKILL_MD.exists():
        text = SETUP_SKILL_MD.read_text()
        marker_match = re.search(r"<!-- ooo:VERSION:([\d\w.]+) -->", text)
        old_marker = marker_match.group(1) if marker_match else "?"
        if old_marker == version:
            print(f"  OK    {SETUP_SKILL_MD.relative_to(ROOT)} ({old_marker})")
        elif write:
            update_version_marker(SETUP_SKILL_MD, version)
            print(f"  WRITE {SETUP_SKILL_MD.relative_to(ROOT)} ({old_marker} -> {version})")
            changed = True
        else:
            print(f"  DRIFT {SETUP_SKILL_MD.relative_to(ROOT)} ({old_marker} != {version})")
            changed = True

    if changed and not write:
        print("\nRun with --write to update files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
