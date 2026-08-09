<!--
doc_metadata:
  runtime_scope: [claude]
-->

# Claude Code로 Ouroboros 실행하기

> English: [claude-code.md](./claude-code.md)

Ouroboros는 **Claude Code**를 런타임 백엔드로 쓸 수 있습니다. **Claude Code Pro 또는 Max Plan** 구독을 그대로 활용하므로 별도 API 키가 필요 없습니다.

> 설치와 첫 실행은 [Getting Started](../getting-started.md)(영문)를 보세요.

> **명령어를 어디서 치는지 구분하세요.** 이 문서에는 두 가지 맥락의 명령이 섞여 있습니다.
>
> - **터미널** — 평소 쓰는 셸(bash, zsh 등)에서 치는 명령
> - **Claude Code 세션 안** — `claude`로 세션을 연 다음, 그 안에서만 동작하는 `ooo` 스킬 명령
>
> 아래 코드 블록마다 어디서 치는지 표시했습니다.

## 시작하기 (권장 경로)

대부분의 사람은 이 길로 오면 됩니다. **Ouroboros를 pip로 설치할 필요도, API 키를 설정할 필요도 없습니다** — 런타임은 Claude Code가 맡습니다.

시작하기 전에 호스트에 두 가지가 있어야 합니다:

- **`uvx`** — 플러그인의 MCP 매니페스트가 이걸로 서버를 띄웁니다([`.claude-plugin/.mcp.json`](../../.claude-plugin/.mcp.json)).
- **`python3`** — 번들 스킬들이 셸에서 직접 호출합니다. setup 스킬이 첫 실행 설정을 기록할 때([`skills/setup/SKILL.md:98`](../../.claude-plugin/skills/setup/SKILL.md)), welcome 스킬이 준비 상태를 판별할 때([`skills/welcome/SKILL.md:68`](../../.claude-plugin/skills/welcome/SKILL.md)) 모두 `python3`을 씁니다.

```bash
pipx install uv
pip install --user uv
brew install uv          # macOS / Linuxbrew
```

> **`uvx`가 Python 요구를 대신하지 않습니다.** `uvx --python '>=3.12'`는 **격리된 MCP 프로세스에** 인터프리터를 붙여 줄 뿐, 전역 `python3` 명령을 만들어 주지 않습니다. 위 스킬 스니펫들은 셸에서 `python3`을 직접 찾으므로, `uvx`만 있고 시스템 Python이 없는 호스트는 **첫 setup/welcome 흐름에서 실패합니다.** 스킬 쪽 인터프리터 탐색을 고치는 건 [#2001](https://github.com/Q00/ouroboros/issues/2001)에서 추적합니다.

**터미널:**

```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

그다음 **Claude Code 세션 안에서:**

```
ooo setup
ooo help        # 설치 확인
```

여기까지 하면 끝입니다. 첫 워크플로우도 세션 안에서 `ooo`로 시작합니다.

```
ooo
```

전체 첫 실행 흐름(인터뷰 → seed → 실행)은 [Getting Started](../getting-started.md)(영문)를 보세요.

### 사전 조건 (권장 경로)

- Claude Code CLI 설치 및 인증 완료 (Pro 또는 Max Plan)
- **`uvx`** (uv에 포함) — 위에서 설명한 대로 플러그인 MCP 매니페스트가 이걸로 서버를 띄웁니다

아래 독립 CLI 경로의 `Python >= 3.12` 요구사항은 **이 경로에는 해당하지 않습니다.**

## 독립 CLI로 쓰기 (선택)

Claude Code 세션 밖, 평소 쓰는 셸에서 `ouroboros` 명령을 직접 치고 싶을 때만 이 경로를 씁니다.

### 사전 조건 (독립 CLI 경로)

- Claude Code CLI 설치 및 인증 완료 (Pro 또는 Max Plan)
- **Python >= 3.12**
- Ouroboros 설치 (설치 방법은 [Getting Started](../getting-started.md) 참고)

> 기본값인 in-process SDK 런타임(MCP 1.x)을 쓰려면 `ouroboros-ai[claude]`를 설치하세요. 마켓플레이스 플러그인은 격리된 `ouroboros-ai[mcp]` 환경에서 MCP 2 서버를 띄우고 `[claude-cli]` 워커를 고릅니다. **`[mcp]`를 `[claude]`·`[claude-sdk]`·`[all]`과 한 인터프리터에 같이 넣지 마세요.**

## 설정

Claude Code를 런타임 백엔드로 고르려면 Ouroboros 설정에 다음을 넣습니다:

```yaml
orchestrator:
  runtime_backend: claude  # `ouroboros setup --runtime claude`가 써 줍니다
```

`--orchestrator` CLI 플래그를 쓸 때는 Claude Code가 기본 런타임 백엔드입니다.

## 동작 방식

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |  Claude Code    |
|  (your task)    |     |   (adapter.py)   |     |  (Pro/Max Plan) |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Tools Available |
                        |  - Read          |
                        |  - Write         |
                        |  - Edit          |
                        |  - Bash          |
                        |  - Glob          |
                        |  - Grep          |
                        +------------------+
```

기본 프로필은 Agent SDK와 거기 딸린 인증된 Claude Code 전송을 씁니다. SDK는 MCP 1.x에 머물러 있습니다. 플러그인이 소유하는 MCP 2 서버는 별도 `uvx` 프로세스이고 `--runtime claude-cli`를 쓰기 때문에, 한 인터프리터가 MCP 두 메이저 버전을 동시에 로드하는 일은 없습니다. LiteLLM 합의 모델은 [`credentials.yaml`](../config-reference.md#credentialsyaml)을 보세요.

> 런타임 백엔드를 나란히 비교하려면 [runtime capability matrix](../runtime-capability-matrix.md)를 보세요.

## Claude Code를 쓸 때의 이점

- **API 키 관리가 없다** — Pro/Max Plan 구독을 그대로 씁니다.
- **툴이 풍부하다** — 파일·셸·검색 툴 전체를 Claude Code를 통해 씁니다.
- **세션이 이어진다** — 중단된 워크플로우를 `--resume`으로 재개합니다.

## CLI 옵션

이 절의 명령은 전부 **독립 CLI 경로**용입니다. 평소 쓰는 터미널에서 치며, Claude Code 세션 안이 아닙니다. 플러그인으로 설치했다면 세션 안에서 `ooo` 명령을 쓰세요.

### 인터뷰 명령

**터미널:**

```bash
# 대화형 인터뷰 시작 (Claude Code 런타임)
uv run ouroboros init start --orchestrator "만들고 싶은 것"

# 중단된 인터뷰 재개
uv run ouroboros init start --resume interview_20260127_120000

# 인터뷰 목록
uv run ouroboros init list
```

### 워크플로우 명령

**터미널:**

```bash
# 워크플로우 실행 (Claude Code 런타임)
uv run ouroboros run workflow --orchestrator seed.yaml

# 드라이런 (실행하지 않고 seed만 검증)
uv run ouroboros run workflow --dry-run seed.yaml

# 디버그 출력 (로그와 에이전트 추론 표시)
uv run ouroboros run workflow --orchestrator --debug seed.yaml

# 이전 세션 재개
uv run ouroboros run workflow --orchestrator --resume <session_id> seed.yaml
```

## 문제 해결

### 헬스체크에 "Providers: warning"이 뜬다

LiteLLM 프로바이더를 안 쓸 때 나오는 정상 메시지입니다. orchestrator 모드는 Claude Code를 직접 씁니다.

### 세션이 빈 에러와 함께 실패한다

프로젝트 디렉터리에서 실행하고 있는지 확인하세요.

**터미널:**

```bash
cd /path/to/ouroboros
uv run ouroboros run workflow --orchestrator seed.yaml
```

### "EventStore not initialized"

데이터베이스는 `ouroboros config show`가 알려주는 경로에 자동으로 생성됩니다.

## 비용

Pro 또는 Max Plan으로 Claude Code를 런타임 백엔드로 쓰면:

- **추가 API 비용이 없습니다** — 구독을 그대로 씁니다.
- 실행 시간은 작업 복잡도에 따라 다릅니다.
- 간단한 작업: 보통 15~30초
- 여러 파일을 건드리는 복잡한 작업: 1~3분

> **참고**: Pro 플랜(월 $20)로도 동작하지만 사용량 한도가 낮습니다. 오래 도는 에이전틱 워크플로우라면 세션 중간에 한도에 걸리지 않도록 **Max 플랜을 권장합니다.**

## Active Conductor와 Synapse

Claude Agent SDK와 지속된 Claude 워커 세션은 Synapse의 `inform`/`after_turn` 전송으로 검증된 경로입니다. 전달은 **현재 턴이 끝난 뒤에야** 같은 네이티브 세션을 재개합니다. 재개 가능성을 실시간 체크포인트 `redirect`처럼 내세우지 않으며, 강제 `replace`는 여전히 지원하지 않습니다.

메인 Claude 대화는 읽기 전용 관찰자를 정확히 하나만 위임하고, 사용자에게는 계속 응답할 수 있는 상태로 남습니다. 그러면서 현재 런타임과 모델, 효율성 보증, 범위가 정해진 Discover 목표, 의존성·병렬 수준, 처음 스케줄된 AC들, 주의 사항, 종료 시 보증을 전달합니다. AC는 내부 ID를 묻지 않고 의미로 고릅니다. 안내 문구의 정본은 영어이고, 호스트는 사용자가 지금 쓰고 있는 대화 언어로 자연스럽게 답합니다.
