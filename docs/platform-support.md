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

Git worktree discovery decodes Git for Windows output as UTF-8, so repository
paths containing non-ASCII characters do not depend on the active ANSI code
page. This compatibility does not change the experimental support status.

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
| `claude`, `claude-cli`, `claude-sdk`, `mcp`, `tui`, and supported non-LiteLLM combinations | 3.12-3.14 | Install and run |
| `litellm` | 3.12-3.13 | Package installs, but the LiteLLM dependency is omitted by its Python marker |
| `all` | 3.12-3.13 for LiteLLM; 3.12-3.14 for remaining extras | Installer selects Python 3.13 when available; direct 3.14 installs omit LiteLLM |
| Source checkout with `--extra all` | 3.12-3.13 for LiteLLM; 3.14 for remaining extras | Select Python 3.13 for the co-installable profile; Python 3.14 omits LiteLLM |

LiteLLM currently publishes a `<3.14` Python bound. Use Python 3.13 for current LiteLLM examples, or Python 3.12 when validating the lower supported bound. On Python 3.14, the public extras remain installable but omit LiteLLM; requesting the LiteLLM backend then returns remediation for creating a Python 3.13 environment.

## MCP 2 and Claude Package Profiles

The package profile is a process contract, not a dependency-pin workaround:

| Extra | Transport/runtime | Python payload | Combine with `[mcp]`? |
|-------|-------------------|----------------|-----------------------|
| `[claude]` | Default in-process Claude Agent SDK runtime | Exact SDK/Anthropic pins and MCP 1.x graph | **No** — separate process/environment |
| `[claude-cli]` | Claude CLI subprocess for completions and agent workers | None | Yes |
| `[mcp]` | MCP 2 server/client process | `mcp==2.0.0` | Yes, with CLI profiles |
| `[claude-sdk]` | Explicit alias for the Claude Agent SDK runtime | Same exact SDK/Anthropic pins and MCP 1.x graph | **No** — separate process/environment |
| `[all]` | MCP 1.x application bundle | Includes `[claude]`; excludes MCP 2 | **No** — run `[mcp]` separately |

Supported resolver commands include:

```bash
uv tool install 'ouroboros-ai[claude]'
uv tool install 'ouroboros-ai[mcp,claude-cli]'
uv tool install 'ouroboros-ai[claude-sdk]'
```

`ouroboros-ai[mcp,claude]`, `[mcp,claude-sdk]`, and `[all,mcp]` are unsupported.
A normal resolver rejects their MCP 2/MCP 1.x constraints. If an environment is
forced past dependency resolution, setup and `ouroboros mcp doctor` fail before
changing configuration with this message:

```text
Unsupported package profiles: ouroboros-ai[mcp] requires MCP 2, while ouroboros-ai[claude] and ouroboros-ai[claude-sdk] require MCP 1.x. Use [claude] alone for the Claude SDK runtime, or use [claude-cli] with [mcp]; run the MCP 2 server in a separate environment/process.
```

### Migration from 0.50.8 and earlier

| Previous command/state | New action | Result |
|------------------------|------------|--------|
| `[claude]` or `runtime_backend: claude` | Keep `[claude]`; run `ouroboros setup --runtime claude` | Preserves SDK hooks/streaming on MCP 1.x |
| Explicit CLI worker (`runtime_backend: claude_mcp`) | Install `[claude-cli]`; run `ouroboros setup --runtime claude-cli` | Preserves the out-of-process worker used by MCP 2 |
| `[claude-sdk]` | Keep the profile or use `[claude]`; both select the SDK runtime | Makes the alias explicit without changing behavior |
| `[mcp,claude]` silently resolved to 0.50.6 | Replace it with two environments: `[claude]` for the app and `[mcp]` for the server launcher | Prevents backtracking and preserves both MCP majors |
| `[all]` application bundle | Keep `[all]` alone; launch `[mcp]` through isolated `uvx`/`pipx` | Prevents an implicit MCP-major collision |
