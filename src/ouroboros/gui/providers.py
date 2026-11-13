"""API providers for Ouroboros' LiteLLM backend (``~/.ouroboros/credentials.yaml``).

Agent runtimes (Claude Code, Codex, …) bring their own logins. API providers
matter when the LLM backend is ``litellm``: a model id such as
``groq/llama-4`` picks the ``groq`` entry below for its key and base URL.

The catalog follows opencodex's provider registry (MIT), limited to providers
LiteLLM routes natively. ``litellm_proxy`` covers any OpenAI-compatible
gateway — including a local opencodex at ``http://127.0.0.1:10100/v1``.

Keys never leave this process: the page only sees whether a key is set and
its last four characters.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile
from typing import Any
import urllib.error
import urllib.request

import yaml

from ouroboros.config.models import get_config_dir

_PLACEHOLDER_PREFIX = "YOUR_"
_OCX_URL = "http://127.0.0.1:10100"


class ProviderError(Exception):
    """A provider change was refused."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str  # credentials.yaml key == LiteLLM model prefix
    label: str
    base_url: str
    key_url: str = ""
    example_model: str = ""
    group: str = "cloud"  # cloud | gateway | local
    key_required: bool = True
    featured: bool = False


CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "litellm_proxy",
        "opencodex / OpenAI-compatible gateway",
        f"{_OCX_URL}/v1",
        "https://github.com/lidge-jun/opencodex",
        "gpt-5.5",
        "gateway",
        key_required=False,
        featured=True,
    ),
    ProviderSpec(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/keys",
        "anthropic/claude-sonnet-5",
        "gateway",
        featured=True,
    ),
    ProviderSpec(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com",
        "https://console.anthropic.com/settings/keys",
        "claude-sonnet-5",
        featured=True,
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
        "gpt-5.5",
        featured=True,
    ),
    ProviderSpec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com",
        "https://aistudio.google.com/apikey",
        "gemini-3.5-flash",
        featured=True,
    ),
    ProviderSpec("xai", "xAI Grok", "https://api.x.ai/v1", "https://console.x.ai", "grok-4.5"),
    ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        "https://platform.deepseek.com/api_keys",
        "deepseek-chat",
        featured=True,
    ),
    ProviderSpec(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
        "llama-4-maverick",
    ),
    ProviderSpec(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "https://console.mistral.ai/api-keys",
        "codestral-latest",
    ),
    ProviderSpec(
        "moonshot",
        "Moonshot (Kimi API)",
        "https://api.moonshot.ai/v1",
        "https://platform.moonshot.ai/console/api-keys",
        "kimi-k3.1-pro",
    ),
    ProviderSpec(
        "together_ai",
        "Together",
        "https://api.together.xyz/v1",
        "https://api.together.xyz/settings/api-keys",
    ),
    ProviderSpec(
        "fireworks_ai",
        "Fireworks",
        "https://api.fireworks.ai/inference/v1",
        "https://fireworks.ai/account/api-keys",
    ),
    ProviderSpec(
        "cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        "https://cloud.cerebras.ai/platform/apikeys",
        "gpt-oss-120b",
    ),
    ProviderSpec(
        "deepinfra",
        "DeepInfra",
        "https://api.deepinfra.com/v1/openai",
        "https://deepinfra.com/dash/api_keys",
    ),
    ProviderSpec(
        "sambanova",
        "SambaNova Cloud",
        "https://api.sambanova.ai/v1",
        "https://cloud.sambanova.ai/apis",
    ),
    ProviderSpec(
        "nebius",
        "Nebius Token Factory",
        "https://api.tokenfactory.nebius.com/v1",
        "https://tokenfactory.nebius.com",
    ),
    ProviderSpec(
        "novita",
        "Novita AI",
        "https://api.novita.ai/openai/v1",
        "https://novita.ai/settings/key-management",
    ),
    ProviderSpec(
        "featherless_ai",
        "Featherless AI",
        "https://api.featherless.ai/v1",
        "https://featherless.ai/account/api-keys",
    ),
    ProviderSpec(
        "hyperbolic",
        "Hyperbolic",
        "https://api.hyperbolic.xyz/v1",
        "https://app.hyperbolic.ai",
    ),
    ProviderSpec(
        "huggingface",
        "Hugging Face",
        "https://router.huggingface.co/v1",
        "https://huggingface.co/settings/tokens",
    ),
    ProviderSpec(
        "nvidia_nim",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        "https://build.nvidia.com",
    ),
    ProviderSpec(
        "volcengine",
        "Volcengine Ark",
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://console.volcengine.com/ark/region:ark+cn-beijing/apikey",
    ),
    ProviderSpec(
        "dashscope",
        "Alibaba DashScope (Qwen)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.console.aliyun.com/apiKey",
    ),
    ProviderSpec(
        "vercel_ai_gateway",
        "Vercel AI Gateway",
        "https://ai-gateway.vercel.sh/v1",
        "https://vercel.com/dashboard",
        group="gateway",
    ),
    ProviderSpec(
        "ollama",
        "Ollama (local)",
        "http://localhost:11434",
        "https://ollama.com/download",
        "qwen3-coder",
        "local",
        key_required=False,
    ),
    ProviderSpec(
        "lm_studio",
        "LM Studio (local)",
        "http://localhost:1234/v1",
        "https://lmstudio.ai",
        group="local",
        key_required=False,
    ),
    ProviderSpec(
        "hosted_vllm",
        "vLLM (local)",
        "http://localhost:8000/v1",
        "https://docs.vllm.ai",
        group="local",
        key_required=False,
    ),
)

_BY_ID = {spec.id: spec for spec in CATALOG}
# Ouroboros' own default template keys (credentials.yaml) that map to a catalog id.
_ALIASES = {"google": "gemini"}


def credentials_path() -> Path:
    return get_config_dir() / "credentials.yaml"


def _load() -> dict[str, Any]:
    path = credentials_path()
    if not path.exists():
        return {"providers": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ProviderError(f"credentials.yaml could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("credentials.yaml is not a mapping")
    providers = data.get("providers")
    if providers is None:
        data["providers"] = {}
    elif not isinstance(providers, dict):
        raise ProviderError("credentials.yaml 'providers' is not a mapping")
    return data


def _is_placeholder(key: object) -> bool:
    return isinstance(key, str) and key.strip().upper().startswith(_PLACEHOLDER_PREFIX)


def _key_hint(key: object) -> str | None:
    if not isinstance(key, str) or not key.strip() or _is_placeholder(key):
        return None
    tail = key.strip()[-4:]
    return f"…{tail}" if len(key.strip()) > 8 else "set"


def _write(data: dict[str, Any]) -> None:
    """Validate, back up, then atomically replace credentials.yaml (mode 600)."""
    from ouroboros.config.models import CredentialsConfig

    cleaned = {
        name: entry
        for name, entry in data.get("providers", {}).items()
        if isinstance(entry, dict) and entry.get("api_key")
    }
    try:
        CredentialsConfig.model_validate({"providers": cleaned})
    except Exception as exc:
        raise ProviderError(f"Rejected: {exc}") from exc
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name("credentials.yaml.bak")
        backup.write_bytes(path.read_bytes())
        with suppress(OSError):
            os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
    text = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=".credentials.", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
        temp = Path(handle.name)
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    temp.replace(path)


def _ocx_running() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{_OCX_URL}/healthz", timeout=0.4) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if isinstance(data, dict) and data.get("service") == "opencodex":
        return {"url": _OCX_URL, "version": data.get("version")}
    return None


def view() -> dict[str, Any]:
    data = _load()
    saved: dict[str, Any] = data.get("providers", {})
    rows = []
    for spec in CATALOG:
        entry = saved.get(spec.id)
        if entry is None:
            entry = next(
                (
                    saved[alias]
                    for alias, target in _ALIASES.items()
                    if target == spec.id and alias in saved
                ),
                None,
            )
        entry = entry if isinstance(entry, dict) else {}
        hint = _key_hint(entry.get("api_key"))
        rows.append(
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "featured": spec.featured,
                "default_base_url": spec.base_url,
                "base_url": entry.get("base_url") or None,
                "key_url": spec.key_url,
                "key_required": spec.key_required,
                "configured": hint is not None,
                "key_hint": hint,
                "model_format": f"{spec.id}/{spec.example_model or '<model>'}",
            }
        )
    extra = sorted(
        name
        for name in saved
        if name not in _BY_ID
        and name not in _ALIASES
        and _key_hint(saved[name].get("api_key") if isinstance(saved[name], dict) else None)
    )
    from ouroboros.gui.settings import SettingsModel

    model = SettingsModel()
    return {
        "path": str(credentials_path()),
        "providers": rows,
        "other": extra,
        "llm_backend": model.current("llm.backend"),
        "litellm_installed": _litellm_installed(),
        "opencodex": _ocx_running(),
    }


def _litellm_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("litellm") is not None


def summary() -> dict[str, int]:
    try:
        rows = view()["providers"]
    except ProviderError:
        return {"configured": 0, "total": len(CATALOG)}
    return {"configured": sum(1 for r in rows if r["configured"]), "total": len(rows)}


def save(provider_id: str, payload: dict[str, Any]) -> None:
    spec = _BY_ID.get(provider_id)
    if spec is None:
        raise ProviderError(f"Unknown provider: {provider_id}")
    data = _load()
    providers = data["providers"]
    current = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
    api_key = payload.get("api_key")
    base_url = payload.get("base_url")
    entry: dict[str, Any] = dict(current or {})
    if isinstance(api_key, str) and api_key.strip():
        entry["api_key"] = api_key.strip()
    elif not entry.get("api_key") or _is_placeholder(entry.get("api_key")):
        if spec.key_required:
            raise ProviderError("Paste an API key first.")
        # LiteLLM still wants a non-empty key for keyless local servers.
        entry["api_key"] = "not-needed"
    if isinstance(base_url, str):
        cleaned = base_url.strip()
        if cleaned and not cleaned.startswith(("http://", "https://")):
            raise ProviderError("Base URL must start with http:// or https://")
        if cleaned and cleaned != spec.base_url:
            entry["base_url"] = cleaned
        elif spec.group in ("local", "gateway"):
            entry["base_url"] = cleaned or spec.base_url
        else:
            entry.pop("base_url", None)
    providers[provider_id] = entry
    _write(data)


def remove(provider_id: str) -> None:
    data = _load()
    providers = data["providers"]
    removed = providers.pop(provider_id, None)
    for alias, target in _ALIASES.items():
        if target == provider_id:
            removed = providers.pop(alias, None) or removed
    if removed is None:
        return
    _write(data)


def test(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """List models from the provider's OpenAI-style ``/models`` endpoint."""
    spec = _BY_ID.get(provider_id)
    if spec is None:
        raise ProviderError(f"Unknown provider: {provider_id}")
    saved = _load()["providers"].get(provider_id) or {}
    key = (payload.get("api_key") or "").strip() or saved.get("api_key") or ""
    base = (payload.get("base_url") or "").strip() or saved.get("base_url") or spec.base_url
    if spec.id == "ollama":
        url = base.rstrip("/") + "/api/tags"
    elif spec.id == "anthropic":
        url = base.rstrip("/") + "/v1/models"
    elif spec.id == "gemini":
        url = base.rstrip("/") + "/v1beta/models"
    else:
        url = base.rstrip("/") + "/models"
    headers = {"User-Agent": "ouroboros-gui"}
    if key and not _is_placeholder(key) and key != "not-needed":
        if spec.id == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif spec.id == "gemini":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed catalog URL
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "message": f"HTTP {exc.code} from {url}"}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"ok": False, "message": f"Could not reach {url}: {exc}"}
    items = body.get("data") or body.get("models") or [] if isinstance(body, dict) else []
    names = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if isinstance(name, str):
                names.append(name.removeprefix("models/"))
    return {"ok": True, "count": len(names), "models": names[:200]}


__all__ = ["CATALOG", "ProviderError", "remove", "save", "summary", "test", "view"]
