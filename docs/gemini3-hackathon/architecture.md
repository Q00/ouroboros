# Gemini 3 Integration Architecture

> Generated: 2026-02-03
> Approach: Clean (thinking_budget full support)
> Branch: feat/gemini3-hackathon

## Overview

ouroboros 파이프라인 전체에 Gemini 3 API를 통합하여 해커톤 우승을 목표로 함.
기존 LiteLLMAdapter를 확장하고 `thinking_budget` 파라미터를 지원하여 Gemini 3의 extended reasoning 기능을 활용.

## System Diagram

```
                    ┌─────────────────────────────────────┐
                    │         Gemini 3 Integration        │
                    │   (via LiteLLM + thinking_budget)   │
                    └─────────────────┬───────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
        ▼                             ▼                             ▼
┌───────────────┐           ┌─────────────────┐           ┌─────────────────┐
│   Big Bang    │           │   PAL Router    │           │    Consensus    │
│   (Phase 0)   │           │   (Phase 1)     │           │   (Stage 3)     │
├───────────────┤           ├─────────────────┤           ├─────────────────┤
│ • Interview   │           │ Tier Selection: │           │ 3-Model Judge:  │
│ • Ambiguity   │───────────│ • Frugal (1x)   │           │ • GPT-4o        │
│ • Seed Gen    │           │ • Standard(10x) │───────────│ • Claude Sonnet │
│               │           │ • Frontier(30x) │           │ • Gemini 3 ✨   │
│ thinking:1000 │           │   └─ Gemini 3   │           │ thinking: 2000  │
└───────────────┘           └─────────────────┘           └─────────────────┘
```

## Data Flow

```
User Configuration (config.yaml)
    ↓
    economics.tiers.frontier.models: ["google/gemini-3-flash-preview"]
    ↓
[PAL Router or Direct Model Selection]
    ↓
    get_model_for_tier(Tier.FRONTIER, config)
    ↓
    ModelConfig(provider="google", model="gemini-3-flash-preview")
    ↓
[Integration Point: Big Bang / Consensus]
    ↓
    CompletionConfig(
        model="google/gemini-3-flash-preview",
        thinking_budget=2000  # Gemini 3 specific
    )
    ↓
[LiteLLMAdapter]
    ↓
    _build_completion_kwargs() → kwargs["thinking_budget"] = 2000
    ↓
[LiteLLM Library]
    ↓
    litellm.acompletion(**kwargs)
    ↓
[Google Gemini 3 API]
    ↓
    Extended reasoning with thinking budget
    ↓
[Response Processing]
    ↓
    CompletionResponse → Result.ok(response)
```

## Components

| Component | Responsibility | Location | Changes |
|-----------|---------------|----------|---------|
| CompletionConfig | LLM 요청 설정 | `providers/base.py` | +thinking_budget field |
| LiteLLMAdapter | LLM 통신 | `providers/litellm_adapter.py` | +env var, +kwargs |
| TierConfig | Tier별 모델 설정 | `config/models.py` | +Gemini 3 models |
| DeliberativeConfig | Consensus 설정 | `evaluation/consensus.py` | +thinking_budget |
| InterviewEngine | Big Bang 인터뷰 | `bigbang/interview.py` | +conditional thinking |

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| LiteLLM 재사용 | Gemini 이미 지원, 별도 adapter 불필요 |
| thinking_budget Optional | Backwards compatible, 기존 코드 무변경 |
| Configuration-driven | Tier config 수정만으로 모델 교체 가능 |
| Env var: GEMINI_API_KEY | Google 공식 권장 + GOOGLE_API_KEY fallback |

## Implementation Plan

### Phase 1: Core Provider Support (필수)
1. `base.py` - CompletionConfig에 thinking_budget 추가
2. `litellm_adapter.py` - kwargs 전달 + env var 지원
3. `config/models.py` - Gemini 3 tier 추가

### Phase 2: Integration Points
4. `consensus.py` - Judge에 thinking_budget 적용
5. `interview.py` - Big Bang에 조건부 thinking 적용

### Phase 3: Testing & Docs
6. Unit tests for Gemini 3 integration
7. Documentation update

## Files to Modify

| File | Purpose | LOC |
|------|---------|-----|
| `src/ouroboros/providers/base.py` | thinking_budget field | +1 |
| `src/ouroboros/providers/litellm_adapter.py` | env var + kwargs | +5 |
| `src/ouroboros/config/models.py` | tier models | +3 |
| `src/ouroboros/evaluation/consensus.py` | Judge config | +5 |
| `src/ouroboros/bigbang/interview.py` | conditional thinking | +1 |

**Total: ~15 LOC core changes**

## Files to Create

| File | Purpose | LOC |
|------|---------|-----|
| `tests/unit/providers/test_gemini3_integration.py` | Unit tests | ~50 |

## Environment Setup

```bash
# Required
export GEMINI_API_KEY="your-api-key"

# Alternative (fallback)
export GOOGLE_API_KEY="your-api-key"
```

## Model Identifiers

```python
# LiteLLM format
"google/gemini-3-flash-preview"

# Alternative formats (also supported)
"gemini/gemini-3-flash-preview"
```

## Thinking Budget Guidelines

| Use Case | Budget | Rationale |
|----------|--------|-----------|
| Interview Questions | 1000 | 적절한 깊이의 질문 |
| Ambiguity Scoring | 1500 | 세부 분석 필요 |
| Seed Generation | 2000 | 구조화된 추출 |
| Consensus Judge | 2000 | 최종 판정의 신중함 |
