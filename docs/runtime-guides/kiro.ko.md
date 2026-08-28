# Ouroboros 런타임 가이드: Kiro CLI

[Kiro CLI](https://kiro.dev/docs/cli/)를 실행 런타임으로 삼아 Ouroboros를 쓰는 방법입니다. Kiro는 **헤드리스 모드**로 돕니다(`kiro-cli chat --no-interactive`, 문서는 <https://kiro.dev/docs/cli/headless/>).

> English: [kiro.md](./kiro.md)
>
> 영문 원문 전체를 옮긴 완역본입니다. 원문이 갱신되면 이 문서도 함께 갱신해야 합니다.

## 설치

Ouroboros가 쓰기 전에 Kiro CLI가 설치되고 인증돼 있어야 합니다:

```bash
# 설치 확인 — --resume-id 지원에는 2.2.0 이상이 필요합니다
kiro-cli --version
kiro-cli chat --help | grep -- --resume-id
```

Kiro 배포판이 제공하는 방식(AWS Builder / IAM 로그인 등)으로 **한 번 로그인해 두세요.** 그래야 헤드리스 호출이 프롬프트 없이 동작합니다.

## Setup

```bash
pipx install 'ouroboros-ai[mcp]'         # 또는: uv tool install 'ouroboros-ai[mcp]'
ouroboros setup --runtime kiro
```

setup에는 `uvx` 또는 `pipx`가 **필요합니다.** MCP 2 서버가 호환되지 않는 호스트 Python 환경을 상속하지 못하게 하기 위해서입니다. 둘 다 없으면 setup은 `~/.ouroboros/config.yaml`을 건드리기 **전에** 0이 아닌 코드로 종료합니다.

setup이 하는 일:

1. `kiro-cli`가 `PATH`에 있는지 확인합니다(또는 설정의 `OUROBOROS_KIRO_CLI_PATH` / `orchestrator.kiro_cli_path`를 존중).
2. `~/.ouroboros/config.yaml`에 씁니다:
   ```yaml
   orchestrator:
     runtime_backend: kiro
     kiro_cli_path: /usr/local/bin/kiro-cli  # 탐지된 경로
   llm:
     backend: kiro
   ```
3. `~/.kiro/settings/mcp.json`에 Ouroboros MCP 서버를 등록합니다. `env` 블록을 미리 채워 두어 `ooo <skill>` 단축이 Kiro 어댑터로 자동 디스패치되게 합니다:
   ```json
   {
     "mcpServers": {
       "ouroboros": {
         "command": "uvx",
         "args": ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
         "disabled": false,
         "env": {
           "OUROBOROS_RUNTIME": "kiro",
           "OUROBOROS_LLM_BACKEND": "kiro"
         }
       }
     }
   }
   ```

   `uvx`가 없고 `pipx`가 있으면 setup은 동등한 격리 launcher를 대신 씁니다:
   ```json
   {
     "command": "pipx",
     "args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
   }
   ```

setup은 멱등적입니다. 다시 돌려도 다른 MCP 항목과 커스텀 `env` 키는 보존됩니다. 이 항목은 **항상** `uvx` 또는 `pipx run`을 써서, 서버가 격리된 패키지 환경에서 MCP 2 의존성 프로파일을 받게 합니다. 첫 `uvx` 실행에서는 Kiro 쪽 타임아웃을 더 길게 줘야 할 수 있습니다. **setup은 MCP 메이저 버전을 알 수 없는 더 빠른 전역 바이너리로 절대 대체하지 않습니다.**

### 모델 목록 탐색

설정 UI는 전역 Kiro 모델 목록을 내장하는 대신 현재 인증된 Kiro 계정에서
사용 가능한 모델을 조회합니다. 먼저 `kiro-cli chat --listmodels -f json`을
실행하고, CLI 릴리스에 따라 새 표기인 `--list-models --format json`도
재시도합니다. 반환된 ID는 Kiro의 `default` sentinel을 유지한 채 선택 목록에
합쳐지므로 계정 등급, 지역, Enterprise 모델 allow list 변경이 로컬에 그대로
반영됩니다. 조회가 실패하거나 Kiro가 오프라인이면 외부 카탈로그를 추측해
사용하지 않고 `default`와 커스텀 모델 입력을 유지합니다.

## 사용

작업할 디렉터리에서 Kiro 세션을 엽니다:

```bash
cd ~/projects/my-new-idea
kiro-cli chat
```

세션 안에서 스킬 단축은 Claude Code나 Codex에서와 똑같이 동작합니다:

```
> ooo interview "I want to build a todo list CLI"
```

Kiro가 `ouroboros_interview` MCP 도구를 호출하고, 소크라테스식 질문을 스트리밍한 뒤, 답변을 위해 제어권을 돌려줍니다. 모호도 점수가 `≤ 0.2`로 떨어져 Ouroboros가 세션을 READY로 선언할 때까지 턴을 이어가세요. 그다음 `ooo seed`(또는 `ouroboros_generate_seed` 직접 호출)로 Seed YAML을 굳힙니다.

### 워크플로 실행

Seed가 생긴 뒤에는, Kiro 안에 머물며 `ouroboros_execute_seed`를 부르거나, 터미널에서 Kiro 런타임을 골라 돌립니다:

```bash
ouroboros run ~/.ouroboros/seeds/seed_<id>.yaml --runtime kiro
```

### 스킬 디스패치 계층

Kiro는 공통 무상태 `ouroboros.router` 리졸버에 `SkillInterceptor`를 더해 씁니다. `ooo <skill>`과 `/ouroboros:<skill>` 접두사는 Kiro 서브프로세스가 **뜨기 전에** 매칭됩니다. 따라서 스킬 디스패치는 Kiro / Codex / Claude에서 동일하게 동작합니다. [Shared `ooo` Skill Dispatch Router](../guides/ooo-skill-dispatch-router.md)(영문)를 보세요.

### 권한 모드

러너가 주도하는 seed 실행은 신규·재개 Kiro 디스패치 **양쪽 모두**에 `bypassPermissions`를 강제합니다. Kiro는 그 계약을 `--trust-all-tools`로 번역합니다. 같은 호출에 도구 봉투가 실려 있으면 봉투는 프롬프트 가이드로는 포함되지만, **네이티브 승인 경계를 `--trust-tools`로 낮추지는 못합니다.**

### 지정 재개 (호출자가 세션 id를 줄 때)

호출자가 알고 있는 세션 id를 넘기면 어댑터가 Kiro의 네이티브 `--resume-id` 플래그로 전달합니다(잘못됐거나 셸에 안전하지 않은 id는 argv 조립 시점에 거부됩니다):

```bash
kiro-cli chat --no-interactive --resume-id 6f8a3c21-... "next turn"
```

맨 `--resume`가 그 디렉터리의 **가장 최근** 세션을 재개하는 것과 달리, `--resume-id`는 요청한 id를 정확히 존중합니다.

Ouroboros는 평범한 `execute_task` 실행에서 Kiro 세션 id를 **현재 캡처하지 않습니다.** 헤드리스 모드가 stdout에 노출하지 않기 때문입니다. 그래서 내장 체크포인트/재개는 `kiro-cli chat --list-sessions -f json`으로 id를 **밖에서** 가져오는 호출자에게만 열려 있습니다. 아래 *선언된 능력*의 정직한 플래그를 보세요. 여기는 향후 작업 영역입니다.

## 선언된 능력

Kiro의 `KiroAgentAdapter.capabilities`는 다음으로 평가됩니다:

```python
RuntimeCapabilities(
    skill_dispatch=True,
    targeted_resume=False,
    structured_output=False,
)
```

`targeted_resume=False`는 Kiro 헤드리스 모드의 구체적 한계를 반영합니다. `kiro-cli chat --no-interactive`는 실행 중 stdout·stderr 어디에도 세션 id를 노출하지 않아서, 어댑터가 정상 실행에서 재개 가능한 핸들을 잡을 수 없습니다. 세션 id는 사후에 `kiro-cli chat --list-sessions`로만 보입니다. 어댑터는 호출자가 밖에서 구해 온 id로 `--resume-id <session_id>`를 여전히 이해하지만, **끝까지 지킬 수 없는 네이티브 재개 능력을 광고하지는 않습니다.** `--list-sessions -f json`을 완료 경로에 엮거나 `kiro-cli acp`를 채택하면 이 플래그가 뒤집힐 것입니다.

`structured_output=False`는 Kiro 헤드리스가 Claude·Codex가 내는 JSONL 이벤트 스트림 대신 **평문 stdout**(어댑터가 ANSI 프롬프트 마커를 제거)을 낸다는 뜻입니다. 구조화된 이벤트에 의존하는 호출자는 백엔드 이름이 아니라 `capabilities.structured_output`으로 분기하세요. 그래야 나중에 ACP 기반 Kiro 어댑터가 이 플래그를 뒤집어도 소비자가 깨지지 않습니다.

## 향후 작업: ACP

Kiro는 [Agent Client Protocol](https://kiro.dev/docs/cli/acp/) 표면(`kiro-cli acp`)도 노출합니다. 구조화된 JSON-RPC 이벤트와 더 풍부한 세션 관리를 제공하죠. 이 어댑터는 **의도적으로** 아직 쓰지 않습니다. `RuntimeCapabilities` + `SkillInterceptor` 추상화는 나중에 `KiroACPAdapter`를 그냥 추가하고 `structured_output=True`로 뒤집기만 하면 되도록, 호출자를 바꾸지 않아도 되게 쓰였습니다.

## 문제 해결

### Kiro 로그에 `connection closed: initialize response`

격리된 Ouroboros 서버가 응답하기 전에 Kiro의 MCP 초기화가 타임아웃됐습니다. `~/.kiro/settings/mcp.json`을 확인하세요. `command`는 `uvx` 또는 `pipx`여야 하고, 위 Setup에 나온 `ouroboros-ai[mcp]` 인자가 맞아야 합니다. 첫 `uvx` 실행은 패키지를 해석하는 동안 Kiro 시작 타임아웃을 더 길게 줘야 할 수 있습니다. **이 항목을 `ouroboros`나 `python -m` 직접 실행으로 절대 바꾸지 마세요.** 그런 환경은 MCP 2를 보장할 수 없습니다.

### `I don't have a tool called ouroboros_*`

MCP 서버가 다른 이름으로 로드됐거나 아예 로드되지 않았습니다. `uvx` 또는 `pipx`가 `PATH`에 있는지 확인하고, 아무 터미널에서나 `ouroboros setup --runtime kiro`를 다시 돌린 뒤 Kiro 세션을 재시작하세요. setup은 레거시 직접 바이너리 항목을 지원되는 격리 launcher로 교체하며, launcher가 없으면 **런타임 변경을 저장하지 않고** 0이 아닌 코드로 종료합니다.

### 응답이 `> `로 시작하거나 이스케이프 시퀀스가 섞여 나옴

Kiro의 헤드리스 stdout에는 터미널 프롬프트 마커가 그대로 실립니다. 어댑터가 SGR/CSI 이스케이프와 앞머리 `> ` 마커를 제거한 뒤 내용을 넘깁니다. 그게 새어 나온다면 수정 이전의 옛 Ouroboros 휠을 쓰고 있을 가능성이 큽니다. 커밋 `9d0db8a`(`fix(kiro): strip ANSI prompt marker + color escapes from stdout`)가 포함된 버전으로 다시 설치하세요.

## 더 읽을거리

- [Kiro CLI 헤드리스 모드](https://kiro.dev/docs/cli/headless/) — 업스트림 문서
- [런타임 능력 매트릭스](../runtime-capability-matrix.md) — 런타임 간 비교
- [스킬 디스패치 라우터](../guides/ooo-skill-dispatch-router.md) — `ooo` 단축이 라우팅되는 방식
- [`kiro-cli acp` 문서](https://kiro.dev/docs/cli/acp/) — 업스트림 ACP 표면. 이 어댑터는 사용하지 않음
