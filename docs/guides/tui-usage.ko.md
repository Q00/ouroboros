# TUI 대시보드 레퍼런스

> English: [tui-usage.md](./tui-usage.md)

Ouroboros에는 워크플로우를 실시간으로 지켜보는 두 가지 터미널 UI(TUI)
백엔드가 있습니다. 기본값은 [Textual](https://textual.textualize.io/)로 만든
Python UI이고, 다른 하나는 [SuperLightTUI](https://github.com/subinium/SuperLightTUI)로
만든 네이티브 Rust UI(`slt`)입니다. 두 백엔드는 데이터 모델의 일부를 공유하지만
화면과 키 바인딩은 서로 같지 않습니다.

> **처음이신가요?** 설치와 시작은 [Getting Started](../getting-started.md)(영문)를 보세요.

## 실행

```bash
# 기본 Python Textual 백엔드
ouroboros tui monitor

# 특정 데이터베이스 파일을 지켜보려면
ouroboros tui monitor --db-path ~/.ouroboros/ouroboros.db

# 네이티브 Rust SLT 백엔드(ouroboros-tui 바이너리 필요)
ouroboros tui monitor --backend slt

# 네이티브 백엔드가 직접 소유하는 데모 시뮬레이션
ouroboros-tui --mock
```

기본 Textual 백엔드는 **세션 선택** 화면에서 기존 세션을 고른 뒤 대시보드로
넘어갑니다. SLT는 가장 최근 세션을 대시보드에 불러오고 세션 목록을 4번 화면에
둡니다.

## 화면 구성

<!-- tui-contract:textual-screens -->
### Textual 화면(기본 백엔드)

Textual에는 숫자로 고르는 화면 4개와 별도의 세션 선택·계보 화면이 있습니다.

| 키 | 단축키 | 화면 | 용도 |
|-----|----------|--------|---------|
| `1` | | **대시보드** | 기본 화면. 단계 진행, AC 트리, 노드 상세 |
| `2` | | **실행** | 실행 타임라인, 단계별 출력, 상세 이벤트 |
| `3` | `l` | **로그** | 레벨별 색이 붙는 필터 가능한 로그 뷰어 |
| `4` | `d` | **디버그** | 상태 검사기, 원본 이벤트, 설정 덤프 |
| | `s` | **세션 선택** | 세션 전환 |
| | `e` | **계보** | 세대별 진화 계보 보기 |

<!-- tui-contract:slt-screens -->
### SLT 화면(네이티브 백엔드)

SLT의 화면 4개는 구성이 다릅니다. 별도 로그·디버그 화면은 없으며, `l`은 실행
화면 안의 로그 패널을 열고 모달이 키를 소유하지 않을 때 `Esc`는 패널을 닫습니다.
명령 팔레트가 열려 있으면 `Esc`는 팔레트만 닫고 아래의 로그 패널과 필터는
그대로 보존합니다. 필터에 포커스가 있을 때 `l`은 패널을 닫지 않고 필터 문자로
입력됩니다. 패널이 열려 있어도 전역 단축키 `q`, `1`-`4`, `Ctrl+P`는 계속 예약됩니다.

| 키 | 단축키 | 화면 | 용도 |
|-----|----------|--------|---------|
| `1` | | **대시보드** | 단계 진행, AC 트리, 노드 상세 |
| `2` | | **실행** | 단계별 출력, 이벤트 타임라인, 선택형 로그 패널 |
| `3` | `e` | **계보** | 세대별 진화 이력 |
| `4` | `s` | **세션** | 세션 탐색과 불러오기 |
| | `l` | **실행 로그 패널** | 실행 화면에 있을 때만 열기 |
| | `Esc` | **실행 로그 패널** | 활성 모달이 없을 때 열린 패널 닫기 |

## Textual 대시보드 (키: 1)

세 부분으로 나뉩니다.

```
+---------------------------------------------------------------------+
|  < Discover  ->  * Define  ->  < Design  ->  > Deliver              |
+----------------------------------+----------------------------------+
|                                  |                                  |
|  AC EXECUTION TREE               |  NODE DETAIL                     |
|  +- root                         |                                  |
|    +- ◐ AC1 (executing)          |  AC: AC1                         |
|    | +- ● SubAC1 (complete)      |  Status: Executing               |
|    | +- ○ SubAC2 (pending)       |  Depth: 1                        |
|    +- ○ AC2 (pending)            |                                  |
|    +- ● AC3 (complete)           |  Content:                        |
|                                  |  Create a User model with...     |
|                                  |                                  |
+----------------------------------+----------------------------------+
```

### Double Diamond 단계 바

네 단계 중 지금 어디인지 보여줍니다.

- **Discover** — 문제 공간을 넓히는 단계
- **Define** — 핵심 문제로 좁히는 단계
- **Design** — 해법을 넓히는 단계
- **Deliver** — 구현으로 좁히는 단계

현재 단계가 강조됩니다. 워크플로우가 진행되면 단계도 자동으로 넘어갑니다.

### AC 실행 트리

모든 검수 기준(AC)과 하위 AC를 계층으로 보여줍니다.

| 아이콘 | 상태 |
|------|--------|
| `○` (흐림) | 대기 — 아직 시작 안 함 |
| `⊘` (빨강) | 차단 — 선행 항목을 기다리는 중 |
| `◐` (노랑) | 실행 중 |
| `●` (초록) | 완료 — 평가 통과 |
| `✖` (빨강) | 실패 — 통과 못 함 |
| `◆` (파랑) | 원자 — 더 쪼개지지 않는 잎 노드 |
| `◇` (청록) | 분해됨 — 하위 AC를 가짐 |

**이동**: 화살표 키로 트리를 오갑니다. Enter 또는 클릭으로 노드를 고르면 오른쪽 패널에 상세가 뜹니다. `t`를 누르면 트리 위젯에 포커스가 갑니다.

### 노드 상세 패널

트리에서 AC나 하위 AC를 고르면 이 패널에 나옵니다.

- **ID**: 노드 식별자
- **Status**: 현재 실행 상태
- **Depth**: 트리 깊이 (0 = 루트, 1 = 최상위 AC, 2 이상 = 하위 AC)
- **Content**: 검수 기준 전문

## Textual 로그 화면 (키: 3 또는 `l`)

필터와 스크롤이 되는 로그 뷰어이고, 심각도에 따라 색이 다릅니다.

| 레벨 | 색 |
|-------|-------|
| DEBUG | 흐린 회색 |
| INFO | 흰색 |
| WARNING | 노랑 |
| ERROR | 빨강 |
| CRITICAL | 굵은 빨강 |

워크플로우가 도는 동안 실시간으로 갱신됩니다.

## Textual 실행 화면 (키: 2)

- **타임라인**: 실행 이벤트를 시간순으로
- **단계별 출력**: 각 단계의 결과
- **툴 호출**: 에이전트가 어떤 툴을 썼고 결과가 무엇이었는지

## Textual 디버그 화면 (키: 4 또는 `d`)

문제를 파고들 때 씁니다.

- **상태 검사기**: 현재 `TUIState` 값(단계, drift, 비용, AC 트리)
- **원본 이벤트**: EventStore에서 가공되지 않은 이벤트
- **설정**: 지금 적용된 파이프라인·실행 설정

## Textual 세션 선택 (키: `s`)

쓸 수 있는 세션을 훑고 고릅니다. 여러 워크플로우를 돌려놓고 오갈 때 씁니다.

## Textual 계보 화면 (키: `e`)

진화 루프(`ooo evolve`)를 쓸 때 세대별 계보를 봅니다. seed가 여러 번 반복하며 어떻게 바뀌고 수렴했는지 보여줍니다.

## 키보드 단축키

<!-- tui-contract:textual-keys -->
### Textual 키 우선순위

| 키 | 동작 |
|-----|--------|
| `1` ~ `4` | Textual 1~4번 화면으로 전환 |
| `s` | 세션 선택 |
| `e` | 계보 보기 |
| `q` | TUI 종료 |
| `p` | 실행 소유자가 연결됐을 때 일시정지 요청 |
| 대시보드 또는 세션 선택 화면의 `r` | 실행 소유자가 연결됐을 때 재개 요청 |
| 실행 화면의 `r` | 실행 화면 새로고침. 재개하지 않음 |
| 디버그 화면의 `r` | 디버그 화면 새로고침. 재개하지 않음 |
| 로그 화면의 `r` | 활성 바인딩 없음. 재개하지 않음 |
| 계보 선택 화면의 `r` | 계보 목록 새로고침. 재개하지 않음 |
| 계보 상세 화면의 `r` | rewind 확인 절차 열기. 재개하지 않음 |

> **주의**: `ouroboros tui monitor`는 이벤트 저장소에 **관찰자로** 붙으므로
> 돌고 있는 실행을 소유하지 않습니다. 따라서 생명주기 제어용 `p`와 `r`은
> 푸터에서 숨겨지고 눌러도 아무 일도 없습니다. 실행을 멈추려면
> `ouroboros cancel execution`을 쓰세요.
>
> Textual에서는 임베딩하는 쪽이 `OuroborosTUI.set_pause_callback()` /
> `set_resume_callback()`으로 실행 소유자를 연결했을 때만 생명주기 키가
> 나타납니다. 이때도 위 표에 나온 화면별 `r` 바인딩이 앱 수준의 재개
> 바인딩보다 우선합니다. 대시보드와 세션 선택 화면은 재개를 제공합니다.
>
> 연결돼 있어도 **화면에 보이는 생명주기 상태는 실행 제어 경로가 확인된
> 이벤트를 저장한 뒤에야** 바뀝니다. 일시정지는
> `orchestrator.session.paused`, 재개는 `runtime_status: running`을 실은
> progress로 확인합니다. 요청이 불가능하거나 실패하면 경고/에러로 보고되고
> 상태는 그대로입니다.

<!-- tui-contract:slt-lifecycle -->
### SLT 키와 데모 소유권

| 키 | 동작 |
|-----|--------|
| `1` ~ `4` | SLT 1~4번 화면으로 전환 |
| `e` | 계보 화면(`3`) |
| `s` | 세션 화면(`4`) |
| `l` | 실행 화면(`2`)에서 로그 패널 열기 |
| `Esc` | 명령 팔레트가 열려 있으면 팔레트 닫기, 세션 화면에서는 대시보드로 돌아가기, 실행 화면에서는 열린 로그 패널 닫기 |
| `Ctrl+P` | 명령 팔레트 열기 |
| `q` | TUI 종료 |
| `p` / `r` | SLT가 데모 시뮬레이션을 소유할 때만 일시정지/재개 |

SLT가 데모 시뮬레이션을 소유해 `p` / `r`을 제공하는 경우는 세 가지입니다.

1. `ouroboros-tui --mock`으로 명시적으로 실행한 경우
2. 선택한 데이터베이스는 열렸지만 이벤트가 하나도 없는 경우
3. 데이터베이스를 열 수 없어 SLT가 데모 데이터로 대체한 경우

뒤의 두 가지는 자동 fallback일 뿐 실제 실행을 제어하는 것이 아닙니다. 실제
이벤트가 든 데이터베이스에 연결하면 SLT는 관찰자가 되고, 푸터와 명령 팔레트에서
생명주기 제어가 사라집니다. 저장된 progress가 일시정지로 보이던 실행을 다시
실행 중으로 바꿀 수는 있지만, 그 투영이 관찰자에게 실행 제어권을 주지는 않습니다.

### 이동

| 키 | 동작 |
|-----|--------|
| `Up` / `Down` | 선택 이동 / 스크롤 |
| `Tab` | 다음 위젯으로 포커스 |
| `Shift+Tab` | 이전 위젯으로 포커스 |
| `Enter` | 선택 / 펼치기 |

### 대시보드 전용

| 키 | 동작 |
|-----|--------|
| `t` | AC 트리 위젯에 포커스 |
| `Up` / `Down` | AC 트리 이동 |
| `Enter` | AC 노드를 골라 상세 보기 |

## 구조 메모

Textual 백엔드는 `EventStore`를 0.5초 간격으로 폴링해 구독합니다. 이벤트는
Textual 메시지로 바뀌어 현재 화면으로 전달됩니다.

```
EventStore -> app._subscribe_to_events() (0.5초 폴링)
           -> create_message_from_event()
           -> post_message() -> 화면 핸들러
```

주요 메시지 종류:

- `PhaseChanged` — Double Diamond 단계 전환
- `ACUpdated` — AC 상태 변경
- `WorkflowProgressUpdated` — AC 트리 구조와 상태
- `ExecutionUpdated` — 세션 시작/완료/실패/일시정지
- `SubtaskUpdated` — 하위 작업 계층 갱신
- `DriftUpdated` — drift 점수 변경
- `CostUpdated` — 토큰 사용량 / 비용 갱신
- `ToolCallStarted` / `ToolCallCompleted` — 에이전트 툴 사용
- `AgentThinkingUpdated` — 에이전트 추론 출력
- `ParallelBatchStarted` / `ParallelBatchCompleted` — 병렬 실행 이벤트

SLT는 같은 SQLite 이벤트 저장소를 직접 읽어 Rust `AppState`에 반영합니다.
SLT의 탭 매핑, 생명주기 소유권, mock fallback은 Textual 메시지 파이프라인과
별도의 구현입니다.

### 런타임 계약 출처

아래 설명은 저장소에 들어 있는 현재 런타임 정의에 연결돼 있습니다.

- Textual 앱 바인딩: [`src/ouroboros/tui/app.py`](../../src/ouroboros/tui/app.py)
- Textual 화면별 재정의: [`src/ouroboros/tui/screens/`](../../src/ouroboros/tui/screens/)
- SLT 화면 매핑과 mock fallback: [`crates/ouroboros-tui/src/main.rs`](../../crates/ouroboros-tui/src/main.rs)
- SLT 생명주기 capability 상태: [`crates/ouroboros-tui/src/state.rs`](../../crates/ouroboros-tui/src/state.rs)

## 문제 해결

**아무 데이터도 안 보인다**

- 워크플로우가 돌고 있는지, 또는 실행 ID를 넘겼는지 확인하세요.
- `ouroboros config show`로 EventStore 경로를 확인하고, 그 파일이 실제로 있는지 보세요.

**AC 트리가 갱신되지 않는다**

- 0.5초마다 폴링하므로 짧은 지연은 정상입니다.
- 실행이 멈춰 있다면 그 실행을 소유한 쪽에서 재개해야 합니다. `ouroboros tui monitor`로는 재개할 수 없습니다.

**생명주기 일시정지/재개를 쓸 수 없다**

- `ouroboros tui monitor`에서는 정상입니다. 실행을 소유하지 않으므로 해당
  생명주기 바인딩이 숨겨집니다. 새로고침·rewind 같은 화면별 `r` 동작은 계속
  쓸 수 있습니다. 실행을 멈추려면 `ouroboros cancel execution`을 쓰세요.

**화면이 깨진다**

- 터미널이 256색과 유니코드를 지원하는지 확인하세요.
- 최소 크기는 80열 x 24행을 권장합니다.
- 렌더링이 계속 깨지면 다른 터미널 에뮬레이터를 써보세요.
