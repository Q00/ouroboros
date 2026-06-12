"""Standalone settings GUI for Ouroboros configuration (#1411).

This package is deliberately independent from :mod:`ouroboros.tui` (the
monitor TUI): importing it must not pull in ``OuroborosTUI``, so a separate
consumer (ourocode) can embed the settings app on its own. Keep this
``__init__`` free of heavy imports — the Textual app lives in
:mod:`ouroboros.config_tui.app` and is imported lazily by the launcher.
"""
