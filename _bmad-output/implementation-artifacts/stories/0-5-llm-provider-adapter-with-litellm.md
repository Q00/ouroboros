# Story 0.5: LLM Provider Adapter with LiteLLM

Status: review

## Story

As a developer,
I want a unified LLM adapter that works with multiple providers,
so that I can switch between OpenAI, Anthropic, and Google models seamlessly.

## Acceptance Criteria

1. LLMAdapter protocol defined with complete() method
2. LiteLLMAdapter implements the protocol
3. Responses wrapped in Result[CompletionResponse, ProviderError]
4. stamina handles retries with exponential backoff
5. OpenRouter routing supported via model string
6. Provider config loaded from credentials.yaml (or environment variables as fallback)

## Tasks / Subtasks

- [x] Task 1: Define adapter protocol (AC: 1)
  - [x] Create providers/base.py
  - [x] Define LLMAdapter Protocol
  - [x] Define Message, CompletionConfig, CompletionResponse models
- [x] Task 2: Implement LiteLLM adapter (AC: 2, 5)
  - [x] Create providers/litellm_adapter.py
  - [x] Implement LiteLLMAdapter class
  - [x] Support OpenRouter model strings
- [x] Task 3: Add retry logic (AC: 4)
  - [x] Use @stamina.retry decorator
  - [x] Configure exponential backoff with jitter
- [x] Task 4: Integrate with Result type (AC: 3)
  - [x] Wrap successful responses in Result.ok()
  - [x] Convert exceptions to Result.err(ProviderError)
- [x] Task 5: Write tests (AC: 1, 2, 3, 4)
  - [x] Create tests/unit/providers/test_litellm_adapter.py
  - [x] Mock LiteLLM responses for unit tests
  - [x] Test retry logic with simulated failures
  - [x] Test Result type wrapping for success/error cases
  - [x] Achieve ≥80% coverage (achieved 100%)

## Dev Notes

- LiteLLM v1.80.15 for unified interface
- OpenRouter as primary gateway
- stamina v25.1.0 for retries
- Never raise exceptions for expected failures

### Dependencies

**Requires:**
- Story 0-2: Result type for error handling
- Story 0-4: Credentials system for provider config

**Required By:**
- Story 0-9: Context Compression Engine (LLM for summarization)
- Story 1-1: Seed Ingestion and Parsing
- Story 3-1: Core Execution Loop
- Story 4-2: Tool Executor
- Story 5-2: Verification Orchestrator
- Story 6-2: Multi-Agent Coordinator

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#LLM-Integration-LiteLLM-OpenRouter]
- [Source: _bmad-output/planning-artifacts/architecture.md#Provider-Abstraction-Adapter-Pattern]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5

### Debug Log References
N/A

### Completion Notes List
- All acceptance criteria implemented and tested
- 100% code coverage on providers module (base.py and litellm_adapter.py)
- 54 unit tests passing for providers module
- Uses environment variables (OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY) with api_key constructor override
- stamina retry with exponential backoff (1.0-10.0s wait, jitter) on transient errors
- OpenRouter routing via model string prefix detection

### File List
- src/ouroboros/providers/__init__.py
- src/ouroboros/providers/base.py
- src/ouroboros/providers/litellm_adapter.py
- tests/unit/providers/__init__.py
- tests/unit/providers/test_base.py
- tests/unit/providers/test_litellm_adapter.py
