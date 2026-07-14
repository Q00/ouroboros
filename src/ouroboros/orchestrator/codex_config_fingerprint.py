from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Final, Protocol

_GLOBAL_ROUTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "chatgpt_base_url",
        "experimental_thread_config_endpoint",
        "forced_chatgpt_workspace_id",
        "forced_login_method",
        "openai_base_url",
        "oss_provider",
        "profile",
        "profiles",
        "service_tier",
    }
)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


class CodexConfigFingerprintError(RuntimeError):
    pass


def fingerprint_codex_config_files(codex_home: Path) -> str:
    candidates: dict[str, Path] = {"config.toml": codex_home / "config.toml"}
    try:
        for path in codex_home.glob("*.config.toml"):
            candidates[path.name] = path
    except OSError as exc:
        raise CodexConfigFingerprintError("Cannot inspect Codex profile configuration") from exc

    digest = hashlib.sha256()
    digest.update(b"ouroboros-codex-config-v2\0")
    digest.update(str(codex_home.resolve(strict=False)).encode("utf-8"))
    digest.update(b"\0")
    for name, path in sorted(candidates.items()):
        digest.update(name.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        _update_config_digest(digest, path, global_config=name == "config.toml")
    return digest.hexdigest()


def _update_config_digest(
    digest: _Digest,
    path: Path,
    *,
    global_config: bool,
) -> None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise CodexConfigFingerprintError("Cannot inspect Codex profile configuration") from exc

    if path.is_symlink():
        try:
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
        except OSError as exc:
            raise CodexConfigFingerprintError("Cannot inspect Codex profile configuration") from exc
    if not path.is_file():
        digest.update(f"non-file:{stat_result.st_mode}\0".encode("ascii"))
        return

    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise CodexConfigFingerprintError("Cannot parse Codex profile configuration") from exc
    except OSError as exc:
        raise CodexConfigFingerprintError("Cannot read Codex profile configuration") from exc

    if global_config:
        config = {
            key: value
            for key, value in config.items()
            if key.startswith("model") or key in _GLOBAL_ROUTING_KEYS
        }
    digest.update(
        json.dumps(
            config,
            default=_serialize_toml_value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\0")


def _serialize_toml_value(value: date | datetime | time) -> dict[str, str]:
    return {"toml_type": type(value).__name__, "value": value.isoformat()}
