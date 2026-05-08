# `ooo auto` Evolution Plan

> 작업 일자: 2026-05-09
> 목적: `ooo auto`를 "OS 위 도메인 무관 E2E 자동화 엔진"으로 진화시키기 위한 단계별 plan
> 후속: 이 plan을 기반으로 GitHub Issue (RFC)를 발행

---

## 1. 비전

`ooo auto`는 단일 자동화 도구가 아니라, **Ouroboros 코어(OS) 위에 올라간 첫 번째 first-party UserLevel 프로그램**이다 (#725).

목표 상태:
- 한 줄 목표 → Seed → 실행 → **자가 검증 → 자가 회복**까지 자동 수렴
- 도메인 무관 (코딩/디자인/리서치/기획/...)
- 검증 불가 환경에서도 lateral thinking으로 우회
- 모든 결정이 결정론적이거나, 결정론으로 안 되면 사람을 부름

이 plan은 현재 `ooo auto`를 그 비전까지 끌어올리기 위한 갭 8개를 단계별로 닫는다.

---

## 2. 이미 잘 된 것 (보존)

다음은 건드리지 않는다 — 이미 plan의 토대다.

| 자산 | 위치 | 역할 |
|---|---|---|
| Driver 봉인 | `interview_driver.py` | timeout + max_rounds로 LLM 폭주 차단 |
| AutoAnswerer 결정론 | `answerer.py` | 패턴 매칭 답변 + 출처 태그 |
| Seed 결정론 게이트 | `grading.py` | vague 차단, observable 강제, 5개 지표 |
| SeedRepairer | `seed_repairer.py` | 5라운드 결정론 보수 + fingerprint 무한루프 방지 |
| Ledger 출처 추적 | `ledger.py` | evidence-backed vs assumption-only |
| Run handoff 안전 | `pipeline.py` | unknown_no_handle / unknown_timeout 중복 실행 차단 |
| Resume 영속화 | `state.py` | 모든 phase 디스크 저장, 정확한 지점 재개 |

---

## 3. 갭 매트릭스

| # | 갭 | 우선순위 | Phase |
|---|---|---|---|
| 5 | AutoAnswerer가 user_preference 미반영 | 🟢 빠른 win | P1 |
| 4 | `ambiguity_score` 출처 불명 / 결정론 약화 | 🟢 빠른 win | P1 |
| 2 | Self-evolutionary loop 부재 | 🔴 1순위 | P2 |
| 3 | 실행 결과의 AC 자동 검증 부재 | 🔴 1순위 | P2 |
| 1 | 코딩 도메인 강한 편향 | 🔴 1순위 (구조 큼) | P3 |
| 7 | AutoAnswerer 1834 LOC 비대화 | 🟠 장기 (1번과 결합) | P3 |
| 8 | Ledger 충돌 해결 정책 명시성 | 🟠 중간 | P4 |
| 6 | Plugin manager (#725) | ⚪ defer | (외부 RFC) |

---

## 4. Design Pillars (세 추상화로 8개 갭 정리)

세 갭이 셋으로 풀린다.

### Pillar A — DomainProfile (갭 1 + 갭 7 통합)

**핵심 통찰**: "Verifiable"의 의미가 도메인마다 다른데, 코어가 그걸 다 알고 있다. 코어는 *"Verifiable해야 한다"* 만 알아야 한다.

```python
@dataclass(frozen=True)
class DomainProfile:
    name: str  # "coding", "design", "research", ...

    # 코어가 호출, 도메인이 응답
    repo_context_extractor: Callable[[Path], AutoAnswerContext]
    verifiable_predicates: tuple[VerifiablePredicate, ...]
    intent_classifier: IntentClassifier  # 다국어 정규식 책임
    vague_terms: frozenset[str]
    safe_defaults: dict[str, _DefaultSpec]

    # 자동 감지
    detector: Callable[[Path], float]  # 0.0-1.0 confidence


class VerifiablePredicate(Protocol):
    code: str  # "exit_code", "wcag_contrast", "source_count"
    def matches(self, criterion: str) -> bool: ...
    def repair_template(self, criterion: str) -> str: ...
```

**활성화 (3단계 fallback)**:
1. `--domain` 명시
2. 자동 감지 (모든 profile의 `detector(cwd)` confidence 가장 높음)
3. 다중 매칭 (monorepo 등) → predicate **합집합**

**가장 중요한 결과**:
- 코어 `answerer.py` 1834줄이 코어에서 분리됨 (Pillar A가 갭 7도 해결)
- Coding 패턴이 첫 번째 내장 profile이 됨, 다른 도메인은 plugin이 등록
- #725 plugin contract와 자연스럽게 결합

### Pillar B — 결정론 충돌 정책 (갭 8)

**핵심 통찰**: 결정론적 충돌 *해결*이 아니라, 결정론적 *BLOCKED 분기*가 우아하다.

```python
SOURCE_PRIORITY = (
    LedgerSource.USER_GOAL,           # 사람 의도가 항상 이김
    LedgerSource.REPO_FACT,           # 객관적 사실
    LedgerSource.EXISTING_CONVENTION,
    LedgerSource.NON_GOAL,
    LedgerSource.USER_PREFERENCE,     # ← 갭 5에서 추가
    LedgerSource.CONSERVATIVE_DEFAULT,
    LedgerSource.INFERENCE,
    LedgerSource.ASSUMPTION,
)
```

**해결 규칙 (3단계)**:
1. 출처 priority 다름 → 높은 쪽 자동 승리
2. 출처 priority 같음 → confidence 높은 쪽 (tiebreaker)
3. priority + confidence 둘 다 같음 → **BLOCKED, 사람 호출**

**왜 우아한가**:
- 사람 의도(USER_GOAL)가 LLM 추론(INFERENCE)을 언제나 이김
- 자명한 경우만 자동, 애매하면 사람
- 출처 태그가 이미 entry에 박혀 있음 → 추가 메타데이터 0
- 디버깅 가능 ("왜 이 entry가 이겼는가" 설명 가능)

### Pillar C — Self-Evolutionary Loop with Personas (갭 2 + 갭 3 + 갭 4 핵심 보강)

**핵심 통찰**: Evaluate가 안 풀리면 같은 검증 재시도가 아니라, lateral thinking으로 검증 자체를 우회한다.

**상태머신 확장**:
```
... → RUN → EVALUATE ─pass─→ COMPLETE
              │
              fail
              ↓
       UNSTUCK_LATERAL ──pass─→ COMPLETE
              │ (hacker/architect/contrarian persona)
              fail
              ↓
       SEED_REGENERATE ──→ REVIEW (loop back)
              │
              fail
              ↓
           BLOCKED
```

**3단계 회복 시도**:

1. **EVALUATE (qa 결정론)** — Seed의 verifiable AC를 코드가 실제 실행 결과에 자동 매칭
   - AC: `command exits with code 0` → 실행 결과 exit code 검증
   - AC: `http 200` → 실제 응답 status 검증
   - AC: `WCAG AA contrast` → (DomainProfile predicate가 검증 함수 제공)

2. **UNSTUCK_LATERAL (페르소나 lateral)** — 검증 자체가 환경 제약으로 불가능할 때
   - 예시: "Xcode로 빌드 검증" → Xcode 없음 → **hacker 페르소나** 호출
     - hacker가 대안 검증 경로 제안 ("CLI build로 .ipa 생성 확인", "test target만 swift test")
     - 새 AC가 ledger에 `[from-auto][lateral_repair]` 출처로 박힘
   - 페르소나 routing:
     | 실패 양상 | 페르소나 |
     |---|---|
     | 환경 제약 ("도구 없음") | hacker |
     | 정보 부족 ("뭘 봐야 할지 모름") | researcher |
     | 검증 자체가 과스펙 | simplifier |
     | 검증 구조 자체가 잘못 | architect |
     | AC 자체가 잘못된 가정 | contrarian |

3. **SEED_REGENERATE** — lateral도 안 풀리면 Seed의 가정 자체가 틀렸다는 증거. ledger 업데이트 후 SEED_GENERATION 재진입.

**무한루프 방지 (SeedRepairer 패턴 재사용)**:
- `max_evaluate_rounds = 3`
- 같은 fingerprint failure 두 번 → 결정론 보수 불가능 판정 → BLOCKED
- 같은 페르소나 두 번 호출 금지 (lateral도 한 페르소나당 한 번)
- 전체 wall-clock budget (`max_total_seconds`) — `ralph` 패턴(#789) 재사용

**갭 4 보강 (`ambiguity_score`)**:
```python
ambiguity_score = max(
    driver_reported_score,        # LLM 자기 평가 (낙관적)
    deterministic_floor(ledger),  # 코드가 보는 객관적 위험
)

def deterministic_floor(ledger):
    return (
        0.05 * len(ledger.open_gaps())
        + 0.10 * len(ledger.conflicting_entries())
        + 0.05 * assumption_only_section_ratio(ledger)
    )
```

LLM이 "내 Seed 완벽해(0.05)"라고 우겨도, 코드가 보는 floor가 0.30이면 0.30 적용. 자기 합리화 봉인.

### 갭 5 보강 — `USER_PREFERENCE` 출처

Driver만 사용자 메모리에 접근, 답변할 때 새 출처 태그 박음:

```python
class AutoAnswerSource(StrEnum):
    ...
    USER_PREFERENCE = "user_preference"  # ← 신규
```

`[from-auto][user_preference] ...`로 ledger에 추적. Pillar B의 우선순위에서 `EXISTING_CONVENTION`과 `CONSERVATIVE_DEFAULT` 사이에 위치 (관습보단 약하지만 보수 기본값보단 강함).

---

## 5. Phase별 Roadmap

### Phase 1 — Quick Wins (갭 4, 5)

**목표**: 결정론 약점 봉인, user_preference 흐름 개통.

- [ ] `LedgerSource.USER_PREFERENCE` 추가 (`ledger.py`, `answerer.py`)
- [ ] `AutoAnswerSource.USER_PREFERENCE` 추가
- [ ] Driver에 user_preferences 컨텍스트 전달 경로 (memory file 파서)
- [ ] `deterministic_floor(ledger)` 함수 추가 (`grading.py`)
- [ ] `ambiguity_score` 결합 로직 (Seed metadata 채울 때 floor 강제)
- [ ] 테스트: 우선순위, floor 강제, user_preference 출처 태그

**예상 PR 크기**: 작음 (~300 LOC, 테스트 포함)
**의존성**: 없음

### Phase 2 — Self-Evolutionary Loop (갭 2, 3) + Persona 통합

**목표**: RUN 후 검증, 실패 시 lateral 회복, 그래도 실패 시 Seed 재생성.

- [ ] `AutoPhase.EVALUATE` 추가 (`state.py`)
- [ ] `AutoPhase.UNSTUCK_LATERAL` 추가
- [ ] AC 자동 검증기 (`auto/evaluator.py` 신규) — Seed AC ↔ run output 매칭
- [ ] Persona routing table 구현 (실패 양상 → 페르소나)
- [ ] `mcp__ouroboros__ouroboros_lateral_think` 통합 호출
- [ ] EVALUATE → SEED_GENERATION 역방향 transition
- [ ] 무한루프 방지: max_evaluate_rounds, same-fingerprint guard, persona once-only
- [ ] Wall-clock budget (`ralph` 패턴 재사용)
- [ ] 테스트: pass path, lateral fallback, regenerate path, BLOCKED path

**예상 PR 크기**: 중-대 (~800 LOC)
**의존성**: Phase 1 (deterministic floor가 evaluate gate에서도 쓰임)

### Phase 3 — DomainProfile (갭 1 + 갭 7)

**목표**: 도메인 편향 제거, 코어 슬림화. `coding` profile 첫 내장.

- [ ] `DomainProfile` dataclass 정의 (`auto/domain/profile.py`)
- [ ] `VerifiablePredicate` Protocol 정의
- [ ] 기존 `_OBSERVABLE_HINTS` + `observable_patterns` → `coding` profile로 이주
- [ ] 기존 `repo_context.py` → `coding` profile의 extractor로 이주 + extension hook
- [ ] `IntentClassifier` 클래스로 다국어 정규식 분리 (`answerer.py` 슬림화)
- [ ] Profile 자동 감지 (`detector(cwd)`) + `--domain` 플래그
- [ ] 다중 profile 합집합 검증 로직
- [ ] 테스트: profile detection, predicate union, coding profile 회귀

**예상 PR 크기**: 대 (~1500 LOC, 대부분 이주)
**의존성**: Phase 2 (EVALUATE가 profile predicate 사용)

### Phase 4 — Ledger 충돌 정책 (갭 8)

**목표**: 충돌 해결 정책 한 군데 명시화.

- [ ] `SOURCE_PRIORITY` 상수 정의 (`ledger.py`)
- [ ] `resolve_conflict(entry_a, entry_b)` 함수
- [ ] CONFLICTING 상태 entry 발견 시 자동 resolve 시도 → 안 되면 BLOCKED
- [ ] 테스트: priority cases, tiebreaker, BLOCKED path

**예상 PR 크기**: 작음 (~200 LOC)
**의존성**: Phase 1 (USER_PREFERENCE가 우선순위에 들어감)

### Defer — Plugin Manager (갭 6)

`docs/rfc/userlevel-plugins.md`(이미 Accepted) + Q00/ouroboros#725로 이관. DomainProfile이 plugin 등록 contract의 첫 실증 사례가 됨.

---

## 6. 검증 전략

각 phase에서:

1. **결정론 회귀 테스트** — 같은 입력 → 같은 출력 (`pytest -k "deterministic"`)
2. **Goldset** — 알려진 모호한 골 / 알려진 명확한 골을 받아서 등급이 일관되게 나오는지
3. **Phase 2부터** — XCode 시나리오 같은 시뮬레이션: "환경 제약 → lateral → 대안 검증"
4. **Cross-phase**: Phase 3 후 Phase 1/2 회귀 테스트 통과 확인

---

## 7. RFC / Issue 분할 계획

이 plan을 다음과 같이 GitHub에 등록한다.

### Parent RFC Issue
**제목**: `RFC: ooo auto evolution to domain-agnostic self-healing E2E`

내용:
- 비전 + 이미 잘 된 것 (#2)
- 갭 매트릭스 (#3)
- 세 Pillar 디자인 (#4)
- Phase별 sub-issue 링크
- Open questions

### Sub-issues (Phase별)
- `feat(auto): user_preference source + ambiguity_score deterministic floor (P1)`
- `feat(auto): self-evolutionary loop with persona lateral (P2)`
- `feat(auto): DomainProfile + coding profile migration (P3)`
- `feat(auto): ledger conflict resolution policy (P4)`

### 기존 RFC 연결
- `docs/rfc/userlevel-plugins.md` (Accepted)에 "DomainProfile은 plugin contract의 첫 first-party 실증 사례" 한 줄 추가

---

## 8. Open Questions

답이 안 정해진 것 — RFC 코멘트로 의견 받기.

1. **`coding` profile 분리 시점**: 코어와 같이 두고 internal? 아니면 첫 내장 plugin으로 외화?
2. **Persona 호출 비용**: lateral은 LLM 호출 수회. wall-clock budget은 ralph 패턴 재사용으로 OK인지?
3. **Multi-profile 합집합의 충돌**: 코딩 profile은 "exit 0" 강제, 디자인 profile은 "WCAG 통과" 강제. AC 하나가 둘 중 하나만 만족하면 OK인가? (현재 plan: OR 합집합)
4. **EVALUATE 실패 시 cost reporting**: lateral persona가 호출되면 사용자에게 비용 visibility 어떻게 줄 것인가?
5. **`USER_PREFERENCE` 우선순위 위치**: `EXISTING_CONVENTION`보다 위? 아래? (현재 plan: 아래)

---

## 9. 명시적 비-목표

이 plan에서 다루지 않는 것 — 별도 RFC/이슈로 분리.

- Plugin manager 구현 자체 (→ #725, #728-#735)
- 새 LLM provider 추가 (→ providers/)
- TUI/CLI UX 변경
- 메모리 파일 포맷 변경 (Claude Code 측 결정)

---

## 10. 다음 액션

1. 이 plan을 사용자가 review
2. 합의되면 `RFC: ooo auto evolution` issue 발행 (이 문서 본문 그대로 + 약간 다듬어서)
3. Phase별 sub-issue 발행 + parent에 링크
4. Phase 1부터 PR 시작
