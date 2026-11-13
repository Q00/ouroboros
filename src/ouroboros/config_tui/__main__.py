"""Run the terminal settings app directly: ``python -m ouroboros.config_tui``.

A debugging convenience; ``ouroboros config`` runs the same app in a terminal
and opens the browser console's settings page everywhere else.
"""

from ouroboros.config_tui.app import SettingsApp

if __name__ == "__main__":
    SettingsApp().run()
