"""Managed ``ooo`` frontdoor bridge installers for CLI runtimes.

Ouroboros ships managed bridge extensions that route exact-prefix ``ooo ...``
inputs from interactive coding-agent CLIs into the shared skill dispatcher.
The TypeScript renderers live beside their installers in
:mod:`ouroboros.cli.commands.pi_bridge` and
:mod:`ouroboros.cli.commands.omp_bridge`; this module owns the shared
install-site concerns (launcher detection, hash-compared writes) so
:mod:`ouroboros.cli.commands.setup` stays within its module-size budget.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ouroboros.cli.commands.omp_bridge import omp_ooo_bridge_source_text
from ouroboros.cli.commands.pi_bridge import pi_ooo_bridge_source_text
from ouroboros.cli.formatters.panels import print_info, print_success, print_warning

PI_OOO_BRIDGE_FILENAME = "ouroboros-ooo-bridge.ts"
OMP_OOO_BRIDGE_FILENAME = "ouroboros-ooo-bridge.ts"


def detect_bridge_dispatch_entry() -> tuple[str, list[str]]:
    """Return the launcher a managed bridge should use for this install."""
    # Deferred import: setup imports this module at load time, so importing
    # setup back must stay call-scoped.
    from importlib import metadata as importlib_metadata
    import shutil
    import sys

    from ouroboros.cli.commands.setup import _is_source_tree_ouroboros_build

    if _is_source_tree_ouroboros_build():
        return sys.executable, ["-m", "ouroboros"]
    try:
        version = importlib_metadata.version("ouroboros-ai")
    except importlib_metadata.PackageNotFoundError:
        return sys.executable, ["-m", "ouroboros"]
    if ".dev" in version:
        return sys.executable, ["-m", "ouroboros"]
    return shutil.which("ouroboros") or "ouroboros", []


def _atomic_write_text(path: Path, content: str) -> None:
    """Thin call-scoped seam into setup's atomic writer (setup owns it)."""
    from ouroboros.cli.commands.setup import _atomic_write_text as _write

    _write(path, content)


def pi_ooo_bridge_source_text_for_install() -> str:
    """Return the managed Pi extension source for ``ooo`` frontdoor dispatch."""
    command, args = detect_bridge_dispatch_entry()
    return pi_ooo_bridge_source_text(command=command, args=args)


def omp_ooo_bridge_source_text_for_install() -> str:
    """Return the managed OMP extension source for ``ooo`` frontdoor dispatch."""
    command, args = detect_bridge_dispatch_entry()
    return omp_ooo_bridge_source_text(command=command, args=args)


def _write_managed_bridge(
    *,
    dest: Path,
    content: str,
    display_name: str,
    reload_hint: str,
) -> bool:
    """Hash-compare and write one managed bridge extension file.

    Returns True when the file is current (already up to date or written);
    False when the write failed. Never rewrites an unchanged file so
    ``ouroboros update`` stays idempotent for live sessions.
    """
    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    existing_hash: str | None = None
    if dest.exists():
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None

    if existing_hash == new_hash:
        print_info(f"{display_name} ooo bridge already up to date: {dest}")
        return True

    try:
        _atomic_write_text(dest, content)
    except OSError as exc:
        print_warning(f"Could not install {display_name} ooo bridge at {dest}: {exc}")
        return False

    print_success(
        f"{'Updated' if existing_hash is not None else 'Installed'} {display_name} ooo bridge:"
        f" {dest}"
    )
    print_info(reload_hint)
    return True


def install_pi_ooo_bridge() -> bool:
    """Install the managed Pi extension that routes interactive ``ooo`` input.

    Pi auto-discovers global extensions in ``~/.pi/agent/extensions/*.ts``.
    Writing a single managed file there avoids modifying Pi itself or any
    third-party package such as roach-pi, while making the bridge available to
    every Pi-based/customized session after ``/reload`` or restart.
    """
    dest = Path.home() / ".pi" / "agent" / "extensions" / PI_OOO_BRIDGE_FILENAME
    return _write_managed_bridge(
        dest=dest,
        content=pi_ooo_bridge_source_text_for_install(),
        display_name="Pi",
        reload_hint="Restart Pi or run /reload in an existing Pi session to load the bridge.",
    )


def install_omp_ooo_bridge() -> bool:
    """Install the managed OMP extension that routes interactive ``ooo`` input.

    OMP (Oh My Pi) auto-discovers global extensions in
    ``~/.omp/agent/extensions/*.ts`` (same layout as Pi's ``~/.pi``).
    Writing a single managed file there makes the bridge available to every
    OMP session after restart, without modifying OMP itself.
    """
    dest = Path.home() / ".omp" / "agent" / "extensions" / OMP_OOO_BRIDGE_FILENAME
    return _write_managed_bridge(
        dest=dest,
        content=omp_ooo_bridge_source_text_for_install(),
        display_name="OMP",
        reload_hint="Restart OMP or run /reload in an existing OMP session to load the bridge.",
    )


__all__ = [
    "OMP_OOO_BRIDGE_FILENAME",
    "PI_OOO_BRIDGE_FILENAME",
    "detect_bridge_dispatch_entry",
    "install_omp_ooo_bridge",
    "install_pi_ooo_bridge",
    "omp_ooo_bridge_source_text_for_install",
    "pi_ooo_bridge_source_text_for_install",
]
