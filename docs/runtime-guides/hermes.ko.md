# Hermes Agent에서 Ouroboros 실행하기

[Hermes Agent](https://github.com/NousResearch/hermes-agent)를 실행 런타임으로 Ouroboros를 사용하는 방법입니다.

> English: [hermes.md](./hermes.md)
>
> 영문 원문 전체를 옮긴 완역본입니다. 원문이 갱신되면 이 문서도 함께 갱신해야 합니다.

## 설치

Hermes CLI 0.8.0 이상이 설치돼 있어야 합니다.

```bash
# 설치 확인
hermes version
```

## Setup

패키지를 격리해 실행하는 런처로 MCP 프로필을 설치한 뒤 `hermes` 런타임을 선택합니다.

```bash
pipx install 'ouroboros-ai[mcp]'         # 또는: uv tool install 'ouroboros-ai[mcp]'
ouroboros setup --runtime hermes
```

setup에는 `uvx` 또는 `pipx`가 필요합니다. setup은 이 런처로 격리된 프로세스를 띄워 MCP 2 서버를 등록합니다. 호스트 환경에는 Claude SDK의 MCP 1.x 의존성이 섞여 있거나 MCP extra가 없을 수 있습니다. 그래서 `ouroboros` 바이너리나 `python -m`을 직접 실행하는 방식으로는 절대 폴백하지 않습니다. 런처가 둘 다 없으면 저장된 Ouroboros 런타임 설정을 바꾸지 않고 0이 아닌 코드로 종료합니다.

setup은 다음 세 가지를 처리합니다.

1. `~/.ouroboros/config.yaml`에서 `hermes` 백엔드를 쓰도록 설정합니다.
2. `~/.hermes/skills/autonomous-ai-agents/ouroboros/`에 Ouroboros 스킬을 설치합니다.
3. `~/.hermes/config.yaml`에 Ouroboros MCP 서버를 등록합니다.

`uvx`를 쓰면 다음 호스트 항목이 생깁니다.

```yaml
mcp_servers:
  ouroboros:
    command: uvx
    args: [--isolated, --python, ">=3.12", --from, "ouroboros-ai[mcp]", ouroboros, mcp, serve]
    enabled: true
```

`pipx`만 있으면 다음과 같이 등록합니다.

```yaml
mcp_servers:
  ouroboros:
    command: pipx
    args: [run, --spec, "ouroboros-ai[mcp]", ouroboros, mcp, serve]
    enabled: true
```

## 사용

설정이 끝나면 Ouroboros는 Hermes를 오케스트레이터 런타임 백엔드로 씁니다. 이때 `llm.backend`는 바뀌지 않습니다. 인터뷰와 모호도 채점처럼 LLM만 쓰는 흐름은 사용자가 설정한 LLM 어댑터를 계속 씁니다.

### 워크플로 실행

```bash
ouroboros run seed.yaml --runtime hermes
```

### Hermes로 스크립팅

Hermes 세션에서는 명령 앞에 `ooo`를 붙여 Ouroboros 스킬을 실행할 수 있습니다.

```bash
hermes chat -q "ooo interview 'Build a new CLI tool'"
hermes chat -q "ooo run seed.yaml"
```

Hermes는 공통 무상태 `ouroboros.router` 리졸버로 `ooo`와 `/ouroboros:` 명령을 정확히 디스패치합니다. 명령을 추가하거나 바꿀 때는 관련 `SKILL.md`의 frontmatter만 고치면 됩니다. 로깅과 메시지 조립, MCP 호출은 런타임 안에서 처리합니다. [공통 `ooo` 스킬 디스패치 라우터](../guides/ooo-skill-dispatch-router.md)(영문)를 참고하세요.

## 설정

`~/.ouroboros/config.yaml`에서 Hermes CLI 경로를 바꿀 수 있습니다.

```yaml
orchestrator:
  runtime_backend: hermes
  hermes_cli_path: ~/.local/bin/hermes
```

## 기술 세부사항

### 세션 관리

Ouroboros는 Hermes CLI가 quiet 모드(`-Q`)에서 내보내는 `session_id`로 Hermes 세션을 추적합니다. 이 ID와 `--resume` 플래그로 대화를 재개합니다.

### 권한 모드

새 Hermes 턴을 시작할 때나 기존 턴을 재개할 때나 Seed 실행은 `bypassPermissions`를 강제합니다. 런타임은 이 계약을 Hermes의 네이티브 `--yolo --accept-hooks` 플래그로 바꿉니다. 이 플래그를 쓰면 위험한 명령 승인과 처음 보는 셸 훅 승인이 헤드리스 실행을 막지 못합니다.

### 출력 파싱

Ouroboros는 Hermes CLI 출력을 파싱해 최종 응답과 세션 메타데이터를 추출합니다. 프로그래밍 방식으로 실행할 때는 추론 블록과 배너를 자동으로 제거합니다.

## Active Conductor와 Synapse

설치된 Hermes를 실제로 점검했을 때 세션 마커는 나왔지만 Hermes 세션 저장소에서 그 세션을 다시 열지는 못했습니다. Ouroboros는 이 결과에 따라 Hermes가 Synapse 전달을 지원한다고 표시하지 않습니다. CLI 플래그만으로 세션 연속성이 입증됐다고 보지 않으며 `inform`, `after_turn`, 체크포인트 `redirect`, 하드 `replace` 요청은 모두 거부합니다. 일반 Hermes 실행과 관찰자 진행 보고는 그대로 동작합니다. 메인 호스트는 지원하지 않는 전달 방식이라는 점을 사용자의 대화 언어로 분명히 설명합니다.
