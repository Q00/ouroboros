"""Merge one ``codexModelMigration`` marker into ``~/.ouroboros/prefs.json``.

Run by the welcome skill after the legacy Codex model question, with the
marker as the only argument (``automatic-v1`` or ``kept-gpt-5-v1``)::

    ouroboros_python - "automatic-v1" < "$WELCOME_DIR/scripts/record_codex_model_migration.py"

Existing keys are kept; unreadable or non-object prefs start from ``{}``.
"""

import json
import os
import sys


def main() -> None:
    path = os.path.expanduser("~/.ouroboros/prefs.json")
    try:
        prefs = json.load(open(path, encoding="utf-8"))  # noqa: SIM115 - as the skill had it
    except Exception:
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    prefs["codexModelMigration"] = sys.argv[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
