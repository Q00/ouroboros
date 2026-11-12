"""Atomically rewrite the four legacy ``gpt-5`` stage-model pins to ``default``.

Run by the welcome skill's legacy Codex model migration, with the config path
as the only argument::

    ouroboros_python - "$HOME/.ouroboros/config.yaml" < "$WELCOME_DIR/scripts/migrate_codex_models.py"

Exits non-zero with a message, and leaves the file unchanged, when any of the
four keys is missing.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile


def main() -> None:
    path = Path(sys.argv[1])
    original = path.read_text(encoding="utf-8")
    replacements = {
        ("clarification", "default_model"): "default",
        ("execution", "default_model"): "default",
        ("evaluation", "semantic_model"): "default",
        ("resilience", "reflect_model"): "default",
    }
    seen: set[tuple[str, str]] = set()
    section: str | None = None
    output: list[str] = []
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        key = stripped.split(":", 1)[0].strip("'\"") if ":" in stripped else ""
        if indent == 0 and ":" in stripped:
            section = key
        target = (section or "", key)
        if indent > 0 and target in replacements:
            newline = "\n" if line.endswith("\n") else ""
            prefix = line[:indent]
            comment = ""
            value_part = line.strip().split(":", 1)[1]
            if " #" in value_part:
                comment = " #" + value_part.split(" #", 1)[1].rstrip("\n")
            output.append(f"{prefix}{key}: {replacements[target]}{comment}{newline}")
            seen.add(target)
        else:
            output.append(line)
    missing = set(replacements) - seen
    if missing:
        raise SystemExit(f"Cannot migrate Codex model pins; missing keys: {sorted(missing)}")
    updated = "".join(output)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.yaml.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(updated)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
