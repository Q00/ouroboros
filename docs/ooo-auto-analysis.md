# `ooo auto` — 분석 노트 및 마케팅 카피

> 작업 일자: 2026-05-09
> 목적: `ooo auto` 기능의 실제 동작 메커니즘 정리 + LinkedIn 발표 카피 초안

---

## 1. 개요

`ooo auto`는 한 줄 목표를 받아 Seed 합성 → 실행 → 자가 평가까지 자동 수렴하는 End-to-End 워크플로우 엔진. Codex Goal과의 결정적 차이는 **목표가 non-verifiable해도 Seed를 결정론적으로 verifiable하게 만든다**는 점.

```bash
ooo auto "주말에 친구들이랑 할 술자리 게임 웹앱 만들어줘"
```

상태머신:
```
CREATED → INTERVIEW → SEED_GENERATION → REVIEW ⇄ REPAIR → RUN → COMPLETE
                                                            ↓
                                                       BLOCKED / FAILED
```

각 단계는 디스크에 영속화되어 `--resume <auto_session_id>`로 정확한 지점에서 재개 가능.

---

## 2. Driver의 역할 — LLM 봉인

`src/ouroboros/auto/interview_driver.py`

```python
"""The driver never relies on the backend to terminate by itself.
All backend calls are timeout-bounded and the loop is capped by max_rounds."""

timeout_seconds: float = 60.0
max_rounds: int = 12
```

- 모든 LLM 호출에 timeout
- 모든 루프에 max round cap
- "백엔드가 알아서 끝내겠지"를 안 믿음

### 질문자 vs 답변자 분리

| 역할 | 구현 | LLM 호출 | 메모리 접근 |
|---|---|---|---|
| 질문자 (Backend) | Hermes/Claude/Codex 등 | O | O |
| 답변자 (AutoAnswerer) | 결정론적 패턴매칭 | X | X |

답변마다 출처 태그 강제:
- `[from-auto][user_goal] ...`
- `[from-auto][repo_fact] ...`
- `[from-auto][existing_convention] ...`
- `[from-auto][conservative_default] ...`
- `[from-auto][assumption] ...`
- `[from-auto][non_goal] ...`
- `[from-auto][blocker] ...`

→ ledger에서 어디서 온 답변인지 모두 추적 가능.

---

## 3. Seed A등급 게이트 — 결정론

`src/ouroboros/auto/grading.py`

### 3.1 모호한 단어 차단

```python
VAGUE_TERMS = ("easy", "intuitive", "robust", "scalable",
               "better", "improve", "optimized",
               "user-friendly", "seamless")

def _is_vague(value: str) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in VAGUE_TERMS)
```

### 3.2 검증 가능성 — 2단계 정규식

```python
def _is_observable(value: str) -> bool:
    # 1단계: observable 힌트 단어가 있는지
    if not any(hint in lowered for hint in _OBSERVABLE_HINTS):
        return False
    # 2단계: 실제 검증 가능한 문장 구조인지
    observable_patterns = (
        r"`[^`]+`\s+(prints|returns|creates|writes|exits|displays)",
        r"\b(api|endpoint|request)\b.+\b(returns|responds|status)\b",
        r"\b(cli|command|process)\b.+\b(exits|returns)\b\s+(with\s+)?(exit\s+code\s+)?0\b",
        r"\b(http\s+)?status\s+2\d\d\b",
        ...
    )
    return any(re.search(pattern, lowered) for pattern in observable_patterns)
```

단어만 끼워넣은 가짜 검증성은 2단계에서 걸러짐.

### 3.3 5개 지표 임계치

```python
grade = SeedGrade.A
if blockers:
    grade = SeedGrade.C
elif (
    scores["coverage"] < 0.90
    or scores["ambiguity"] > 0.20
    or scores["testability"] < 0.85
    or scores["execution_feasibility"] < 0.80
    or scores["risk"] > 0.25
    or findings
):
    grade = SeedGrade.B
```

### 3.4 하드 차단 조건

- `seed.metadata.ambiguity_score > 0.20`
- Seed goal이 ledger goal과 토큰 60% 미만 일치 (LLM이 목표 바꾸는 것 방지)
- ledger 미해결 gap 존재
- high-risk assumption 포함

---

## 4. SeedRepairer — 결정론적 보수

`src/ouroboros/auto/seed_repairer.py`

```python
max_repair_rounds: int = 5

def converge(self, seed, *, ledger=None):
    for _ in range(self.max_repair_rounds):
        if review.grade_result.grade == SeedGrade.A and review.may_run:
            return current, review, history
        repair = self.repair_once(current, review, ledger=ledger)
        if repair.blocker or not repair.changed:
            return current, review, history
        # 같은 high finding 반복 시 중단 (무한루프 방지)
        if high and high == previous_high_fingerprints:
            return current, review, history
```

### 보수 규칙 (LLM 호출 0회)

| Finding | 처리 |
|---|---|
| `vague_acceptance_criteria` | 모호 단어 정규식 제거 + 검증 가능 템플릿 교체 |
| `untestable_acceptance_criteria` | 동일 템플릿 적용 |
| `missing_acceptance_criteria` | 표준 AC 추가 |
| `missing_constraints` | "기존 패턴 재사용, 신규 의존성 금지" 추가 |
| `missing_non_goals` | goal 분석 후 안전한 non-goal 자동 합성 |

5라운드 안에 A 못 만들거나 같은 high finding 반복 시 → `BLOCKED` 상태로 사람 호출.

---

## 5. Codex Goal과의 차이

| | Codex Goal | ooo auto |
|---|---|---|
| 입력 | verifiable한 골 | non-verifiable한 한 줄도 OK |
| 검증 방식 | 실행 결과 통과/실패 | 실행 전 Seed 단계에서 결정론적 명세화 |
| Drift 방어 | 실행 중 재시도 | 실행 시작 전 차단 |
| 적용 도메인 | 테스트 가능한 코딩 | 코딩 + 디자인 + 리서치 + 기획 (플러그인) |

**핵심**: `ooo auto`는 모호한 골을 받아 Seed 단계에서 강제로 verifiable하게 만든다. AI가 실행을 시작할 때는 이미 drift할 자유가 없다.

---

## 6. LinkedIn 발표 카피 (최종본)

```markdown
# Ouroboros에 `ooo auto` 가 추가됐습니다

뭘 만들어야 할지 몰라도, 한 줄만 던지면 됩니다.

ooo auto "주말에 친구들이랑 할 술자리 게임 웹앱 만들어줘"

목표 해석 → Seed 합성 → 실행 → 자가 평가까지 중단 없이 굴러갑니다.
개인적으로는 Codex Goal보다 체감이 좋습니다.

---

## 왜 다른가 — Driver와 Seed

다른 자동화 도구들이 30분 뒤 엉뚱한 결과를 내놓는 이유는 단순합니다.
LLM이 모호한 빈칸을 LLM스럽게 채우다가 산으로 갑니다.

ooo auto는 두 단계로 이걸 막습니다.

1. Driver — LLM을 시간/라운드로 봉인
모든 호출에 timeout, 모든 루프에 cap.
빈칸은 LLM이 아니라 결정론적 응답기가 채우고, 출처 태그를 박습니다.
[repo_fact], [user_goal], [conservative_default], [assumption] —
어디서 온 답변인지 ledger에 추적됩니다.

2. Seed — 모호함을 사전에 제거
실행 전에 결정론적 A등급 게이트를 통과해야 합니다.
easy, intuitive, robust 같은 모호한 단어는 차단.
AC에는 검증 가능한 표현 강제. 못 맞추면 자동 보수, 그래도 안 되면 사람한테 토스.

## Codex Goal과의 결정적 차이

> Codex Goal은 verifiable한 골을 요구한다.
> ooo auto는 non-verifiable한 골을 verifiable한 Seed로 만든다.

## 그래서 — 어떤 도메인이든 E2E

ooo auto는 도메인을 가리지 않는 엔진입니다.
플러그인만 끼우면 어떤 도메인이든 E2E 자동화로 변환됩니다.

- 코딩 플러그인 + ooo auto = 스펙→구현→테스트
- 디자인 플러그인 + ooo auto = 시안→피드백 루프
- 리서치 플러그인 + ooo auto = 조사→리포트
- 여러분의 도메인 + ooo auto = 여러분만의 자동화

폭주를 Seed 단계에서 결정론으로 차단하기 때문에,
verifiable한 코딩이라는 좁은 영역 너머로 갈 수 있습니다.

#AI #Automation #Ouroboros #Agent
```

---

## 7. 향후 검토 포인트

- **AutoAnswerer에 `user_preference` 소스 추가 가능성**: 현재 `AutoAnswerContext`에는 `repo_facts`만 있음. Claude Code 메모리 파일을 파싱해 `user_preferences` 필드로 주입하면, 사용자 취향 반영 + 결정론 + 출처 추적 모두 유지 가능. (사용자가 이번 작업 범위에서는 보류.)
- **플러그인 어댑터 명세**: "어떤 도메인이든 E2E"를 실현하려면 플러그인이 어떤 인터페이스로 ooo auto에 연결되는지 명세 필요.

---

## 참고 파일

- `src/ouroboros/auto/pipeline.py` — AutoPipeline 상태머신
- `src/ouroboros/auto/interview_driver.py` — Driver 봉인 로직
- `src/ouroboros/auto/answerer.py` — 결정론적 응답기
- `src/ouroboros/auto/grading.py` — A등급 게이트
- `src/ouroboros/auto/seed_repairer.py` — 결정론적 보수 루프
- `src/ouroboros/auto/ledger.py` — Seed Draft Ledger (출처 추적)
- `skills/auto/SKILL.md` — 사용자 진입점
