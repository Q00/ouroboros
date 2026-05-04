"""Regression tests for hook script output encodings."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]


def _run_script_with_cp949_stdout(
    script_name: str, tmp_path: Path, stdin: str = ""
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "cp949"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name)],
        input=stdin.encode("utf-8"),
        capture_output=True,
        env=env,
        check=False,
    )


def test_keyword_detector_emits_utf8_when_parent_stdout_is_cp949(tmp_path: Path) -> None:
    """Windows Korean code pages must not crash on emoji skill suggestions."""
    result = _run_script_with_cp949_stdout("keyword-detector.py", tmp_path, "hello\n")

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "🎯 MATCHED SKILLS" in result.stdout.decode("utf-8")


def test_drift_monitor_emits_utf8_when_parent_stdout_is_cp949(tmp_path: Path) -> None:
    """PostToolUse hook should also normalize stdout/stderr for non-UTF-8 locales."""
    data_dir = tmp_path / ".ouroboros" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "interview_active.json").write_text("{}", encoding="utf-8")

    result = _run_script_with_cp949_stdout("drift-monitor.py", tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8").startswith("Ouroboros session active")
