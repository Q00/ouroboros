# Language Support & Mechanical Verification

Ouroboros Stage 1 (Mechanical Verification) auto-detects your project's language and runs appropriate lint, build, test, and static analysis commands. No configuration is needed for supported languages.

## Supported Languages

| Language | Detected By | Lint | Build | Test | Static | Coverage |
|----------|------------|------|-------|------|--------|----------|
| Python (uv) | `uv.lock` | `uv run ruff check .` | `uv run python -m py_compile` | `uv run pytest` | `uv run mypy` | `uv run pytest --cov` |
| Python | `pyproject.toml` / `setup.py` | `ruff check .` | `python -m py_compile` | `pytest` | `mypy` | `pytest --cov` |
| Zig | `build.zig` | — | `zig build` | `zig build test` | — | — |
| Rust | `Cargo.toml` | `cargo clippy` | `cargo build` | `cargo test` | — | — |
| Go | `go.mod` | `go vet ./...` | `go build ./...` | `go test ./...` | — | `go test -cover ./...` |
| Java (Maven) | `pom.xml` | — | `mvn clean compile` | `mvn test` | — | — |
| Node (npm) | `package-lock.json` | `npm run lint` | `npm run build` | `npm test` | — | — |
| Node (pnpm) | `pnpm-lock.yaml` | `pnpm lint` | `pnpm build` | `pnpm test` | — | — |
| Node (bun) | `bun.lockb` | `bun lint` | `bun run build` | `bun test` | — | — |
| Node (yarn) | `yarn.lock` | `yarn lint` | `yarn build` | `yarn test` | — | — |

A dash (—) means the check is skipped for that language.

## How Detection Works

Ouroboros scans the project's `working_dir` for marker files in priority order. The first match wins. More specific markers (like `uv.lock`) are checked before generic ones (like `pyproject.toml`).

If no language is detected, all Stage 1 checks are skipped gracefully and evaluation proceeds to Stage 2 (Semantic Evaluation).

## Custom Overrides: `.ouroboros/mechanical.toml`

For languages not in the preset list, or to customize commands for your project, create `.ouroboros/mechanical.toml` in your project root:

```toml
# Override any command. Omitted keys use auto-detected defaults.
# Set to empty string "" to skip a check.

lint = "cargo clippy -- -D warnings"
build = "cargo build --release"
test = "cargo nextest run"
static = ""          # skip static analysis
coverage = ""        # skip coverage

# Optional settings
timeout = 600               # seconds per command (default: 300)
coverage_threshold = 0.5    # minimum coverage ratio (default: 0.7)
```

### Override Priority

1. **`.ouroboros/mechanical.toml`** — highest priority, project-specific
2. **Auto-detected preset** — based on marker files
3. **Skip** — if neither exists, checks are skipped

### Security: Executable Allowlist

Commands from `.ouroboros/mechanical.toml` are validated against an allowlist of known build/test/lint executables before execution. If a command uses an executable not on the list, it is silently skipped and a warning is logged.

This prevents untrusted repositories from running arbitrary commands when evaluated in CI/CD environments. Hardcoded language presets bypass this check since they are trusted.

If your tool is blocked, check the `_ALLOWED_EXECUTABLES` set in `src/ouroboros/evaluation/languages.py` and submit a PR to add it.

### Examples

**Zig project with custom build flags:**
```toml
build = "zig build -Doptimize=ReleaseSafe"
test = "zig build test -Doptimize=Debug"
```

**C/C++ project (no auto-detection):**
```toml
build = "cmake --build build"
test = "ctest --test-dir build"
lint = "clang-tidy src/*.cpp"
```

**Java Maven project with additional checks:**
```toml
build = "mvn clean compile"
test = "mvn test"
lint = "mvn checkstyle:check"
static = "mvn spotbugs:check"
coverage = "mvn verify -Pcoverage"
```

**Haskell project:**
```toml
build = "cabal build"
test = "cabal test"
lint = "hlint src"
```

**Skip all Stage 1 checks:**
```toml
lint = ""
build = ""
test = ""
static = ""
coverage = ""
```

## Using with the MCP Tool

The `ouroboros_evaluate` MCP tool accepts a `working_dir` parameter for language detection:

```
working_dir: "/path/to/your/project"
```

If omitted, it defaults to the current working directory.
