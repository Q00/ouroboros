"""System resource helpers for evidence-aware execution."""

from __future__ import annotations

import platform
import subprocess

_MIN_FREE_MEMORY_GB = 2.0
_MEMORY_CHECK_INTERVAL_SECONDS = 5.0
_MEMORY_WAIT_MAX_SECONDS = 120.0


def _get_available_memory_gb() -> float | None:
    """Get available memory in GB. Returns None if check fails."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            pages_free = 0
            pages_inactive = 0
            page_size = 4096  # macOS default
            for line in result.stdout.splitlines():
                if "page size of" in line:
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            page_size = int(part)
                elif line.startswith("Pages free:"):
                    pages_free = int(line.split(":")[1].strip().rstrip("."))
                elif line.startswith("Pages inactive:"):
                    pages_inactive = int(line.split(":")[1].strip().rstrip("."))
            return (pages_free + pages_inactive) * page_size / (1024**3)

        elif system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
            return None

        else:
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
