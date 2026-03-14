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
