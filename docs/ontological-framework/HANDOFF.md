# Ontological Framework - Handoff Document

> Generated: 2026-01-29
> Purpose: Context restoration after session compaction error
> Target Version: v0.4.0

---

## TL;DR

**목표**: Ouroboros에 "본질을 묻는" 철학적 프레임워크 추가

**핵심 개념**: The Two Ancient Methods
1. **Socratic Questioning** (기존) - "Why?", "What if?" → 숨겨진 가정 드러냄
2. **Ontological Analysis** (추가) - "What IS this?", "Root cause or symptom?" → 근원적 문제 찾음

**Consensus 역할 분리**:
- **Advocate** (찬성) - 해결책의 강점 주장
- **Devil's Advocate** (반대) - 온톨로지 질문으로 "증상 치료 아닌가?" 비판
- **Judge** (심판) - 양측 의견 검토 후 최종 판결

---

## 현재 구현 상태

### ✅ 완료 (#1)

**`src/ouroboros/core/ontology_questions.py`** (~240 lines)

```python
# 구현된 내용:
- OntologicalQuestionType (enum): ESSENCE, ROOT_CAUSE, PREREQUISITES, HIDDEN_ASSUMPTIONS
- OntologicalQuestion (frozen dataclass): question, purpose, follow_up
- ONTOLOGICAL_QUESTIONS (dict): 4개 핵심 질문 정의
- OntologicalInsight (frozen dataclass): 분석 결과 타입
- OntologicalAnalyzer (Protocol): 분석기 인터페이스
- build_ontological_prompt(): 단일 질문 프롬프트 생성
- build_devil_advocate_prompt(): Devil's Advocate 전용 프롬프트
```

**`src/ouroboros/evaluation/models.py`** (타입 정의 추가)

```python
# 추가된 내용:
- VoterRole (enum): ADVOCATE, DEVIL, JUDGE
- FinalVerdict (enum): APPROVED, REJECTED, CONDITIONAL
- JudgmentResult (frozen dataclass): Judge 판결 결과
- DeliberationResult (frozen dataclass): 2라운드 토론 결과
- Vote.role 필드 추가 (Optional[VoterRole])
```

---

### 🔄 진행 중 (#2)

**`src/ouroboros/evaluation/consensus.py`**

현재 상태:
- ✅ 모듈 docstring에 두 모드 설명됨
- ✅ `build_devil_advocate_prompt()` import됨
- ✅ `ConsensusEvaluator` (기존 단순 투표) 유지
- ❌ **`DeliberativeConsensus` 클래스 미구현**

**구현해야 할 내용**:

```python
# 추가해야 할 프롬프트 (line ~370 이후)
ADVOCATE_SYSTEM_PROMPT = """You are the ADVOCATE in a deliberative review.
Your role is to find and articulate the STRENGTHS of this solution..."""

JUDGE_SYSTEM_PROMPT = """You are the JUDGE in a deliberative review.
You will receive the ADVOCATE's and DEVIL's positions..."""

# 추가해야 할 클래스
class DeliberativeConsensus:
    """Two-round deliberative consensus evaluator."""

    async def deliberate(
        self, context: EvaluationContext
    ) -> Result[DeliberationResult, ProviderError]:
        # Round 1: Get positions (parallel)
        advocate_task = self._get_position(context, VoterRole.ADVOCATE)
        devil_task = self._get_position(context, VoterRole.DEVIL)

        advocate_result, devil_result = await asyncio.gather(...)

        # Round 2: Judge reviews both
        judge_result = await self._get_judgment(
            context, advocate_result, devil_result
        )

        return Result.ok(DeliberationResult(...))
```

---

### ⏳ 대기 중 (#3, #4)

**`src/ouroboros/bigbang/ontology.py`** (미생성)

```python
# 생성해야 할 내용:
class InterviewOntologyAnalyzer:
    """Ontological analyzer for interview phase."""

    def should_ask_ontological_question(self, round_number: int) -> bool:
        """Every 3rd round starting from round 3."""
        return round_number >= 3 and round_number % 3 == 0

    def select_question_type(self, round_number, context) -> OntologicalQuestionType:
        """Select which ontological question to ask."""
        ...

    def build_ontological_system_prompt(self, ...) -> str:
        """Build system prompt for ontological questioning."""
        ...
```

**`src/ouroboros/bigbang/ambiguity.py`** (확장 필요)

```python
# 수정해야 할 내용:
# 기존 가중치 조정
GOAL_CLARITY_WEIGHT = 0.35      # was 0.40
CONSTRAINT_CLARITY_WEIGHT = 0.25  # was 0.30
SUCCESS_CRITERIA_CLARITY_WEIGHT = 0.25  # was 0.30

# 새 가중치 추가
ONTOLOGY_CLARITY_WEIGHT = 0.15  # NEW

# ScoreBreakdown에 ontology_clarity 필드 추가
# SCORING_SYSTEM_PROMPT에 4번째 기준 추가
```

---

### ⏳ 대기 중 (#5)

**테스트 파일 (미생성)**

```
tests/unit/core/test_ontology_questions.py
tests/unit/bigbang/test_ontology.py
tests/unit/evaluation/test_deliberative_consensus.py
tests/unit/bigbang/test_ambiguity_extended.py
```

---

## 의존성 그래프

```
[0] core/ontology_questions.py  ✅ DONE
     |
     +----------------------------------+
     |                                  |
     v                                  v
[1] bigbang/ontology.py     [2] evaluation/consensus.py
    ⏳ PENDING                  🔄 IN PROGRESS
     |
     v
[3] bigbang/ambiguity.py
    ⏳ PENDING
```

---

## 핵심 설계 결정

| 결정 | 내용 | 이유 |
|------|------|------|
| Interview 통합 | Socratic과 번갈아 사용 (매 3번째 라운드) | 두 방법이 상호 보완적 |
| Consensus 토론 | 2 라운드 (입장 → 판결) | 간결함과 효과의 균형 |
| Ontology 가중치 | 15% | 영향력 있지만 지배적이지 않게 |
| Devil's Advocate | 온톨로지 질문 사용 | Consensus와 Core 연결 |

---

## 파일 변경 요약

| 파일 | 변경 유형 | 예상 라인 | 상태 |
|------|----------|----------|------|
| `core/ontology_questions.py` | New | ~240 | ✅ Done |
| `evaluation/models.py` | Modify | +50 | ✅ Done |
| `evaluation/consensus.py` | Refactor | +150 | 🔄 In Progress |
| `bigbang/ontology.py` | New | ~100 | ⏳ Pending |
| `bigbang/ambiguity.py` | Modify | +50 | ⏳ Pending |

---

## 다음 작업

### 즉시 (#2 완료)

```bash
# consensus.py에 DeliberativeConsensus 클래스 구현
# 1. ADVOCATE_SYSTEM_PROMPT 추가
# 2. JUDGE_SYSTEM_PROMPT 추가
# 3. DeliberativeConsensus 클래스 구현
# 4. run_deliberative_evaluation() 편의 함수 추가
```

### 그 다음 (#3, #4)

```bash
# bigbang/ontology.py 생성
# bigbang/ambiguity.py 확장
```

### 마지막 (#5)

```bash
# 테스트 추가
# 기존 테스트 통과 확인
```

---

## 관련 문서

- Requirements: `docs/ontological-framework/requirements.md`
- Architecture: `docs/ontological-framework/architecture.md`
- Vision: `vision-draft.md`

---

## 참조 코드

### 기존 Interview

```
src/ouroboros/bigbang/interview.py:429  # _build_system_prompt()
```

### 기존 Ambiguity

```
src/ouroboros/bigbang/ambiguity.py:303  # _build_scoring_system_prompt()
```

### 기존 Consensus

```
src/ouroboros/evaluation/consensus.py:199  # ConsensusEvaluator class
```

### CONTRARIAN (Future)

```
src/ouroboros/resilience/lateral.py  # ThinkingPersona.CONTRARIAN
```

---

## 검증 명령어

```bash
# 테스트 실행
uv run pytest tests/unit/core/test_ontology_questions.py -v
uv run pytest tests/unit/evaluation/ -v

# 타입 체크
uv run mypy src/ouroboros/core/ontology_questions.py
uv run mypy src/ouroboros/evaluation/

# 린트
uv run ruff check src/ouroboros/core/ontology_questions.py
```

---

## 핵심 통찰 (이전 대화 요약)

1. **Consensus = 온톨로지적 검증**
   - 현재 Consensus는 "코드 잘 됐어?"만 물음
   - 변경 후: "진짜 근본 해결책이야?"도 물음

2. **Devil's Advocate = 온톨로지 역할**
   - 찬성/반대/심판 구조로 토론
   - Devil이 "증상 치료 아닌가?" 질문

3. **Claude Code와의 융합**
   - Claude Code는 다중 모델 토론 안 함
   - Ouroboros의 Consensus가 고유 가치

---

*이 문서는 세션 복구를 위한 핸드오프 문서입니다.*
