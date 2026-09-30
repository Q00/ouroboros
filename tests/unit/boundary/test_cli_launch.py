"""A CLI oracle runs exactly the checkout files the controller proved.

The controller proves a CLI target's files (``oracle_run._cli_target_files``)
and the target process then opens them again to run them. A file swapped in
between (for a link out of the checkout, or for another file that is put
back afterwards, so no mutation check sees it) must never run as the proven
target: the launcher in the target process runs only the bytes of the file
whose identity the controller proved, and anything else is ``unprovable``.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import oracle_run
from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.runtime.exec_sandbox import sandbox_unavailable_reason

from .test_oracle import _oracle_result, _repo
from .test_oracle_run import _cli_package

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-follow proof")

# The checkout's code prints the smaller number (the case expects 8 for 3, 8);
# the code swapped in from outside prints the larger one, so it would pass.
WRONG = "import sys\nprint(min(int(sys.argv[1]), int(sys.argv[2])))\n"
RIGHT = "import sys\nprint(max(int(sys.argv[1]), int(sys.argv[2])))\n"
MOD = "import sys\nfrom . import pick\nprint(pick(int(sys.argv[1]), int(sys.argv[2])))\n"
CHECKOUT = {"tool.py": WRONG, "pkg/__init__.py": "pick = min\n", "pkg/mod.py": WRONG}
OUTSIDE = {"tool.py": RIGHT, "pkg/__init__.py": "pick = max\n", "pkg/mod.py": RIGHT}


def _outside(root: Path, marker: Path) -> Path:
    """The outside code, which leaves ``marker`` behind if it ever runs."""
    leave = f"open({str(marker)!r}, 'w').close()\n"
    return _repo(root, {path: leave + text for path, text in OUTSIDE.items()})


async def _run(root: Path, symbol: str, *, on_base: bool = False) -> oracle_run.OracleRun:
    package = _cli_package(root, symbol)
    return await oracle_run.run_oracle_check(
        {item.path: item.content for item in package.files},
        package.oracles[0],
        root,
        timeout_seconds=30,
        on_base=on_base,
        env=dict(os.environ),
        interpreter=pin_interpreter(sys.executable, "test"),
        binding=None,
    )


@pytest.mark.parametrize(
    ("symbol", "target"),
    [("tool.py", "tool.py"), ("-m pkg.mod", "pkg/mod.py"), ("-m pkg.mod", "pkg/__init__.py")],
    ids=["script", "module", "package_init"],
)
@pytest.mark.parametrize("swap", ["link", "restored", "rewritten"])
async def test_a_target_swapped_after_its_proof_never_runs_as_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol: str, target: str, swap: str
) -> None:
    root = _repo(tmp_path / "cand", dict(CHECKOUT))
    if target == "pkg/__init__.py":
        (root / "pkg" / "mod.py").write_text(MOD)
    marker = tmp_path / "outside-code-ran"
    outside = _outside(tmp_path / "outside", marker)
    path = root / target
    original = path.read_text()
    aside = tmp_path / "aside"
    proven = oracle_run._cli_target_files
    launch = oracle_run._cli_case

    def prove_then_swap(cwd: Path, bound: str) -> Any:
        result = proven(cwd, bound)
        # Right after the proof, before the launch.
        if swap == "link":
            (tmp_path / "link").symlink_to(outside / target)
            os.replace(tmp_path / "link", path)
        elif swap == "restored":
            os.replace(path, aside)
            path.write_text((outside / target).read_text())
        else:
            path.write_text((outside / target).read_text())  # same inode, other bytes
        return result

    async def launch_then_restore(*args: Any, **kwargs: Any) -> Any:
        try:
            return await launch(*args, **kwargs)
        finally:
            # The proven file is back: no mutation check would see anything.
            if swap == "restored":
                os.replace(aside, path)
            elif swap == "rewritten":
                path.write_text(original)

    monkeypatch.setattr(oracle_run, "_cli_target_files", prove_then_swap)
    monkeypatch.setattr(oracle_run, "_cli_case", launch_then_restore)
    run = await _run(root, symbol)

    assert not marker.exists(), "code from outside the checkout ran"
    assert run.result is not None
    assert run.result["resolve"] == "unprovable"
    assert run.return_code == 3 and not run.signature_seen
    if swap != "link":
        # The identity check fired, not an incidental failure to open.
        assert "not the proven file" in run.output


@pytest.mark.parametrize(
    ("files", "symbol"),
    [
        # A script sees the argv, ``__name__`` and ``sys.path[0]`` of ``python bin/tool.py``.
        (
            {
                "bin/helper.py": "def pick(a, b):\n    return max(a, b)\n",
                "bin/tool.py": (
                    "import os, sys\nfrom helper import pick\n"
                    "assert __name__ == '__main__' and sys.argv[0] == 'bin/tool.py'\n"
                    "assert __file__ == os.path.abspath('bin/tool.py')\n"
                    "assert sys.path[0] == os.path.dirname(__file__)\n"
                    "print(pick(int(sys.argv[1]), int(sys.argv[2])))\n"
                ),
            },
            "bin/tool.py",
        ),
        # A module sees its package, and a relative import works as under ``-m``.
        (
            {
                "pkg/__init__.py": "pick = max\n",
                "pkg/mod.py": (
                    "import os, sys\nfrom . import pick\n"
                    "assert __name__ == '__main__' and __package__ == 'pkg'\n"
                    "assert __spec__.name == 'pkg.mod' and sys.argv[0] == __file__\n"
                    "assert sys.path[0] == os.getcwd()\n"
                    "print(pick(int(sys.argv[1]), int(sys.argv[2])))\n"
                ),
            },
            "-m pkg.mod",
        ),
        # ``-m pkg`` runs the package's ``__main__``.
        (
            {
                "pkg/__init__.py": "pick = max\n",
                "pkg/__main__.py": (
                    "import sys\nfrom . import pick\n"
                    "assert __spec__.name == 'pkg.__main__'\n"
                    "print(pick(int(sys.argv[1]), int(sys.argv[2])))\n"
                ),
            },
            "-m pkg",
        ),
    ],
    ids=["script", "module", "package_main"],
)
@pytest.mark.parametrize("mode", ["sandbox_off", "sandbox_on"])
async def test_the_proven_target_runs_as_python_would_run_it(
    tmp_path: Path, files: dict[str, str], symbol: str, mode: str, request: pytest.FixtureRequest
) -> None:
    if mode == "sandbox_on":
        request.getfixturevalue("real_check_isolation")
        reason = sandbox_unavailable_reason(deny_network=True)
        if reason is not None:
            pytest.skip(f"execution sandbox unavailable on this host: {reason}")
    base = _repo(tmp_path / "base", files)
    run = await _run(base, symbol)
    assert run.result is not None and run.result["resolve"] == "ok"
    assert run.return_code == 0, run.output
    admission = await admit_check_package(_cli_package(base, symbol), base)
    assert _oracle_result(admission.checks[0])["resolve"] == "ok"
    assert admission.check_tiers == {"oracle_1": "A"}


async def test_a_bare_executable_target_is_not_provable(tmp_path: Path) -> None:
    # Its bytes cannot be run from the proven file inside the target process.
    base = _repo(tmp_path / "base", {"tool": "#!/bin/sh\necho 8\n"})
    (base / "tool").chmod(0o755)
    admission = await admit_check_package(_cli_package(base, "tool"), base)
    assert _oracle_result(admission.checks[0])["resolve"] == "unprovable"
    assert admission.check_tiers == {"oracle_1": "U"}
