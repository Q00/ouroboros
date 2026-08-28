"""OMP (Oh My Pi) runtime setup for ``ouroboros setup --runtime omp``.

Split out of :mod:`ouroboros.cli.commands.setup` to respect the module-size
ratchet: setup.py delegates here for every omp-specific step (CLI detection,
config writing, bridge install) and re-exports the entry points under its
historic underscore names.
"""

from __future__ import annotations

import shutil

import yaml

from ouroboros.cli.commands.ooo_bridges import install_omp_ooo_bridge
from ouroboros.cli.formatters.panels import print_error, print_info, print_success


def detect_omp_runtime() -> str | None:
    """Resolve the omp CLI: explicit env/config path first, then PATH."""
    from ouroboros.config import get_omp_cli_path

    try:
        omp_path = get_omp_cli_path()
    except Exception:
        omp_path = None
    return (omp_path if omp_path and shutil.which(omp_path) else None) or shutil.which("omp")


def setup_omp(omp_path: str) -> bool:
    """Configure Ouroboros for the OMP (Oh My Pi) CLI runtime.

    OMP is a base-package agent runtime in the Pi family: setup records the
    executable path and runtime backend, and installs a managed OMP extension
    so interactive OMP sessions can route ``ooo ...`` inputs into Ouroboros'
    shared skill dispatcher. The OMP LLM adapter remains opt-in so setup does
    not unexpectedly move authoring/evaluation traffic to a different
    provider; when selected explicitly, structured response_format requests
    are enforced cooperatively by that adapter.

    Returns True when the runtime configuration is committed and the managed
    bridge is installed. Returns False — with no OMP configuration committed —
    when the bridge write fails, so callers fail closed instead of leaving a
    selection whose interactive ``ooo`` dispatch silently does not work
    (PR #2299 review round 1).
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        create_default_config(config_dir)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if not isinstance(config_dict, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting OMP setup.")
        return False

    orch = config_dict.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {}
        config_dict["orchestrator"] = orch
    orch["runtime_backend"] = "omp"
    orch["omp_cli_path"] = omp_path

    # Bridge activation comes before the config commit: a failed bridge write
    # must not leave a persisted OMP selection without interactive `ooo`
    # dispatch.
    if not install_omp_ooo_bridge():
        print_error("OMP ooo bridge installation failed — OMP configuration was not changed.")
        return False

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    print_success(f"Configured OMP runtime (CLI: {omp_path})")
    print_info(f"Config saved to: {config_path}")
    return True


def select_omp_runtime(available: dict[str, str | None]) -> None:
    """Handle the ``omp`` branch of setup's interactive runtime selection.

    Prints omp-specific install guidance and exits non-zero when the CLI is
    missing or the setup flow fails (for example, a failed bridge write);
    otherwise runs the full omp setup flow.
    """
    import typer

    omp_path = available.get("omp")
    if not omp_path:
        print_error(
            "OMP (Oh My Pi) CLI not found.\n"
            "Install the omp CLI so `omp` is on PATH, set "
            "OUROBOROS_OMP_CLI_PATH, or configure orchestrator.omp_cli_path."
        )
        raise typer.Exit(1)
    if not setup_omp(omp_path):
        raise typer.Exit(1)


__all__ = ["detect_omp_runtime", "select_omp_runtime", "setup_omp"]
