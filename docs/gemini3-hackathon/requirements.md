# Gemini 3 Hackathon - ouroboros Integration

> Generated: 2026-02-03
> Status: Clarified
> Deadline: 2026-02-09 (6 days remaining)

## Original Request

> "Gemini 3 해커톤 우승을 위한 ouroboros + Gemini 3 API 통합"

## Clarified Specification

### Goal
ouroboros 파이프라인 전체에 Gemini 3 API를 전략적으로 통합하여 해커톤 우승

### Scope

**In Scope:**
- Big Bang (Socratic/Ontological Analysis) - Gemini 3 reasoning 활용
- PAL Router - Gemini 3를 Frontier tier로 배치
- Consensus Evaluation - Judge 역할에 Gemini 3 활용
- Google AI SDK 직접 통합 (새 adapter 구현)

**Out of Scope:**
- UI/Frontend 개발
- AI Studio app (코드 기반으로 진행)

### Constraints

- **Deadline**: 2026.02.09 5:00 PM PT (KST: 02.10 10:00 AM)
- **Priority**: 기술 완성도 > 데모 영상
- **Required**: Gemini 3 API 필수 사용
- **Language**: 영어 지원 필수

### Success Criteria

1. [ ] Gemini 3 adapter 동작 (Google AI SDK)
2. [ ] PAL Router에서 Gemini 3 Frontier tier 선택 가능
3. [ ] Consensus Judge에 Gemini 3 사용
4. [ ] 데모 가능한 상태 (working demo)
5. [ ] 제출 요건 충족:
   - [ ] 데모 영상 (≤3분, YouTube/Vimeo)
   - [ ] 설명 (~200 words)
   - [ ] 공개 코드 저장소

## Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| 통합 범위 | 전체 파이프라인 최적화 | Innovation 30% 점수 극대화 |
| API 접근 방식 | Google AI SDK 직접 사용 | Technical Execution 40% 점수 극대화 |
| 우선순위 | 기술 완성도 우선 | 심사 기준 70%가 기술+혁신 |

## Judging Criteria Alignment

| Criteria | Weight | Our Strategy |
|----------|--------|--------------|
| Technical Execution | 40% | Google AI SDK 직접 통합, 코드 품질 |
| Innovation/Wow Factor | 30% | "AI가 AI를 의심한다" - Socratic + Ontological |
| Potential Impact | 20% | 85% 비용 절감, GIGO 문제 해결 |
| Presentation/Demo | 10% | 모호한 요구사항 → 명확한 Seed 변환 데모 |

## Technical Stack

- **Python**: 3.14+
- **Gemini 3 SDK**: google-generativeai
- **Existing**: LiteLLM (for other providers), structlog, stamina
- **Testing**: pytest, 97%+ coverage target

## Worktree Setup

```
/Users/jaegyu.lee/Project/ouroboros          ← main (기존 작업)
/Users/jaegyu.lee/Project/ouroboros-gemini3  ← feat/gemini3-hackathon (이 작업)
```
