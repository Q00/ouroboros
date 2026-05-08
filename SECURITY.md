# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| latest  | Yes                |
| < latest | Best-effort       |

We recommend always running the latest release of Ouroboros to benefit from
the most recent security fixes and improvements.

## Reporting a Vulnerability

If you discover a security vulnerability in Ouroboros, please report it
responsibly. **Do not open a public GitHub issue for security vulnerabilities.**

### How to Report

Send an email to **jqyu.lee@gmail.com** with the following information:

- A description of the vulnerability and its potential impact
- Steps to reproduce the issue, including any relevant configuration
- The version(s) of Ouroboros affected
- Any suggested mitigations or fixes, if available

### What to Expect

- **Acknowledgement**: We will acknowledge receipt of your report within
  48 hours.
- **Assessment**: We will investigate and provide an initial assessment within
  7 business days.
- **Resolution**: For confirmed vulnerabilities, we aim to release a fix
  within 30 days of validation, depending on severity and complexity.
- **Disclosure**: We will coordinate with you on public disclosure timing.
  We follow responsible disclosure practices and will credit reporters
  unless anonymity is requested.

### Severity Classification

We use the following severity levels to prioritize fixes:

- **Critical** -- Remote code execution, credential exposure, or complete
  bypass of security controls.
- **High** -- Privilege escalation, significant data leakage, or denial of
  service with low complexity.
- **Medium** -- Limited information disclosure, configuration weaknesses,
  or issues requiring significant user interaction to exploit.
- **Low** -- Minor issues with minimal security impact.

## Security Considerations

Ouroboros is a workflow engine that orchestrates AI agent runtimes. Users
should be aware of the following security considerations:

- **Workflow specifications** can invoke arbitrary tool calls through the
  configured runtime backend. Review workflow files before execution, especially
  those from untrusted sources.
- **API keys and credentials** should be managed through environment variables
  or secure secret stores, never committed to workflow specifications or
  version control.
- **Runtime backends** (Claude Code, Codex CLI) have their own security
  models. Consult each runtime's documentation for platform-specific
  security guidance.

## Scope

This security policy covers the `ouroboros-ai` Python package and its
official documentation. Third-party plugins, runtime backends, and
downstream integrations are outside the scope of this policy.

## Installation Channel

Official installation channels for Ouroboros:

- **PyPI** -- `ouroboros-ai`, installable via `pipx`, `uv tool`, or `pip`.
- **GitHub repository** -- `https://github.com/Q00/ouroboros`. The
  `scripts/install.sh` one-liner installer is hosted here and is also
  advertised in `README.md`. It auto-detects an available CLI runtime and
  registers the MCP server, which is why it remains the default Quick Start
  path.
- **uv** -- when uv is required, prefer package-manager installs
  (`pipx install uv`, `pip install --user uv`, `brew install uv`) before
  falling back to the vendor one-liner (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
  Both `scripts/install.sh` and `skills/setup/SKILL.md` surface these
  alternatives so users in policy-restricted environments are not forced
  through pipe-to-shell.

If you operate in an environment that prohibits piping remote scripts to
a shell, use the PyPI path (`pipx install ouroboros-ai` then
`ouroboros setup`) -- it produces the same final configuration.

## Bulk-Disclosure Scanner Policy

Automated scanners that bulk-file pattern-matched findings (for example,
`curl | sh` detectors) **should still follow the private reporting
channel above before opening a public issue**, even for low-confidence
matches. This gives maintainers a chance to triage, deduplicate, and
respond before the report becomes externally indexable.

Conventional installer patterns shared with peer tooling (rustup, uv,
Homebrew, Deno, Bun, etc.) are treated as *Medium / hardening* at most
under the severity ladder above; they are not classified as *Critical*
solely because they match a `curl | sh` pattern.
