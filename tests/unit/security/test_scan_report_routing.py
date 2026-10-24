import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import ouroboros


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Checkpoint publication requires POSIX no-follow directory descriptors",
)
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize(
    ("skill_name", "filename", "is_ignored"),
    [
        ("vuln-scan", "VULN-FINDINGS.json", True),
        ("vuln-scan", "VULN-FINDINGS.md", True),
        ("threat-model", "THREAT_MODEL.md", False),
    ],
)
def test_scan_report_command_keeps_external_and_nested_targets_read_only(
    tmp_path: Path, external: bool, skill_name: str, filename: str, is_ignored: bool
) -> None:
    checkout = Path(ouroboros.__file__).resolve().parents[2]
    skill = (checkout / f".claude/skills/{skill_name}/SKILL.md").read_text()
    output = f"docs/security/{filename}"
    prefix = f"python3 .claude/skills/_lib/checkpoint.py append {output} "
    commands = [part for part in skill.split("`") if part.startswith(prefix)]
    assert len(commands) == 1, "The skill must provide one executable report command"
    root = tmp_path / "workspace"
    root.mkdir()
    target = (tmp_path if external else root) / "target"
    target.mkdir()
    (target / "source.py").write_text("sentinel")
    payload = root / "fresh-payload"
    content = json.dumps({"target": str(target), "findings": []})
    payload.write_text(content)
    argv = shlex.split(commands[0].replace("<payload-file>", str(payload)))
    argv[:2] = [sys.executable, str(checkout / argv[1])]
    result = subprocess.run(
        argv,
        cwd=root,
        env={**os.environ, "CHECKPOINT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (root / output).read_text().strip() == content
    assert [(p.name, p.read_text()) for p in target.iterdir()] == [("source.py", "sentinel")]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", output],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert ignored.returncode == (0 if is_ignored else 1)
    assert ignored.stdout.strip() == (output if is_ignored else "")
