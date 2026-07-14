# Platform Support

Operating system and runtime backend compatibility for Ouroboros.

For installation instructions, see [Getting Started](getting-started.md).

## Requirements

- **Python**: >= 3.12 for core and non-LiteLLM profiles
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

| Runtime Backend    | macOS | Linux | Windows (WSL 2) | Windows (native) |
|--------------------|-------|-------|------------------|-------------------|
| Claude Code        | Yes   | Yes   | Yes              | Experimental      |
| Codex CLI          | Yes   | Yes   | Yes              | Not supported     |
| *(custom adapter)* | Depends on adapter | Depends on adapter | Depends on adapter | Depends on adapter |

See the [runtime capability matrix](runtime-capability-matrix.md) for a feature comparison across backends.

## Linux Distribution Notes

- **Ubuntu/Debian**: Python 3.12+ may require the `deadsnakes` PPA on older releases.
- **Fedora 38+**: Python 3.12 is available in the default repositories.
- **Alpine**: Not tested. Native dependencies may require additional build tools.

## Windows (WSL 2)

For the best Windows experience, use [WSL 2](https://learn.microsoft.com/en-us/windows/wsl/install) with a supported Linux distribution (Ubuntu recommended). All runtime backends and features are fully supported under WSL 2.

Windows 11 Home is a valid WSL 2 host when virtualization and the required Windows optional features are available. If WSL itself will not install, follow the [Windows WSL 2 troubleshooting guide](guides/windows-wsl-troubleshooting.md) before installing Ouroboros.

## Windows (native) Caveats

Native Windows support is **experimental**. Known limitations:

- **File path handling**: Some workflow operations assume POSIX-style paths.
- **Process management**: Subprocess spawning and signal handling differ on Windows.
- **Codex CLI**: Not supported on native Windows. Use WSL 2 instead.
- **Terminal/TUI**: Requires a terminal with ANSI support (Windows Terminal recommended; `cmd.exe` is not supported).
- **CI testing**: Native Windows is not part of the current CI matrix.

If you encounter Windows-specific issues, please [open an issue](https://github.com/Q00/ouroboros/issues) with the `platform:windows` label.

## Python Version Compatibility

| Python Version | Status        |
|----------------|---------------|
| 3.12           | Supported     |
| 3.13           | Supported     |
| 3.14           | Supported for core and non-LiteLLM profiles |
| 3.14 beta/RC   | Best effort   |
| < 3.12         | Not supported |

The minimum required version is **Python >= 3.12** as specified in `pyproject.toml`. Source checkouts default to **stable Python 3.14** through `.python-version`; that default does not narrow the supported runtime range to 3.14-only.

## Python Profile Matrix

| Profile | Supported Python | Python 3.14 behavior |
|---------|------------------|----------------------|
| Base package | 3.12-3.14 | Install and run |
| `claude`, `mcp`, `tui`, and non-LiteLLM combinations | 3.12-3.14 | Install and run |
| `litellm` | 3.12-3.13 | Package installs, but the LiteLLM dependency is omitted by its Python marker |
| `all` | 3.12-3.13 for LiteLLM; 3.12-3.14 for remaining extras | Installer selects Python 3.13 when available; direct 3.14 installs omit LiteLLM |
| Source checkout with `--all-extras` | 3.12-3.13 for LiteLLM; 3.14 for remaining extras | Select Python 3.13 for the full profile; Python 3.14 omits LiteLLM |

LiteLLM currently publishes a `<3.14` Python bound. Use Python 3.13 for current LiteLLM examples, or Python 3.12 when validating the lower supported bound. On Python 3.14, the public extras remain installable but omit LiteLLM; requesting the LiteLLM backend then returns remediation for creating a Python 3.13 environment.
