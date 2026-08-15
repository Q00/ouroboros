"""Executable browser-state regression for the generated dashboard page."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from ouroboros.dashboard_web.page import INDEX_HTML


def _live_script() -> str:
    _head, marker, tail = INDEX_HTML.partition("<script>")
    assert marker, "dashboard page has no script element"
    script, marker, _foot = tail.partition("</script>")
    assert marker, "dashboard page script is not terminated"
    return script


def test_generated_page_executes_picker_state_machine_in_node_vm() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the dashboard browser contract regression"
    harness = Path(__file__).with_name("page_runtime_harness.cjs")
    completed = subprocess.run(
        [node, str(harness)],
        input=_live_script(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, (
        "generated dashboard JavaScript behavior failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    assert completed.stdout.strip() == "dashboard browser state machine: PASS"
