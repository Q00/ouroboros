"""Merge the welcome completion fields into ``~/.ouroboros/prefs.json``.

Run by the welcome skill's Step 4, prefixed with ``STAR_ASKED=true`` when the
GitHub star question was asked::

    ouroboros_python - < "$WELCOME_DIR/scripts/record_welcome_completed.py"

Sets ``welcomeShown`` and a UTC ``welcomeCompleted`` timestamp, plus
``star_asked`` when ``STAR_ASKED=true``. Existing keys are kept; unreadable or
non-object prefs start from ``{}``.
"""

from datetime import UTC, datetime
import json
import os


def main() -> None:
    path = os.path.expanduser("~/.ouroboros/prefs.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            prefs = json.load(f)
        if not isinstance(prefs, dict):
            prefs = {}
    except Exception:
        prefs = {}
    prefs.update(
        {
            "welcomeShown": True,
            "welcomeCompleted": datetime.now(UTC).isoformat(),
        }
    )
    if os.environ.get("STAR_ASKED") == "true":
        prefs["star_asked"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
