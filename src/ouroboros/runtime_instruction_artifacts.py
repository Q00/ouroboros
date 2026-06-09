"""Install runtime-owned instruction artifacts for skill capability guides."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from ouroboros.backends.capabilities import render_backend_skill_capability_guide

GUIDE_FILENAME = "ouroboros-skill-capability-guide.md"
COPILOT_INSTRUCTIONS_DIRNAME = "ouroboros-instructions"
COPILOT_AGENTS_FILENAME = "AGENTS.md"
_SECTION_START = "<!-- ouroboros:skill-capability-guide:start -->"
_SECTION_END = "<!-- ouroboros:skill-capability-guide:end -->"


@dataclass(frozen=True, slots=True)
class RuntimeInstructionArtifact:
    """One installed runtime instruction artifact."""

    backend: str
    path: Path


def _render_section(backend: str) -> str:
    guide = render_backend_skill_capability_guide(backend).rstrip()
    return f"{_SECTION_START}\n{guide}\n{_SECTION_END}\n"


def _upsert_marked_section(existing: str, section: str) -> str:
    """Insert or replace managed guide sections without dropping user text."""
    chunks: list[str] = []
    replaced = False
    cursor = 0

    while True:
        start = existing.find(_SECTION_START, cursor)
        if start == -1:
            chunks.append(existing[cursor:])
            break

        end = existing.find(_SECTION_END, start + len(_SECTION_START))
        if end == -1:
            chunks.append(existing[cursor:])
            break

        nested_start = existing.find(_SECTION_START, start + len(_SECTION_START), end)
        if nested_start != -1:
            chunks.append(existing[cursor:nested_start])
            cursor = nested_start
            continue

        chunks.append(existing[cursor:start])
        if not replaced:
            chunks.append(section)
            replaced = True
        cursor = end + len(_SECTION_END)

    if replaced:
        return "".join(chunks).rstrip() + "\n"

    base = existing.rstrip()
    if not base:
        return section
    return f"{base}\n\n{section}"


def _write_managed_section(path: Path, backend: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(
        _upsert_marked_section(existing, _render_section(backend)),
        encoding="utf-8",
    )
    return path


def _write_exact_guide(path: Path, backend: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_backend_skill_capability_guide(backend), encoding="utf-8")
    return path


def opencode_instruction_path(config_dir: str | Path) -> Path:
    """Return OpenCode's global instruction artifact path."""
    return Path(config_dir).expanduser() / "AGENTS.md"


def gemini_instruction_path(home: str | Path | None = None) -> Path:
    """Return Gemini CLI's global memory/instruction artifact path."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".gemini" / "GEMINI.md"


def kiro_instruction_path(home: str | Path | None = None) -> Path:
    """Return Kiro's global steering artifact path."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".kiro" / "steering" / GUIDE_FILENAME


def copilot_instruction_dir(home: str | Path | None = None) -> Path:
    """Return the setup-owned Copilot custom-instructions directory."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".copilot" / COPILOT_INSTRUCTIONS_DIRNAME


def copilot_instruction_path(home: str | Path | None = None) -> Path:
    """Return the setup-owned Copilot AGENTS.md instruction artifact path."""
    return copilot_instruction_dir(home) / COPILOT_AGENTS_FILENAME


def gjc_agent_dir(home: str | Path | None = None, environ: dict[str, str] | None = None) -> Path:
    """Return GJC's agent directory for rules/extensions discovery."""
    env = os.environ if environ is None else environ
    explicit_agent_dir = env.get("GJC_CODING_AGENT_DIR", "").strip()
    if explicit_agent_dir:
        return Path(explicit_agent_dir).expanduser()

    config_dir = env.get("GJC_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / "agent"

    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".gjc" / "agent"


def gjc_instruction_path(
    home: str | Path | None = None, environ: dict[str, str] | None = None
) -> Path:
    """Return GJC's global rules artifact path."""
    return gjc_agent_dir(home=home, environ=environ) / "rules" / GUIDE_FILENAME


def install_opencode_instruction_artifact(
    *,
    config_dir: str | Path,
) -> RuntimeInstructionArtifact:
    """Install OpenCode's setup-owned global AGENTS.md guidance section."""
    return RuntimeInstructionArtifact(
        backend="opencode",
        path=_write_managed_section(opencode_instruction_path(config_dir), "opencode"),
    )


def install_gemini_instruction_artifact(
    *,
    home: str | Path | None = None,
) -> RuntimeInstructionArtifact:
    """Install Gemini CLI's setup-owned global GEMINI.md guidance section."""
    return RuntimeInstructionArtifact(
        backend="gemini",
        path=_write_managed_section(gemini_instruction_path(home), "gemini"),
    )


def install_kiro_instruction_artifact(
    *,
    home: str | Path | None = None,
) -> RuntimeInstructionArtifact:
    """Install Kiro's setup-owned global steering guidance file."""
    return RuntimeInstructionArtifact(
        backend="kiro",
        path=_write_managed_section(kiro_instruction_path(home), "kiro"),
    )


def install_copilot_instruction_artifact(
    *,
    home: str | Path | None = None,
) -> RuntimeInstructionArtifact:
    """Install Copilot CLI's setup-owned AGENTS.md guidance file."""
    return RuntimeInstructionArtifact(
        backend="copilot",
        path=_write_managed_section(copilot_instruction_path(home), "copilot"),
    )


def install_gjc_instruction_artifact(
    *,
    home: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> RuntimeInstructionArtifact:
    """Install GJC's setup-owned global rules guidance file."""
    return RuntimeInstructionArtifact(
        backend="gjc",
        path=_write_exact_guide(gjc_instruction_path(home=home, environ=environ), "gjc"),
    )


__all__ = [
    "COPILOT_AGENTS_FILENAME",
    "COPILOT_INSTRUCTIONS_DIRNAME",
    "GUIDE_FILENAME",
    "RuntimeInstructionArtifact",
    "copilot_instruction_dir",
    "copilot_instruction_path",
    "gemini_instruction_path",
    "gjc_agent_dir",
    "gjc_instruction_path",
    "install_copilot_instruction_artifact",
    "install_gemini_instruction_artifact",
    "install_gjc_instruction_artifact",
    "install_kiro_instruction_artifact",
    "install_opencode_instruction_artifact",
    "kiro_instruction_path",
    "opencode_instruction_path",
]
