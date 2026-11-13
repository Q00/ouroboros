"""macOS start times are read from ``ps`` under the C locale.

``ps -o lstart=`` formats the start time with the caller's locale, while
``heartbeat._get_process_start_time`` parses it with a fixed English
``strptime`` format. Under a non-English locale (``LANG=ko_KR.UTF-8`` is the
default on Korean macOS) every read failed, the start time was always ``None``,
and PID-reuse fencing (which only compares when a start time is known) was
silently off. The ``ps`` child now runs with ``LC_ALL=C``.
"""

from __future__ import annotations

from datetime import datetime
import os
import platform
import subprocess
import sys
from typing import Any

import pytest

from ouroboros.orchestrator import heartbeat

_C_LSTART = "Mon Nov  9 02:32:10 2026    \n"
_EPOCH = datetime(2026, 11, 9, 2, 32, 10).timestamp()
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"  # the parser's format in heartbeat.py

#: Real ``ps -p <pid> -o lstart=`` output for the same instant on macOS 26,
#: captured with ``LC_ALL=<locale> ps -o lstart= -p $$``.
LOCALIZED_LSTART = {
    "ko_KR.UTF-8": "2026년 11월  9일 월요일 02시 32분 10초\n",
    "ja_JP.UTF-8": "月 11/ 9 02:32:10 2026\n",
    "de_DE.UTF-8": "Mo.  9 Nov. 02:32:10 2026\n",
    "fr_FR.UTF-8": "lun.  9 nov. 02:32:10 2026\n",
}


def _effective_time_locale(env: dict[str, str]) -> str:
    """POSIX precedence for LC_TIME: LC_ALL, then LC_TIME, then LANG."""
    for key in ("LC_ALL", "LC_TIME", "LANG"):
        if env.get(key):
            return env[key]
    return "C"


class _LocaleAwarePs:
    """Fake ``subprocess.run`` that localizes ``lstart`` the way macOS ``ps`` does."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        env = kwargs.get("env")
        effective = _effective_time_locale(dict(os.environ) if env is None else env)
        stdout = LOCALIZED_LSTART.get(effective, _C_LSTART)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


@pytest.mark.parametrize("raw", sorted(LOCALIZED_LSTART.values()))
def test_localized_lstart_cannot_be_parsed(raw: str) -> None:
    """Why the locale is forced: the parser accepts only the C-locale form."""
    with pytest.raises(ValueError):
        datetime.strptime(raw.strip(), _LSTART_FORMAT)
    assert datetime.strptime(_C_LSTART.strip(), _LSTART_FORMAT).timestamp() == _EPOCH


@pytest.mark.parametrize(
    "locale_env",
    [
        {"LANG": "ko_KR.UTF-8"},
        {"LC_ALL": "ko_KR.UTF-8"},
        {"LC_TIME": "ja_JP.UTF-8", "LANG": "en_US.UTF-8"},
        {"LC_ALL": "de_DE.UTF-8", "LANG": "fr_FR.UTF-8"},
        {"LANG": "en_US.UTF-8"},
        {},
    ],
    ids=["lang-ko", "lc-all-ko", "lc-time-ja", "lc-all-de", "lang-en", "unset"],
)
def test_ps_is_read_under_the_c_locale(
    monkeypatch: pytest.MonkeyPatch, locale_env: dict[str, str]
) -> None:
    for key in ("LC_ALL", "LC_TIME", "LANG"):
        monkeypatch.delenv(key, raising=False)
    for key, value in locale_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OUROBOROS_TEST_KEPT_VARIABLE", "kept")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    ps = _LocaleAwarePs()
    monkeypatch.setattr(subprocess, "run", ps)
    parent_env = dict(os.environ)

    assert heartbeat._get_process_start_time(4242) == _EPOCH

    [(argv, kwargs)] = ps.calls
    assert argv == ["ps", "-p", "4242", "-o", "lstart="]
    env = kwargs.pop("env")
    assert kwargs == {"capture_output": True, "text": True, "timeout": 3}
    assert env["LC_ALL"] == "C"
    # Only LC_ALL is set; everything else is the caller's environment, unchanged.
    assert {k: v for k, v in env.items() if k != "LC_ALL"} == {
        k: v for k, v in parent_env.items() if k != "LC_ALL"
    }
    assert env["OUROBOROS_TEST_KEPT_VARIABLE"] == "kept"
    assert dict(os.environ) == parent_env  # the parent environment is not mutated
    assert env is not os.environ


def test_non_darwin_does_not_spawn_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def no_spawn(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ps must not run off macOS")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    heartbeat._get_process_start_time(os.getpid())


def _has_locale(name: str) -> bool:
    try:
        listed = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return name in listed.stdout.split()


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real macOS ps")
@pytest.mark.parametrize("locale_name", sorted(LOCALIZED_LSTART))
def test_real_ps_start_time_is_read_under_a_localized_parent(
    monkeypatch: pytest.MonkeyPatch, locale_name: str
) -> None:
    if not _has_locale(locale_name):
        pytest.skip(f"{locale_name} is not installed")
    pid = os.getpid()
    expected_raw = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        timeout=3,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout.strip()
    monkeypatch.setenv("LC_ALL", locale_name)

    assert heartbeat._get_process_start_time(pid) == (
        datetime.strptime(expected_raw, _LSTART_FORMAT).timestamp()
    )
