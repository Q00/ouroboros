<p align="right">
  <a href="./README.md">English</a> | <strong>한국어</strong>
</p>

<p align="center">
  <br/>
  ◯ ─────────── ◯
  <br/><br/>
  <img src="./docs/images/ouroboros.png" width="520" alt="Ouroboros">
  <br/><br/>
  <strong>O U R O B O R O S</strong>
  <br/><br/>
  ◯ ─────────── ◯
  <br/>
</p>


<p align="center">
  <strong>프롬프트를 멈추세요. 명세를 시작하세요.</strong>
  <br/>
  <sub>AI가 코드를 한 줄이라도 작성하기 전에, 막연한 아이디어를 검증된 명세로 바꿔주는 Claude Code 플러그인.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#빠른-시작">빠른 시작</a> ·
  <a href="#경이에서-존재론으로">철학</a> ·
  <a href="#순환-구조">원리</a> ·
  <a href="#명령어">명령어</a> ·
  <a href="#아홉-개의-사고">에이전트</a>
</p>

---

> *AI는 무엇이든 만들 수 있다. 어려운 건 무엇을 만들어야 하는지 아는 것이다.*

Ouroboros는 **명세 우선 AI 개발 시스템**입니다. 코드가 단 한 줄도 작성되기 전에 소크라테스식 질문법과 존재론적 분석을 통해 숨겨진 가정을 드러냅니다.

대부분의 AI 코딩은 **출력**이 아니라 **입력**에서 실패합니다. 병목은 AI의 능력이 아니라 인간의 명확성입니다. Ouroboros는 기계가 아니라 인간을 교정합니다.

---

## 경이에서 존재론으로

> *경이 → "어떻게 살아야 하는가?" → "'삶'이란 무엇인가?" → 존재론*
> — 소크라테스

이것이 Ouroboros의 철학적 엔진입니다. 모든 위대한 질문은 더 깊은 질문으로 이어지고, 그 더 깊은 질문은 언제나 **존재론적**입니다 — *"이것을 어떻게 하지?"*가 아니라 *"이것이 정확히 무엇이지?"*

```text
   경이(Wonder)                     존재론(Ontology)
     💡                               🔬
"내가 원하는 게 뭐지?"    →    "내가 원하는 그것은 무엇인가?"
"Task CLI를 만들자"      →    "Task란 무엇인가? Priority란 무엇인가?"
"인증 버그를 고치자"      →    "이것이 근본 원인인가, 아니면 증상인가?"
```

이것은 추상화를 위한 추상화가 아닙니다. *"Task란 무엇인가?"*라는 질문에 답할 때 — 삭제 가능한가 보관 가능한가? 개인용인가 팀용인가? — 특정 유형의 재작업을 통째로 없앨 수 있습니다. **존재론적 질문이야말로 가장 실용적인 질문입니다.**

Ouroboros는 **Double Diamond** 구조를 통해 이 철학을 아키텍처에 녹여냅니다:

```text
    ◇ 경이            ◇ 설계
   ╱  (발산)         ╱  (발산)
  ╱    탐색         ╱    창조
 ╱                 ╱
◆ ──────────── ◆ ──────────── ◆
 ╲                 ╲
  ╲    정의         ╲    전달
   ╲  (수렴)        ╲  (수렴)
    ◇ 존재론         ◇ 평가
```

첫 번째 다이아몬드는 **소크라테스적**입니다: 질문으로 발산하고 존재론적 명확성으로 수렴합니다. 두 번째 다이아몬드는 **실용적**입니다: 설계 옵션으로 발산하고 검증된 결과물로 수렴합니다. 각 다이아몬드는 이전 단계를 전제로 합니다 — 이해하지 못한 것은 설계할 수 없습니다.

---

## 빠른 시작

**1단계 — 플러그인 설치** (터미널에서):

```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**2단계 — 셋업 실행** (Claude Code 세션 내에서):

```text
# Claude Code를 시작한 후 입력:
ooo setup
```

> `ooo` 명령어는 Claude Code 스킬입니다 — 터미널이 아닌 **Claude Code 세션 안에서** 실행됩니다.
> 셋업은 MCP server를 전역으로 등록(1회)하고, 선택적으로 프로젝트의 CLAUDE.md에 Ouroboros 참조 블록을 추가합니다.

**3단계 — 빌드 시작:**

```text
ooo interview "I want to build a task management CLI"
```

<details>
<summary><strong>무슨 일이 일어났나요?</strong></summary>

```text
ooo interview  →  소크라테스식 질문이 12개의 숨겨진 가정을 드러냄
ooo seed       →  답변을 불변 명세로 결정화 (Ambiguity: 0.15)
ooo run        →  Double Diamond 분해를 통해 실행
ooo evaluate   →  3단계 검증: Mechanical → Semantic → Consensus
```

뱀이 한 바퀴를 완성했습니다. 순환할 때마다, 이전보다 더 많이 알게 됩니다.

</details>

---

## 순환 구조

우로보로스(자기 꼬리를 삼키는 뱀)는 장식이 아닙니다. 이것 자체가 아키텍처입니다:

```text
    Interview → Seed → Execute → Evaluate
        ↑                           ↓
        └──── Evolutionary Loop ────┘
```

각 순환은 반복이 아니라 **진화**합니다. 평가의 출력이 다음 세대의 입력으로 피드백되며, 시스템이 무엇을 만들고 있는지 진정으로 이해할 때까지 계속됩니다.

| 단계 | 수행 내용 |
|:------|:-------------|
| **Interview** | 소크라테스식 질문으로 숨겨진 가정을 드러냄 |
| **Seed** | 답변을 불변 명세로 결정화 |
| **Execute** | Double Diamond: 발견 → 정의 → 설계 → 전달 |
| **Evaluate** | 3단계 게이트: Mechanical ($0) → Semantic → Multi-Model Consensus |
| **Evolve** | 경이 *("아직 모르는 것은 무엇인가?")* → 성찰 → 다음 세대 |

> *"여기서 우로보로스가 자기 꼬리를 삼킵니다: 평가의 출력이*
> *다음 세대 Seed 명세의 입력이 됩니다."*
> — `reflect.py`

존재론 유사도가 0.95 이상이 되면 수렴에 도달합니다 — 시스템이 스스로에게 질문을 거듭하여 명확성에 도달한 것입니다.

### Ralph: 멈추지 않는 순환

`ooo ralph`는 수렴에 도달할 때까지 세션 경계를 넘어 진화 루프를 지속적으로 실행합니다. 각 단계는 **무상태(stateless)** 방식입니다: EventStore가 전체 계보를 재구성하므로, 머신이 재시작되더라도 뱀은 멈춘 지점에서 다시 이어갑니다.

```text
Ralph Cycle 1: evolve_step(lineage, seed) → Gen 1 → action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       → Gen 2 → action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       → Gen 3 → action=CONVERGED ✓
                                                └── Ralph 종료.
                                                    존재론이 안정화됨.
```

### 모호성 점수: 경이와 코드 사이의 관문

Interview는 준비가 됐다고 느낄 때가 아니라, **수학적으로** 준비가 됐을 때 끝납니다. Ouroboros는 모호성을 가중 명확도의 보수(complement)로 정량화합니다:

```text
Ambiguity = 1 − Σ(clarityᵢ × weightᵢ)
```

각 차원은 LLM이 0.0~1.0으로 평가하고 (재현성을 위해 temperature 0.1), 가중치를 적용합니다:

| 차원 | Greenfield | Brownfield |
|:----------|:----------:|:----------:|
| **목표 명확도** — *목표가 구체적인가?* | 40% | 35% |
| **제약 명확도** — *제한 사항이 정의되었는가?* | 30% | 25% |
| **성공 기준** — *결과가 측정 가능한가?* | 30% | 25% |
| **컨텍스트 명확도** — *기존 코드베이스를 이해하고 있는가?* | — | 15% |

**임계값: Ambiguity ≤ 0.2** — 이 조건을 충족해야만 Seed를 생성할 수 있습니다.

```text
예시 (Greenfield):

  Goal: 0.9 × 0.4  = 0.36
  Constraint: 0.8 × 0.3  = 0.24
  Success: 0.7 × 0.3  = 0.21
                        ──────
  Clarity             = 0.81
  Ambiguity = 1 − 0.81 = 0.19  ≤ 0.2 → ✓ Seed 생성 가능
```

왜 0.2인가? 가중 명확도 80%에서 남은 미지수는 충분히 작아서 코드 수준의 결정으로 해소할 수 있기 때문입니다. 그 이상의 모호성에서는 아키텍처를 추측하고 있는 것입니다.

### 존재론 수렴: 뱀이 멈추는 시점

진화 루프는 영원히 돌지 않습니다. 연속된 세대가 존재론적으로 동일한 스키마를 생성하면 멈춥니다. 유사도는 스키마 필드의 가중 비교로 측정됩니다:

```text
Similarity = 0.5 × name_overlap + 0.3 × type_match + 0.2 × exact_match
```

| 구성 요소 | 가중치 | 측정 대상 |
|:----------|:------:|:-----------------|
| **Name overlap** | 50% | 두 세대에 같은 필드명이 존재하는가? |
| **Type match** | 30% | 공유 필드의 타입이 동일한가? |
| **Exact match** | 20% | 이름, 타입, 설명이 모두 동일한가? |

**임계값: Similarity ≥ 0.95** — 이 조건에서 루프가 수렴하고 진화를 멈춥니다.

하지만 단순 유사도만이 유일한 신호는 아닙니다. 시스템은 병리적 패턴도 감지합니다:

| 신호 | 조건 | 의미 |
|:-------|:----------|:--------------|
| **정체(Stagnation)** | 3세대 연속 유사도 ≥ 0.95 | 존재론이 안정화됨 |
| **진동(Oscillation)** | Gen N ≈ Gen N-2 (주기 2 순환) | 두 설계 사이에서 왕복 중 |
| **반복 피드백** | 3세대에 걸쳐 질문 중복률 ≥ 70% | 경이가 같은 질문을 반복 중 |
| **Hard cap** | 30세대 도달 | 안전장치 |

```text
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     → similarity 0.78 → CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     → similarity 1.00 → CONVERGED ✓
```

두 개의 수학적 관문, 하나의 철학: **명확해질 때까지 만들지 말고 (Ambiguity ≤ 0.2), 안정될 때까지 진화를 멈추지 마라 (Similarity ≥ 0.95).**

---

## 명령어

> 모든 `ooo` 명령어는 터미널이 아닌 Claude Code 세션 안에서 실행됩니다.
> 설치 후 `ooo setup`을 실행하여 MCP server를 등록(1회)하고, 선택적으로 프로젝트의 CLAUDE.md와 통합할 수 있습니다.

| 명령어 | 기능 |
|:--------|:-------------|
| `ooo setup` | MCP server 등록 (1회) |
| `ooo interview` | 소크라테스식 질문 → 숨겨진 가정 드러내기 |
| `ooo seed` | 불변 명세로 결정화 |
| `ooo run` | Double Diamond 분해를 통한 실행 |
| `ooo evaluate` | 3단계 검증 게이트 |
| `ooo evolve` | 존재론 수렴까지 진화 루프 |
| `ooo unstuck` | 막혔을 때 5가지 수평적 사고 페르소나 |
| `ooo status` | 드리프트 감지 + 세션 추적 |
| `ooo ralph` | 검증 완료까지 지속 루프 |
| `ooo tutorial` | 대화형 실습 튜토리얼 |
| `ooo help` | 전체 레퍼런스 |

---

## 아홉 개의 사고

아홉 개의 에이전트, 각각 다른 사고 양식. 필요할 때만 로드되며 미리 로드되지 않습니다:

| 에이전트 | 역할 | 핵심 질문 |
|:------|:-----|:--------------|
| **Socratic Interviewer** | 질문만 한다. 절대 만들지 않는다. | *"당신은 무엇을 가정하고 있는가?"* |
| **Ontologist** | 증상이 아닌 본질을 찾는다 | *"이것은 정확히 무엇인가?"* |
| **Seed Architect** | 대화에서 명세를 결정화한다 | *"이것은 완전하고 모호하지 않은가?"* |
| **Evaluator** | 3단계 검증 | *"올바른 것을 만들었는가?"* |
| **Contrarian** | 모든 가정에 도전한다 | *"정반대가 사실이라면 어떨까?"* |
| **Hacker** | 틀에 얽매이지 않는 경로를 찾는다 | *"실제로 존재하는 제약은 무엇인가?"* |
| **Simplifier** | 복잡성을 제거한다 | *"동작 가능한 가장 단순한 것은?"* |
| **Researcher** | 코딩을 멈추고 조사를 시작한다 | *"실제로 가진 근거는 무엇인가?"* |
| **Architect** | 구조적 원인을 파악한다 | *"처음부터 다시 만든다면 이렇게 만들겠는가?"* |

---

## 내부 구조

<details>
<summary><strong>18개 패키지 · 166개 모듈 · 95개 테스트 파일 · Python 3.14+</strong></summary>

```text
src/ouroboros/
├── bigbang/        Interview, 모호성 점수 산정, brownfield 탐색
├── routing/        PAL Router — 3단계 비용 최적화 (1x / 10x / 30x)
├── execution/      Double Diamond, 계층적 AC 분해
├── evaluation/     Mechanical → Semantic → Multi-Model Consensus
├── evolution/      Wonder / Reflect 순환, 수렴 감지
├── resilience/     4가지 정체 패턴 감지, 5가지 수평적 사고 페르소나
├── observability/  3요소 드리프트 측정, 자동 회고
├── persistence/    Event Sourcing (SQLAlchemy + aiosqlite), 체크포인트
├── orchestrator/   Claude Agent SDK 통합, 세션 관리
├── core/           타입, 에러, Seed, 존재론, 보안
├── providers/      LiteLLM 어댑터 (100+ 모델)
├── mcp/            Claude Code용 MCP 클라이언트/서버
├── plugin/         Claude Code 플러그인 시스템
├── tui/            터미널 UI 대시보드
└── cli/            Typer 기반 CLI
```

**핵심 내부 구조:**
- **PAL Router** — Frugal (1x) → Standard (10x) → Frontier (30x), 실패 시 자동 상향, 성공 시 자동 하향
- **Drift** — Goal (50%) + Constraint (30%) + Ontology (20%) 가중 측정, 임계값 ≤ 0.3
- **Brownfield** — 12개 이상의 언어 생태계에서 15종의 설정 파일 스캔
- **Evolution** — 최대 30세대, 존재론 유사도 ≥ 0.95에서 수렴
- **Stagnation** — 공전, 진동, 무드리프트, 수확 체감 패턴 감지

</details>

---

## 실시간 모니터링 (TUI)

Ouroboros는 실시간 워크플로우 모니터링을 위한 **터미널 대시보드**를 포함합니다. `ooo run`이나 `ooo evolve` 실행 중에 별도 터미널에서 구동하세요:

```bash
# 설치 및 실행
uvx --from ouroboros-ai ouroboros tui monitor

# 로컬 설치된 경우
uv run ouroboros tui monitor
```

| 키 | 화면 | 표시 내용 |
|:---:|:-------|:-------------|
| `1` | **Dashboard** | 단계 진행률, 수용 기준 트리, 실시간 상태 |
| `2` | **Execution** | 타임라인, 단계별 출력, 상세 이벤트 |
| `3` | **Logs** | 레벨별 색상 구분, 필터링 가능한 로그 뷰어 |
| `4` | **Debug** | 상태 인스펙터, 원시 이벤트, 설정 |

> 자세한 내용은 [TUI 사용 가이드](./docs/guides/tui-usage.md)를 참고하세요.

---

## 기여하기

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

[이슈](https://github.com/Q00/ouroboros/issues) · [토론](https://github.com/Q00/ouroboros/discussions)

---

## Star 히스토리

<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-light-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=light" alt="Star History Chart" width="100%" />
</a>
<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-dark-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=dark" alt="Star History Chart" width="100%" />
</a>

---

<p align="center">
  <em>"시작이 곧 끝이고, 끝이 곧 시작이다."</em>
  <br/><br/>
  <strong>뱀은 반복하지 않는다 — 진화한다.</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
