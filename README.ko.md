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
  <sub>AI가 코드를 쓰기 전에, 막연한 아이디어를 검증된 명세로 바꿔주는 명세 우선 워크플로우 엔진.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#빠른-시작">빠른 시작</a> ·
  <a href="#wonder에서-온톨로지로">철학</a> ·
  <a href="#순환-구조">원리</a> ·
  <a href="#명령어">명령어</a> ·
  <a href="#아홉-개의-사고">에이전트</a>
</p>

---

> **New: PM 모드** — `ooo pm`으로 소크라테스식 PM 인터뷰를 통해 PRD를 자동 생성할 수 있습니다.
> 이해관계자 정렬, 유저 스토리 매핑, 구조화된 PRD 생성까지 — 같은 소크라테스 엔진이 제품 관리 워크플로우도 지원합니다.
>
> ```
> > ooo pm "알림 시스템을 만들고 싶어"
> ```

---

> *AI는 무엇이든 만들 수 있다. 어려운 건 무엇을 만들어야 하는지 아는 것이다.*

Ouroboros는 **명세 우선 AI 개발 시스템**입니다. 이 시스템은 소크라테스식 질문법과 온톨로지 분석을 적용하여, 단 한 줄의 코드도 작성하기 전에 사용자의 숨겨진 가정을 드러냅니다.

대부분의 AI 코딩은 **출력**이 아니라 **입력** 단계에서 실패합니다. 병목 현상의 원인은 AI의 능력이 아니라, 우리가 뭘 만들지 덜 정한 채 시작하기 때문입니다. Ouroboros는 기계가 아닌 인간을 바로잡습니다.

---

## Wonder에서 온톨로지로

> *Wonder → "어떻게 살아야 하는가?" → "'삶'이란 무엇인가?" → 온톨로지*
> — 소크라테스

이게 바로 Ouroboros의 철학적 토대입니다. 좋은 질문은 더 깊은 질문으로 이어지며, 더 깊은 질문은 언제나 **온톨로지**입니다. 즉, *"이걸 어떻게 하지?"*보다 *"이게 정확히 뭐지?"*를 먼저 묻는 겁니다.

```text
   Wonder                         온톨로지
     💡                               🔬
"내가 원하는 게 뭐지?"      →    "내가 원하는 게 정확히 뭐지?"
"Task CLI를 만들자"         →    "Task가 뭐지? Priority는 뭐지?"
"인증 버그를 고치자"        →    "이게 근본 원인일까, 아니면 증상일까?"
```

이것은 단순히 추상화를 위한 것이 아닙니다. *"Task가 뭐지?"* 라는 질문에 답할 때 — 삭제 가능한 것인가, 보관 가능한 것인가? 혼자 하는 것인가, 팀으로 하는 것인가? — 재작업의 한 유형 전체를 없앨 수 있습니다. **온톨로지 질문이야말로 가장 실용적인 질문입니다.**

Ouroboros는 이 철학을 **Double Diamond** 구조로 풀어냅니다:

```text
    ◇ Wonder         ◇ 설계
   ╱  (넓히기)       ╱  (넓히기)
  ╱    탐색         ╱    창조
 ╱                 ╱
◆ ──────────── ◆ ──────────── ◆
 ╲                 ╲
  ╲    정의         ╲    전달
   ╲  (좁히기)      ╲  (좁히기)
    ◇ 온톨로지       ◇ 평가
```

첫 번째 다이아몬드는 **소크라테스적**입니다. 질문을 넓히고, 온톨로지가 또렷해질 때까지 좁혀 갑니다. 두 번째 다이아몬드는 **실용적**입니다. 설계 옵션을 넓히고, 검증된 결과물로 좁혀 갑니다. 각 다이아몬드는 그 이전 단계가 없이는 성립할 수 없습니다. 이해하지 못한 것은 설계할 수 없기 때문입니다.

---

## 빠른 시작

**설치** — 한 줄이면 전부 자동:

```bash
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
```

**시작** — AI 코딩 에이전트를 열고 바로:

```
> ooo interview "I want to build a task management CLI"
```

> Claude Code와 Codex CLI 모두 지원합니다. 런타임 감지, MCP 서버 등록, 스킬 설치까지 자동으로 처리됩니다.

<details>
<summary><strong>다른 설치 방법</strong></summary>

**Claude Code 플러그인만** (시스템 패키지 없이):
```bash
claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros
```
Claude Code 세션 안에서 `ooo setup` 실행.

**pip / uv / pipx**:
```bash
pip install ouroboros-ai                # 기본
pip install ouroboros-ai[claude]        # + Claude Code 의존성
pip install ouroboros-ai[litellm]       # + LiteLLM 멀티 프로바이더
pip install ouroboros-ai[mcp]           # + MCP 서버/클라이언트 지원
pip install ouroboros-ai[tui]           # + Textual 터미널 UI
pip install ouroboros-ai[all]           # 전부
ouroboros setup                         # 런타임 설정
```

런타임별 가이드: [Claude Code](./docs/runtime-guides/claude-code.md) · [Codex CLI](./docs/runtime-guides/codex.md)

</details>

<details>
<summary><strong>완전 삭제</strong></summary>

```bash
ouroboros uninstall
```

모든 설정, MCP 등록, 데이터를 제거합니다. 자세한 내용은 [UNINSTALL.md](./UNINSTALL.md)를 참고하세요.

</details>

<details>
<summary><strong>무슨 일이 일어났나요?</strong></summary>

```text
ooo interview  →  소크라테스식 질문으로 숨겨진 가정 12개를 드러냄
ooo seed       →  답변을 확정된 스펙으로 정리 (Ambiguity: 0.15)
ooo run        →  Double Diamond로 실행
ooo evaluate   →  3단계 검증: Mechanical → Semantic → Consensus
```

뱀이 한 바퀴를 돌고 나면 다음 바퀴는 다릅니다. 전보다 더 많이 알게 되니까요.

</details>

---

## 순환 구조

우로보로스(자기 꼬리를 삼키는 뱀)는 그냥 상징이 아닙니다. 우로보로스는 아키텍처 그 자체입니다:

```text
    Interview → Seed → Execute → Evaluate
        ↑                           ↓
        └──── Evolutionary Loop ────┘
```

각 순환은 같은 걸 반복하는 게 아닙니다. 평가 결과가 다음 세대 입력으로 돌아가고, 시스템이 지금 뭘 만드는지 분명해질 때까지 계속 **진화**합니다.

| 단계 | 수행 내용 |
|:------|:-------------|
| **Interview** | 소크라테스식 질문으로 숨겨진 가정 드러내기 |
| **Seed** | 답변을 확정된 스펙으로 정리 |
| **Execute** | Double Diamond: 발견 → 정의 → 설계 → 전달 |
| **Evaluate** | 3단계 게이트: Mechanical ($0) → Semantic → Multi-Model Consensus |
| **Evolve** | Wonder *("우리가 아직 모르는 게 뭐지?")* → 성찰 → 다음 세대 |

> *"여기서 우로보로스가 자기 꼬리를 삼킵니다: 평가의 출력이*
> *다음 세대 Seed 스펙의 입력이 됩니다."*
> — `reflect.py`

온톨로지 유사도 0.95를 넘기면 거기서 수렴합니다. 질문을 더 돌려도 크게 달라지지 않는다는 뜻입니다.

### Ralph: 멈추지 않는 순환

`ooo ralph`는 수렴에 도달할 때까지 세션 경계를 넘어 지속적으로 진화 루프를 돌립니다. 각 단계는 **무상태(stateless)**로 움직입니다. EventStore가 전체 계보를 다시 만들 수 있어서, 머신이 재시작돼도 뱀은 중단된 지점에서 이어집니다.

```text
Ralph Cycle 1: evolve_step(lineage, seed) → Gen 1 → action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       → Gen 2 → action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       → Gen 3 → action=CONVERGED ✓
                                                └── Ralph 종료.
                                                    온톨로지가 안정됨.
```

### 모호성 점수: Wonder와 코드 사이의 관문

Interview는 느낌으로 끝내지 않습니다. **수학적 계산** 점수가 기준 밑으로 내려와야 끝납니다. Ouroboros는 모호성을 `1 - 가중 명확도`로 계산합니다:

```text
Ambiguity = 1 − Σ(clarityᵢ × weightᵢ)
```

각 차원은 LLM이 0.0~1.0 사이 점수를 매기고 (재현성을 위해 temperature 0.1), 여기에 가중치를 곱합니다:

| 차원 | Greenfield | Brownfield |
|:----------|:----------:|:----------:|
| **목표 명확도** — *목표가 구체적인가?* | 40% | 35% |
| **제약 명확도** — *제한 사항이 정의되었는가?* | 30% | 25% |
| **성공 기준** — *결과가 측정 가능한가?* | 30% | 25% |
| **컨텍스트 명확도** — *기존 코드베이스를 이해하고 있는가?* | — | 15% |

**임계값: Ambiguity ≤ 0.2** — 이 아래로 내려와야 Seed를 만들 수 있습니다.

```text
예시 (Greenfield):

  Goal: 0.9 × 0.4  = 0.36
  Constraint: 0.8 × 0.3  = 0.24
  Success: 0.7 × 0.3  = 0.21
                        ──────
  Clarity             = 0.81
  Ambiguity = 1 − 0.81 = 0.19  ≤ 0.2 → ✓ Seed 생성 가능
```

왜 0.2일까요? 가중 명확도가 80%면 남은 불확실성이 작아서 코드 수준의 판단으로도 충분히 풀 수 있기 때문입니다. 그보다 모호하면 아직 아키텍처를 감으로 정하는 단계에 가깝습니다.

### 온톨로지 수렴: 뱀이 멈추는 시점

진화 루프는 끝없이 돌지 않습니다. 연속된 세대가 온톨로지적으로 같은 스키마를 만들면 거기서 멈춥니다. 유사도는 스키마 필드를 가중 비교해서 계산합니다:

```text
Similarity = 0.5 × name_overlap + 0.3 × type_match + 0.2 × exact_match
```

| 구성 요소 | 가중치 | 측정 대상 |
|:----------|:------:|:-----------------|
| **Name overlap** | 50% | 두 세대에 같은 필드명이 존재하는가? |
| **Type match** | 30% | 공유 필드의 타입이 동일한가? |
| **Exact match** | 20% | 이름, 타입, 설명이 모두 동일한가? |

**임계값: Similarity ≥ 0.95** — 이 선을 넘으면 루프가 수렴하고 멈춥니다.

하지만 유사도만 보는 건 아닙니다. 시스템은 병리적인 패턴도 함께 봅니다:

| 신호 | 조건 | 의미 |
|:-------|:----------|:--------------|
| **정체(Stagnation)** | 3세대 연속 유사도 ≥ 0.95 | 온톨로지가 안정됨 |
| **진동(Oscillation)** | Gen N ≈ Gen N-2 (주기 2 순환) | 두 설계 사이에서 왕복 중 |
| **반복 피드백** | 3세대에 걸쳐 질문 중복률 ≥ 70% | Wonder가 같은 질문만 반복 중 |
| **Hard cap** | 30세대 도달 | 안전장치 |

```text
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     → similarity 0.78 → CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     → similarity 1.00 → CONVERGED ✓
```

기준은 두 개입니다. **충분히 분명해질 때까지는 만들지 않고 (Ambiguity ≤ 0.2), 안정될 때까지는 진화를 계속합니다 (Similarity ≥ 0.95).**

---

## 명령어

> 모든 `ooo` 명령어는 AI 코딩 에이전트(Claude Code, Codex CLI 등) 세션 안에서 실행됩니다.
> 설치 후 `ooo setup`을 실행하여 MCP 서버를 등록(1회)하고, 프로젝트 설정과 통합할 수 있습니다.

| 명령어 | 기능 |
|:--------|:-------------|
| `ooo setup` | MCP 서버 등록 (1회) |
| `ooo interview` | 소크라테스식 질문 → 숨겨진 가정 드러내기 |
| `ooo seed` | 확정된 스펙으로 정리 |
| `ooo run` | Double Diamond로 실행 |
| `ooo evaluate` | 3단계 검증 게이트 |
| `ooo evolve` | 온톨로지 수렴까지 진화 루프 |
| `ooo unstuck` | 막혔을 때 활용 가능한 5가지 수평적 사고 페르소나 |
| `ooo status` | 드리프트 감지 + 세션 추적 |
| `ooo ralph` | 검증될 때까지 계속 도는 루프 |
| `ooo tutorial` | 대화형 실습 |
| `ooo help` | 전체 참조 |
| `ooo pm` | PM 인터뷰 + PRD 생성 |
| `ooo qa` | 범용 QA 판정 |
| `ooo cancel` | 멈춘 실행 취소 |
| `ooo update` | 최신 버전 확인 + 업그레이드 |
| `ooo brownfield` | 기존 저장소 스캔 + 기본값 관리 |

---

## 아홉 개의 사고

아홉 개의 에이전트가 있고, 각자 생각하는 방식이 다릅니다. 필요할 때만 로드하고, 처음부터 다 띄워두지는 않습니다:

| 에이전트 | 역할 | 핵심 질문 |
|:------|:-----|:--------------|
| **Socratic Interviewer** | 질문만 한다. 절대 만들지 않는다. | *"지금 뭘 가정하고 있지?"* |
| **Ontologist** | 증상이 아닌 본질을 찾는다 | *"이게 정확히 뭐지?"* |
| **Seed Architect** | 대화를 통해 스펙을 구체화한다 | *"모호함이 사라졌나?"* |
| **Evaluator** | 3단계로 검증 | *"우리가 맞는 걸 만든 건가?"* |
| **Contrarian** | 모든 가정에 의문을 제기한다 | *"반대 상황이 사실이라면?"* |
| **Hacker** | 색다른 경로를 찾는다 | *"진짜 제약이 뭐지?"* |
| **Simplifier** | 복잡성을 제거한다 | *"돌아가는 것 중 제일 단순한 건?"* |
| **Researcher** | 코딩을 멈추고 조사를 시작한다 | *"근거 있어?"* |
| **Architect** | 구조적 원인을 파악한다 | *"처음부터 다시 짜면 정말 이렇게 갈까?"* |

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
├── resilience/     4가지 정체 패턴 감지, 5가지 측면 페르소나
├── observability/  3요소 드리프트 측정, 자동 회고
├── persistence/    Event Sourcing (SQLAlchemy + aiosqlite), 체크포인트
├── orchestrator/   런타임 추상화 레이어 (Claude Code, Codex CLI)
├── core/           타입, 에러, Seed, 온톨로지, 보안
├── providers/      LiteLLM 어댑터 (100+ 모델)
├── mcp/            MCP 클라이언트/서버
├── plugin/         플러그인 시스템 (스킬/에이전트 자동 탐색)
├── tui/            터미널 UI 대시보드
└── cli/            Typer 기반 CLI
```

**핵심 내부 구조:**
- **PAL Router** — Frugal (1x) → Standard (10x) → Frontier (30x), 실패 시 자동 상향, 성공 시 자동 하향
- **Drift** — Goal (50%) + Constraint (30%) + 온톨로지 (20%) 가중 측정, 임계값 ≤ 0.3
- **Brownfield** — 12개 이상의 언어 생태계에서 15종의 설정 파일 스캔
- **Evolution** — 최대 30세대, 온톨로지 유사도 ≥ 0.95에서 수렴
- **Stagnation** — 스핀, 오실레이션, 드리프트 부재, 수익 감소 패턴 감지

</details>

---

## 실시간 모니터링 (TUI)

Ouroboros에는 실시간 워크플로우를 볼 수 있는 **터미널 대시보드**가 있습니다. `ooo run`이나 `ooo evolve`를 돌릴 때 별도 터미널에서 같이 띄우면 됩니다:

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
