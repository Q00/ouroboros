# Security Policy

## Reporting a Vulnerability

Please **do not** report security vulnerabilities in public GitHub issues, public pull requests, or Discord channels.

Use the most private path available:

1. **Preferred:** GitHub private vulnerability reporting for this repository, if enabled.
2. **Otherwise:** Contact the maintainers through a private maintainer-approved channel.

If you cannot find a private reporting path, do **not** post exploit details publicly. Instead, open a minimal issue requesting a private reporting channel and include **no sensitive details**, proof-of-concept code, tokens, credentials, or exploit steps.

## What to Include

Please include as much of the following as you can:

- A short summary of the vulnerability
- Affected versions, environment, or configuration
- Impact and attack scenario
- Clear reproduction steps
- Any mitigation ideas or proposed fixes

## Handling Expectations

When a valid report is received, maintainers will aim to:

- acknowledge receipt
- assess severity and scope
- work on a fix or mitigation
- coordinate disclosure timing when appropriate

Please keep reports confidential until maintainers confirm that public disclosure is safe.

## Scope

This policy applies to security issues affecting:

- the `ouroboros-ai` package
- the Ouroboros CLI and related source code in this repository
- repository-managed automation or integration points that directly affect users or contributors

## Out of Scope

The following are generally out of scope unless they create a real security impact in this project:

- purely theoretical issues with no plausible attack path
- vulnerabilities in third-party services outside this repository's control
- low-value findings that require unrealistic attacker assumptions

## Public Discussion

Do not post the following in public issues, PRs, or Discord:

- exploit payloads
- credential leaks
- private keys or tokens
- step-by-step abuse instructions
- personal data

