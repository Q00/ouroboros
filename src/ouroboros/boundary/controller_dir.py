"""Controller-owned directory from which an oracle check runs.

An oracle check's files (the product harness and the frozen oracle data, plus
the late binding at verification time) never enter the checkout copy the check
runs in. They are written to a sibling directory of the copy, made read-only,
and digested before and after the command. The check's argv names the harness
by absolute path; its cwd is the checkout copy. A workspace module that tries
to find and edit the check files at import time meets read-only files, and an
edit that gets through anyway (for example after a ``chmod``) is reported as
a protected-byte mutation, which makes the check indeterminate.

Reading is not prevented: the product has no OS sandbox, so artifact code with
the user's privileges can read these files. The target is called in a child
process that receives inputs only, and the comparison happens in a process
that never imports workspace code.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import shutil
import stat

from ouroboros.boundary.binding import Binding
from ouroboros.boundary.oracle import BINDINGS_FILE, ORACLE_DIR, bindings_text, is_oracle_file
from ouroboros.boundary.package import CheckPackage, CheckSpec
from ouroboros.boundary.tree import tree_manifest

_READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_READ_ONLY_DIR = _READ_ONLY_FILE | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
CONTROLLER_PREFIX = "ctrl:"


def controller_dir_for(copy_root: Path) -> Path:
    """The controller directory paired with one checkout copy."""
    return copy_root.parent / f"{copy_root.name}.ctrl"


def prepare_controller_dir(
    package: CheckPackage,
    check: CheckSpec,
    copy_root: Path,
    *,
    bindings: Mapping[str, Binding] | None = None,
) -> tuple[tuple[str, ...], dict[str, str], Path]:
    """Write the oracle files outside the copy; return argv, manifest, and the dir.

    The returned argv replaces the packaged harness path with its absolute
    location in the controller directory. Everything is made read-only.
    """
    ctrl = controller_dir_for(copy_root)
    oracle_root = ctrl / ORACLE_DIR
    oracle_root.mkdir(parents=True)
    for item in package.files:
        if is_oracle_file(item.path):
            target = ctrl / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(item.content.encode("utf-8"))
    binding = (bindings or {}).get(check.check_id)
    if binding is not None:
        with open(oracle_root / BINDINGS_FILE, "xb") as handle:
            handle.write(bindings_text({check.check_id: binding}).encode("utf-8"))
    argv = tuple(
        str(ctrl / arg) if index > 0 and is_oracle_file(arg) else arg
        for index, arg in enumerate(check.argv)
    )
    _set_modes(ctrl, read_only=True)
    return argv, tree_manifest(ctrl, unprotected_names=()), ctrl


def controller_mutations(ctrl: Path, before: Mapping[str, str]) -> tuple[str, ...]:
    """Controller files that changed, disappeared, or appeared during the run."""
    after = tree_manifest(ctrl, unprotected_names=())
    changed = {path for path, digest in before.items() if after.get(path) != digest}
    changed.update(set(after) - set(before))
    return tuple(f"{CONTROLLER_PREFIX}{path}" for path in sorted(changed))


def release_controller_dir(ctrl: Path) -> None:
    """Restore write permission so the directory can be removed."""
    if ctrl.exists():
        _set_modes(ctrl, read_only=False)


def remove_controller_dir(ctrl: Path) -> None:
    release_controller_dir(ctrl)
    shutil.rmtree(ctrl, ignore_errors=True)


def _set_modes(root: Path, *, read_only: bool) -> None:
    # chmod needs ownership, not write access to the parent, so order is free.
    entries: list[tuple[Path, bool]] = [(root, True)]
    for dirpath, dirnames, filenames in os.walk(root):
        entries.extend((Path(dirpath) / name, True) for name in dirnames)
        entries.extend((Path(dirpath) / name, False) for name in filenames)
    for path, is_dir in entries:
        if path.is_symlink():
            continue
        if read_only:
            mode = _READ_ONLY_DIR if is_dir else _READ_ONLY_FILE
        else:
            mode = stat.S_IRWXU if is_dir else stat.S_IRUSR | stat.S_IWUSR
        try:
            os.chmod(path, mode)
        except OSError:
            continue


__all__ = [
    "CONTROLLER_PREFIX",
    "controller_dir_for",
    "controller_mutations",
    "prepare_controller_dir",
    "release_controller_dir",
    "remove_controller_dir",
]
