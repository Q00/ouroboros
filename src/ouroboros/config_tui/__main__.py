"""Run the settings app directly: ``python -m ouroboros.config_tui``.

This entry point is what the web mode serves (textual-serve runs it as a
subprocess), and it doubles as a debugging convenience.
"""

from ouroboros.config_tui.app import SettingsApp

if __name__ == "__main__":
    SettingsApp().run()
