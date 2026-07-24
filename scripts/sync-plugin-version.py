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
BUNDLED_SETUP_SKILL_MD = ROOT / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
VERSION_MARKER_RE = re.compile(r"<!-- ooo:VERSION:([\d\w.]+) -->")


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

    # Fallback: parse git describe like hatch-vcs does.
    # dev-publish.yml intentionally runs this script before installing hatch,
    # so this branch must preserve the same next-dev source version that the
    # subsequent hatch-vcs package build will produce.
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return version_from_git_describe(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    sys.exit("Error: cannot determine version (no hatch, no git tags)")


def version_from_git_describe(desc: str) -> str:
    """Return a hatch-vcs compatible version from ``git describe`` output."""
    normalized = desc.removeprefix("v")
    match = re.fullmatch(r"(?P<base>.+)-(?P<distance>\d+)-g[0-9a-f]+(?:-dirty)?", normalized)
    if match is None:
        return normalized
    next_version = _guess_next_dev_base(match.group("base"))
    return f"{next_version}.dev{match.group('distance')}"


def _guess_next_dev_base(version: str) -> str:
    """Approximate hatch-vcs/setuptools-scm ``guess-next-dev`` for tags."""
    match = re.fullmatch(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)", version)
    if match is not None:
        patch = int(match.group("patch")) + 1
        return f"{match.group('major')}.{match.group('minor')}.{patch}"

    prerelease = re.fullmatch(
        r"(?P<prefix>\d+\.\d+\.\d+(?P<label>a|alpha|b|beta|rc))(?P<num>\d+)",
        version,
    )
    if prerelease is not None:
        return f"{prerelease.group('prefix')}{int(prerelease.group('num')) + 1}"

    return version


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
    matches = list(VERSION_MARKER_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one version marker in {path}")

    new_text = VERSION_MARKER_RE.sub(f"<!-- ooo:VERSION:{version} -->", text)
    if text == new_text:
        return False
    path.write_text(new_text)

    updated_matches = list(VERSION_MARKER_RE.finditer(path.read_text()))
    if len(updated_matches) != 1 or updated_matches[0].group(1) != version:
        raise RuntimeError(f"failed to verify version marker update in {path}")
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

    setup_markers: dict[Path, tuple[str, str]] = {}
    for path in (SETUP_SKILL_MD, BUNDLED_SETUP_SKILL_MD):
        if not path.exists():
            sys.exit(f"Error: required setup skill not found: {path.relative_to(ROOT)}")
        text = path.read_text()
        marker_matches = list(VERSION_MARKER_RE.finditer(text))
        if len(marker_matches) != 1:
            sys.exit(f"Error: expected exactly one version marker in {path.relative_to(ROOT)}")
        setup_markers[path] = (text, marker_matches[0].group(1))

    json_targets: list[tuple[Path, str | None, object, str]] = []
    for path, nested in targets:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise TypeError("top-level JSON value must be an object")
            target: object = data
            if nested:
                for key in nested.split("."):
                    if key.isdigit():
                        if not isinstance(target, list):
                            raise TypeError("numeric path component requires an array")
                        target = target[int(key)]
                    else:
                        if not isinstance(target, dict):
                            raise TypeError("named path component requires an object")
                        target = target[key]
                if not isinstance(target, dict):
                    raise TypeError("version target must be an object")
            old = target.get("version", "?")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            sys.exit(f"Error: could not validate {path.relative_to(ROOT)}: {exc}")
        json_targets.append((path, nested, data, str(old)))

    changed = False
    for path, nested, _data, old in json_targets:
        if not path.exists():
            print(f"  SKIP  {path.relative_to(ROOT)} (not found)")
            continue

        if old == version:
            print(f"  OK    {path.relative_to(ROOT)} ({old})")
        elif write:
            update_json(path, version, nested_key=nested)
            print(f"  WRITE {path.relative_to(ROOT)} ({old} -> {version})")
            changed = True
        else:
            print(f"  DRIFT {path.relative_to(ROOT)} ({old} != {version})")
            changed = True

    for path, (_text, old_marker) in setup_markers.items():
        if old_marker == version:
            print(f"  OK    {path.relative_to(ROOT)} ({old_marker})")
        elif write:
            updated = update_version_marker(path, version)
            if not updated:
                sys.exit(f"Error: failed to update {path.relative_to(ROOT)}")
            print(f"  WRITE {path.relative_to(ROOT)} ({old_marker} -> {version})")
            changed = True
        else:
            print(f"  DRIFT {path.relative_to(ROOT)} ({old_marker} != {version})")
            changed = True

    if changed and not write:
        print("\nRun with --write to update files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
