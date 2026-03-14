# Ouroboros Documentation

> The serpent that devours itself to be reborn anew.

Ouroboros is a specification-first workflow engine for AI coding agents. It transforms ambiguous human requirements into clear, executable specifications through Socratic questioning and ontological analysis -- then runs them on your choice of runtime backend.

## Documentation Index

### Getting Started

- [Getting Started Guide](./getting-started.md) - Installation, configuration, and quick start tutorial
- [Platform Support](./platform-support.md) - Python versions, OS compatibility, and supported runtime backends

### Runtime Guides

- [Claude Code](./runtime-guides/claude-code.md) - Setup, configuration, and usage with the Claude Code runtime backend
- [Codex CLI](./runtime-guides/codex.md) - Setup, configuration, and usage with the OpenAI Codex CLI runtime backend
- [Runtime Capability Matrix](./runtime-capability-matrix.md) - Feature comparison across runtime backends

### Architecture

- [System Architecture](./architecture.md) - Six-phase architecture, runtime abstraction layer, and core concepts
- [CLI Reference](./cli-reference.md) - Command-line interface flags and options

### API Reference

- [API Reference Index](./api/README.md) - Complete API documentation
  - [Core Module](./api/core.md) - Result type, Seed, and error handling
  - [MCP Module](./api/mcp.md) - Model Context Protocol integration

### Guides

- [Quick Start](./guides/quick-start.md) - Get running in under 10 minutes
- [Seed Authoring Guide](./guides/seed-authoring.md) - YAML structure, field reference, examples
- [TUI Usage Guide](./guides/tui-usage.md) - Dashboard, screens, keyboard shortcuts
- [CLI Usage Guide](./guides/cli-usage.md) - Command-line interface reference
- [Common Workflows](./guides/common-workflows.md) - Recipes for typical scenarios

### Contributing

- [Contributing Guide](../CONTRIBUTING.md) - How to set up, code, test, and submit PRs
- [Architecture for Contributors](./contributing/architecture-overview.md) - How modules connect
- [Testing Guide](./contributing/testing-guide.md) - Writing and running tests
- [Key Patterns](./contributing/key-patterns.md) - Result type, immutability, event sourcing, protocols

### Security

- [Security Policy](../SECURITY.md) - Vulnerability reporting and security model

## Key Concepts

### The Six Phases

1. **Big Bang (Phase 0)** - Socratic and ontological questioning to crystallize requirements into a Seed (Ambiguity <= 0.2)
2. **PAL Router (Phase 1)** - Progressive Adaptive LLM selection (Frugal -> Standard -> Frontier)
3. **Double Diamond (Phase 2)** - Discover, Define, Design, Deliver with recursive decomposition
4. **Resilience (Phase 3)** - Stagnation detection and lateral thinking via persona rotation
5. **Evaluation (Phase 4)** - Three-stage verification (Mechanical, Semantic, Consensus)
6. **Secondary Loop (Phase 5)** - TODO registry and batch processing

### Economic Model

| Tier | Cost | When |
|:----:|:----:|------|
| FRUGAL | 1x | complexity < 0.4 |
| STANDARD | 10x | complexity < 0.7 |
| FRONTIER | 30x | critical decisions |

### Core Principles

- **Frugal by default, rigorous in verification** - Start with the simplest approach, escalate only when needed
- **Ambiguity threshold** - Requirements must have ambiguity score <= 0.2 before execution begins
- **Lateral thinking** - When stuck, switch persona and think differently rather than retry harder

## Quick Links

- [GitHub Repository](https://github.com/Q00/ouroboros)
- [PyPI Package](https://pypi.org/project/ouroboros/)

## License

MIT License
