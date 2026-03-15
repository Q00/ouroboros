# Platform Support

This page documents operating system and runtime backend compatibility for Ouroboros.

## Requirements

- **Python**: >= 3.12
- **Package manager**: [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Operating System Support Matrix

| Platform          | Status         | Notes                                              |
|-------------------|----------------|----------------------------------------------------|
| macOS (ARM/Intel) | Supported      | Primary development and CI platform                |
| Linux (x86_64)    | Supported      | Tested on Ubuntu 22.04+, Debian 12+, Fedora 38+   |
| Linux (ARM64)     | Supported      | Tested on Ubuntu 22.04+ (aarch64)                  |
| Windows (WSL 2)   | Supported      | Recommended Windows path; runs the Linux build      |
| Windows (native)  | Experimental   | See [Windows caveats](#windows-native-caveats) below |

## Runtime Backend Support Matrix

Runtime backends are configured via `orchestrator.runtime_backend` in your workflow seed or Ouroboros config.

| Runtime Backend | macOS | Linux | Windows (WSL 2) | Windows (native) |
|-----------------|-------|-------|------------------|-------------------|
| Claude Code     | Yes   | Yes   | Yes              | Experimental      |
| Codex CLI       | Yes   | Yes   | Yes              | Not supported     |

> **Note:** Claude Code and Codex CLI are independent runtime backends with different capabilities and trade-offs. See the [runtime capability matrix](runtime-capability-matrix.md) for a detailed comparison and the [runtime guides](runtime-guides/) for backend-specific details. Feature parity across backends is not guaranteed.

## macOS

Ouroboros is developed and tested primarily on macOS. Both Apple Silicon (ARM) and Intel Macs are supported.

```bash
# Install with uv
uv pip install ouroboros-ai              # Base (core engine)
uv pip install "ouroboros-ai[claude]"    # + Claude Code runtime deps
uv pip install "ouroboros-ai[litellm]"   # + LiteLLM multi-provider support
uv pip install "ouroboros-ai[all]"       # Everything (claude + litellm + dashboard)
```

> **Codex CLI** is installed separately (`npm install -g @openai/codex`). No Python extras required.

## Linux

Supported on major distributions with Python >= 3.12 available. Both x86_64 and ARM64 architectures are tested.

```bash
# Install with uv
uv pip install ouroboros-ai              # Base (core engine)
uv pip install "ouroboros-ai[claude]"    # + Claude Code runtime deps
uv pip install "ouroboros-ai[litellm]"   # + LiteLLM multi-provider support
uv pip install "ouroboros-ai[all]"       # Everything (claude + litellm + dashboard)
```

### Distribution-specific notes

- **Ubuntu/Debian**: Python 3.12+ may require the `deadsnakes` PPA on older releases.
- **Fedora 38+**: Python 3.12 is available in the default repositories.
- **Alpine**: Not tested. Native dependencies may require additional build tools.

## Windows (WSL 2) -- Recommended

For the best Windows experience, use [WSL 2](https://learn.microsoft.com/en-us/windows/wsl/install) with a supported Linux distribution (Ubuntu recommended). Under WSL 2, Ouroboros behaves identically to native Linux.

```bash
# Inside WSL 2
uv pip install ouroboros-ai              # Base
uv pip install "ouroboros-ai[all]"       # Or install everything
```

All runtime backends and features are fully supported under WSL 2.

## Windows (native) Caveats

Native Windows support is **experimental**. Known limitations:

- **File path handling**: Some workflow operations assume POSIX-style paths. Path-related edge cases may occur with native Windows paths.
- **Process management**: Subprocess spawning and signal handling differ on Windows. Long-running workflows may behave unexpectedly.
- **Codex CLI**: Not supported on native Windows. Use WSL 2 instead.
- **Terminal/TUI**: The Textual-based TUI requires a terminal emulator with good ANSI support (Windows Terminal recommended; `cmd.exe` is not supported).
- **CI testing**: Native Windows is not part of the current CI matrix. Bugs may go undetected between releases.

If you encounter Windows-specific issues, please [open an issue](https://github.com/Q00/ouroboros/issues) with the `platform:windows` label.

## Python Version Compatibility

| Python Version | Status    |
|----------------|-----------|
| 3.12           | Supported |
| 3.13           | Supported |
| 3.14+          | Supported |
| < 3.12         | Not supported |

The minimum required version is **Python >= 3.12** as specified in `pyproject.toml`.
