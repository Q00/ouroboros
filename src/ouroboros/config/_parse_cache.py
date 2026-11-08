"""Reuse the parsed data of a YAML file while its bytes are unchanged.

``load_config`` runs many times per process (several getters call it more than
once per lookup) and YAML parsing is nearly all of its cost. This module keeps
the *plain parsed data* -- never a validated model -- keyed by the resolved
path and the file's exact bytes, so any content change is parsed again.
Every caller receives its own deep copy, and a failed parse is never kept.
"""

from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import threading
from typing import Any

import yaml

# One entry per resolved path, for at most this many paths (oldest evicted).
_MAX_ENTRIES = 4
# Larger files are parsed on every call and never retained.
_MAX_CACHED_BYTES = 1024 * 1024

_lock = threading.Lock()
_entries: dict[str, tuple[bytes, Any]] = {}


def safe_load_yaml_file(path: Path) -> Any:
    """Return ``yaml.safe_load`` of *path*, reusing the parse of identical bytes.

    Equivalent to ``with path.open(encoding="utf-8") as f: yaml.safe_load(f)``:
    the same ``OSError`` from opening or reading, the same
    ``UnicodeDecodeError`` and ``yaml.YAMLError`` (including the
    ``in "<path>", line N, column M`` marks), and the same universal-newline
    decoding, because the bytes are parsed through an identical text wrapper.
    """
    with path.open("rb") as f:
        data = f.read()
    key = os.path.realpath(path)
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry[0] != data:
            del _entries[key]  # stale: these bytes are no longer the file
            entry = None
    if entry is not None:
        # The cached object is never handed out, so it is never mutated.
        return copy.deepcopy(entry[1])

    buffer = io.BytesIO(data)
    buffer.name = os.fspath(path)  # what open() reports, so YAML marks match
    with io.TextIOWrapper(buffer, encoding="utf-8") as stream:
        parsed = yaml.safe_load(stream)  # a failure propagates and caches nothing

    if len(data) <= _MAX_CACHED_BYTES:
        retained = copy.deepcopy(parsed)  # the caller may mutate ``parsed``
        with _lock:
            _entries.pop(key, None)
            _entries[key] = (data, retained)
            while len(_entries) > _MAX_ENTRIES:
                del _entries[next(iter(_entries))]
    return parsed


def clear() -> None:
    """Forget every cached parse."""
    with _lock:
        _entries.clear()
