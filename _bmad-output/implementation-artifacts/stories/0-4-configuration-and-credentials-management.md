# Story 0.4: Configuration and Credentials Management

Status: review

## Story

As a developer,
I want to configure Ouroboros via ~/.ouroboros/ directory,
so that API keys and settings are stored securely and persistently.

## Acceptance Criteria

1. ~/.ouroboros/ directory created on first run
2. config.yaml template generated with default settings
3. credentials.yaml template with provider API key placeholders
4. credentials.yaml has chmod 600 permissions automatically
5. Config validates against Pydantic models
6. `ouroboros config init` command creates directory and templates
7. Malformed config.yaml returns clear validation error message
8. Missing credentials.yaml prompts user to run `ouroboros config init`

## Tasks / Subtasks

- [x] Task 1: Create config models (AC: 5)
  - [x] Create config/models.py with Pydantic models
  - [x] Define OuroborosConfig, ProviderConfig, TierConfig
- [x] Task 2: Implement config loader (AC: 1, 2, 3)
  - [x] Create config/loader.py
  - [x] Implement load_config() function
  - [x] Implement create_default_config() function
- [x] Task 3: Handle credentials securely (AC: 4)
  - [x] Set chmod 600 on credentials.yaml
  - [x] Validate credentials structure
- [ ] Task 4: Create CLI command (AC: 6) - SKIPPED (handled by Story 0-6)
  - [ ] Add `config init` command to CLI
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (97% achieved)
  - [x] Test error cases

## Dev Notes

- Config location: ~/.ouroboros/
- Files: config.yaml, credentials.yaml, data/, logs/
- Use YAML for human-readable configs
- Pydantic v2 for validation

### Project Structure Notes

```
~/.ouroboros/
├── config.yaml           # Main configuration
├── credentials.yaml      # API keys (chmod 600)
├── data/
│   └── ouroboros.db      # SQLite database
└── logs/
    └── ouroboros.log     # Structured logs
```

### Dependencies

**Requires:**
- Story 0-1: Project Initialization with uv

**Required By:**
- Story 0-5: Structured Logging with structlog
- Story 2-1: Provider Configuration (Epic 2)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Configuration-Location]

## Dev Agent Record

### Agent Model Used
claude-opus-4-5-20251101

### Debug Log References
N/A

### Completion Notes List
- Implemented full configuration module with Pydantic v2 models
- Created OuroborosConfig, TierConfig, CredentialsConfig, and related models
- Implemented config/loader.py with load_config(), load_credentials(), create_default_config()
- credentials.yaml automatically receives chmod 600 permissions
- Clear validation error messages with field locations
- Missing config prompts user to run `ouroboros config init`
- 77 unit tests with 97% code coverage
- Task 4 (CLI command) skipped as it's handled by Story 0-6

### File List
- src/ouroboros/config/__init__.py
- src/ouroboros/config/models.py
- src/ouroboros/config/loader.py
- tests/unit/config/__init__.py
- tests/unit/config/test_models.py
- tests/unit/config/test_loader.py
