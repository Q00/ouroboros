# Ontological Question Framework Requirements

> Generated: 2026-01-29
> Status: Clarified
> Version Target: v0.4.0

## Original Request

"Ouroboros에 Ontological Analysis 기능 추가 - Interview 단계에서 본질적 질문, Consensus에서 Devil's Advocate, Resilience에서 CONTRARIAN persona가 공유하는 질문 프레임워크 구현"

## Clarified Specification

### Goal

Ouroboros 전체에 "본질을 묻는" 철학적 프레임워크 구현. Socratic Questioning과 함께 Ontological Analysis를 통해 "이게 진짜 문제인가?"를 검증.

**The Two Ancient Methods**:
- **Socratic Questioning**: "Why?", "What if?", "Is it necessary?" - 숨겨진 가정 드러냄
- **Ontological Analysis**: "What IS this?", "Root cause or symptom?", "What's the essence?" - 근원적 문제 찾음

### Scope (v0.4.0)

| Component | Type | Description |
|-----------|------|-------------|
| `core/ontology_questions.py` | New | 공유 질문 프레임워크, 타입 정의, Protocol |
| `bigbang/ontology.py` | New | Interview 단계 Ontological Analysis |
| `evaluation/consensus.py` | Refactor | Deliberative Consensus (Advocate/Devil/Judge) |

### Out of Scope (v0.5.0+)

- Stagnation → Lateral Thinking 연결
- Escalation 연결
- Skill Ecosystem

### Constraints

1. **Interview Integration**: Socratic과 병행 (번갈아 질문)
2. **Consensus Depth**: 2 라운드 (입장 제시 → Judge 판결)
3. **Backward Compatibility**: 기존 테스트 모두 통과
4. **Shared Philosophy**: 동일한 Ontological Question Framework가 여러 곳에서 사용됨

### Success Criteria

- [ ] `core/ontology_questions.py` 구현 완료
  - [ ] ONTOLOGICAL_QUESTIONS 정의
  - [ ] OntologicalInsight 타입 정의
  - [ ] OntologicalAnalyzer Protocol 정의
- [ ] `bigbang/ontology.py` 구현 완료
  - [ ] Interview에서 온톨로지 질문 생성 가능
  - [ ] Ambiguity Score + Ontology Score 복합 점수
- [ ] `evaluation/consensus.py` 리팩터링 완료
  - [ ] Advocate/Devil/Judge 역할 분리
  - [ ] Devil's Advocate에 온톨로지 질문 통합
  - [ ] 2 라운드 토론 (입장 → 판결)
- [ ] 기존 테스트 통과
- [ ] 새 테스트 추가

## Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Interview 통합 방식 | Socratic과 병행 | 두 방법이 상호 보완적 |
| Consensus 토론 깊이 | 2 라운드 | 간결함과 효과의 균형 |
| 구현 범위 | v0.4.0만 | 점진적 구현, 검증 후 확장 |
| PR 대기 | 없음 | core 모듈은 독립적 |

## Technical Context

### Dependency Graph

```
[0] core/ontology_questions.py (의존성 없음)
     |
     +----------------------------------+
     |                                  |
     v                                  v
[1] bigbang/ontology.py     [2] evaluation/consensus.py
```

### Sync Points

Ontological Question Framework가 다음 3곳에서 공유됨:

| Component | Phase | Usage |
|-----------|-------|-------|
| Interview Ontological Analysis | Phase 0 | 사용자에게 질문으로 제시 |
| Consensus Devil's Advocate | Phase 4 | 결과물 평가 시 적용 |
| CONTRARIAN Persona (future) | Phase 3 | Lateral Thinking에서 사용 |

### Ontological Questions (Core)

```python
ONTOLOGICAL_QUESTIONS = {
    "essence": "What IS this, really?",
    "root_cause": "Is this the root cause or a symptom?",
    "prerequisites": "What must exist first?",
    "hidden_assumptions": "What are we assuming?"
}
```

## Related Documents

- Vision: `/vision-draft.md`
- Architecture: `docs/ontological-framework/architecture.md` (TBD)
- Implementation: `docs/ontological-framework/implementation.md` (TBD)

## References

- Existing Interview: `src/ouroboros/bigbang/interview.py`
- Existing Ambiguity: `src/ouroboros/bigbang/ambiguity.py`
- Existing Consensus: `src/ouroboros/evaluation/consensus.py`
- Existing Lateral: `src/ouroboros/resilience/lateral.py`
