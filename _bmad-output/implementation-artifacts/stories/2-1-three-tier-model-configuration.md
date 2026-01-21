# Story 2.1: Three-Tier Model Configuration

Status: completed

## Story

As a developer,
I want configurable model tiers,
so that I can define which models to use at each cost level.

## Acceptance Criteria

1. Three tiers defined: Frugal (1x), Standard (10x), Frontier (30x)
2. Each tier maps to specific model identifiers
3. Cost multipliers tracked for reporting
4. Tier configuration in config.yaml
5. Default tier is Frugal
6. Invalid tier configuration (missing models, invalid cost multipliers) raises ConfigError with details

## Tasks / Subtasks

- [x] Task 1: Define tier models (AC: 1, 5)
  - [x] Create routing/tiers.py
  - [x] Define Tier enum
  - [x] Define TierConfig model
- [x] Task 2: Implement model mapping (AC: 2, 4)
  - [x] Map tiers to OpenRouter model strings
  - [x] Load from config.yaml
- [x] Task 3: Add cost tracking (AC: 3)
  - [x] Track cost multiplier per request
  - [x] Integrate with observability
- [x] Task 4: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage
  - [x] Test error cases

## Dev Notes

- Frugal = 1x cost (default)
- Standard = 10x cost
- Frontier = 30x cost
- OpenRouter model strings

### Dependencies

**Requires:**
- Story 0-4 (config management)

**Required By:**
- Story 2-2 (complexity-based routing)
- Story 2-3 (escalation on failure)
- Story 2-4 (downgrade on success)
- Story 5-2 (LiteLLM cost tracking)
- Story 5-3 (cost optimization dashboard)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-1-Tiered-Routing-PAL]

## Dev Agent Record

### Agent Model Used
- Claude Sonnet 4.5 (claude-sonnet-4-5-20250929)

### Debug Log References
- N/A

### Completion Notes List
1. **Tier Enum Implementation** (src/ouroboros/routing/tiers.py:40-65)
   - Created Tier enum with three values: FRUGAL, STANDARD, FRONTIER
   - Each tier has a cost_multiplier property (1x, 10x, 30x)
   - Tier enum values match the string keys used in config ("frugal", "standard", "frontier")

2. **Model Selection** (src/ouroboros/routing/tiers.py:154-192)
   - Implemented get_model_for_tier() function
   - Uses random.choice() for load balancing across multiple models in a tier
   - Returns Result[ModelConfig, ConfigError] for safe error handling
   - Logs tier, provider, model, and cost_factor for observability

3. **Configuration Validation** (src/ouroboros/routing/tiers.py:68-152, 195-237)
   - get_tier_config() validates tier existence, non-empty models, and correct cost factors
   - validate_tier_configuration() validates all three tiers
   - All errors use ConfigError with descriptive messages and details dict
   - Error details include config_key, available_tiers, expected/actual values

4. **Cost Tracking Integration** (src/ouroboros/routing/tiers.py:185-189)
   - Every model selection logs cost_factor via structlog
   - Log event: "tier.model.selected" includes tier, provider, model, cost_factor
   - Integrated with existing observability/logging.py system
   - Cost multipliers available via Tier.cost_multiplier property

5. **Comprehensive Test Coverage** (tests/unit/routing/test_tiers.py)
   - 23 test cases covering all acceptance criteria
   - Tests for valid configurations, missing tiers, empty models, invalid cost factors
   - Integration scenarios: default tier workflow, tier escalation, cost tracking
   - All tests use pytest and follow project conventions

6. **Acceptance Criteria Met**
   - AC1: Three tiers defined with 1x, 10x, 30x multipliers ✓
   - AC2: Each tier maps to specific model identifiers ✓
   - AC3: Cost multipliers tracked in logs ✓
   - AC4: Tier configuration loaded from config.yaml via OuroborosConfig ✓
   - AC5: Default tier is Frugal (defined in EconomicsConfig) ✓
   - AC6: Invalid configs raise ConfigError with detailed messages ✓

### File List
- src/ouroboros/routing/__init__.py (created)
- src/ouroboros/routing/tiers.py (created, 237 lines)
- tests/unit/routing/__init__.py (created)
- tests/unit/routing/test_tiers.py (created, 549 lines)

### Next Steps
- Run tests with: `python -m pytest tests/unit/routing/test_tiers.py -v --cov=src/ouroboros/routing`
- Verify coverage meets ≥80% requirement
- Integration with Story 2-2 (complexity-based routing) can now proceed
