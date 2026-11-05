# Pi CLI 런타임

LLM completion 사용량은 실행별 최종 `agent_end` 스냅샷의 assistant 메시지에서 합산합니다. 앞선 message/turn completion 이벤트는 같은 메시지를 반복하므로 다시 집계하지 않습니다. 별도로 더해지는 캐시 토큰은 `unallocated_tokens`로 보존하며, 캐시 보존 및 reasoning 세부 항목은 중복해서 더하지 않습니다.

로컬에 설치된 [`pi`](https://github.com/earendil-works/pi) CLI를 사용해 Ouroboros 워크플로를 실행합니다.

Pi 런타임은 서브프로세스 어댑터입니다. 워크플로 엔진, Seed 분해, 체크포인트, 평가 인계와 `ooo` 스킬 디스패치는 Ouroboros가 담당합니다. 각 런타임 작업에서 Pi JSON 모드를 실행하고, Pi의 JSONL 이벤트를 Ouroboros의 `AgentMessage` 값으로 정규화합니다.

## 동작 구조

계층은 세 개로 나뉩니다.

```text
User / CLI / MCP
      |
      | 1. Selects runtime_backend: pi, or sends an ooo shortcut
      v
Ouroboros runtime adapter
      |
      | 2a. ooo shortcut? handle inside Ouroboros before Pi starts
      | 2b. normal task? spawn Pi JSON mode
      v
pi --mode json <prompt>
      |
      | 3. Pi loads its own settings, packages, extensions, tools, model auth
      v
Pi model turn and JSONL events
```

따라서 “Pi가 Ouroboros 런타임이다”라는 뜻은 2b 경로가 존재하고 선택 가능하다는 것입니다. Pi 패키지를 Ouroboros로 가져온다는 뜻은 아닙니다. 또한 setup으로 관리형 Pi 브리지 확장을 설치하지 않는 한, Pi의 대화형 명령 UI가 Ouroboros 명령 라우터의 일부가 된다는 뜻도 아닙니다.

## 사전 준비

| 요구 사항 | 이유 |
|-----------|------|
| `pi` CLI | 제공자 런타임. Pi를 설치해 `pi`를 `PATH`에 두거나 명시적 경로를 설정합니다 |
| Pi 인증 | 처음 사용하기 전에 Pi 제공자 로그인 절차를 실행합니다 |
| Ouroboros 기본 패키지 | `pip install ouroboros-ai` |

Pi에서 OpenAI 구독 기반 Codex 모델을 사용하려면 Pi의 `openai-codex` 제공자 로그인 및 모델 경로를 사용하세요. 일반 `openai` 제공자 경로는 API 키 중심이며 같은 인증 경로가 아닙니다.

## 빠른 시작

```bash
# 1. Pi를 설치하고 인증합니다
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi
# 대화형 Pi 세션에서 /login을 실행하고 openai-codex를 선택합니다.

# 2. Ouroboros에서 Pi를 설정하고 Pi용 ooo 브리지를 설치합니다
ouroboros setup --runtime pi

# 3. 설정된 런타임으로 워크플로를 실행합니다
ouroboros run workflow seed.yaml

# 4. Pi 또는 roach-pi 등 사용자 지정 Pi를 재시작하거나 /reload를 실행한 뒤:
ooo auto build a small CLI
```

Pi가 `PATH` 밖에 설치되어 있다면 다음을 설정합니다.

```bash
export OUROBOROS_PI_CLI_PATH=/absolute/path/to/pi
```

또는 다음과 같이 설정합니다.

```yaml
orchestrator:
  runtime_backend: pi
  pi_cli_path: /absolute/path/to/pi
```

## 런타임 계약

일반 실행 작업에서 Ouroboros는 다음을 실행합니다.

```text
pi --mode json [--model <MODEL>] [--session <SESSION_ID>]
  [--append-system-prompt <SYSTEM>] [--tools <TOOLS>] [--no-tools] <PROMPT>
```

| 인자 | 역할 |
|------|------|
| `--mode json` | Pi의 헤드리스 JSONL 이벤트 스트림을 요청합니다 |
| `--model` | 호출자가 전달하는 선택적 모델 override입니다 |
| `--session` | 특정 세션 재개에 사용하는 선택적 Pi 기본 세션 ID입니다 |
| `--append-system-prompt` | Ouroboros의 `system_prompt`를 Pi의 기본 코딩 프롬프트 뒤에 Pi 고유 방식으로 추가합니다 |
| `--tools` | Pi 자체 플래그로 나열된 도구만 허용합니다. Claude 형식 이름(`Read`, `Bash`, `Glob`, …)은 Pi 소문자 기본 도구(`read`, `bash`, `find`, …)로 매핑하고, 알 수 없는 이름은 확장 도구를 위해 그대로 전달합니다 |
| `--no-tools` | 명시적 도구 미사용 모드입니다. Ouroboros가 `tools=[]`를 요청하면 출력합니다. 기본값 사용(`tools=None`, 플래그 생략)과 모든 도구 비활성화를 구분합니다 |
| `<PROMPT>` | Ouroboros에서 조합한 작업 프롬프트입니다 |

Pi 고유 파라미터 플래그는 `pi --help`로 한 번 확인하고 각각 별도로 기록합니다. 시스템 프롬프트와 비어 있지 않은 허용 목록을 Pi 고유 방식으로 전달하려면 `--append-system-prompt`와 `--tools` 경로가 모두 필요합니다. 그렇지 않으면 해당 파라미터를 사용자 메시지에 조합하고 `translated`로 보고합니다. 빈 목록 제한은 `empty_tool_restriction_support`로 별도 노출하고 영속 런타임 capability 계약에 포함합니다. 따라서 `--no-tools`만 있는 Pi 바이너리는 도구를 비활성화할 수 없는 바이너리와 협상 및 실행 의미의 fingerprint가 다릅니다. `--no-tools`를 사용할 수 없다면 Ouroboros는 `tools=[]`를 제한 없는 기본값으로 조용히 넓히지 않고 Pi를 실행하기 전에 `ToolRestrictionUnenforced`를 반환합니다.

Ouroboros는 초기 `session` 이벤트를 `RuntimeHandle`로 파싱하고, `message_update`의 `text_delta` 이벤트를 assistant 출력으로 스트리밍하며, `message_end`, `turn_end` 또는 `agent_end` 이벤트에서 최종 assistant 텍스트를 읽습니다.

Pi는 프로세스가 종료 상태 `0`으로 끝나더라도 제공자·모델 실패를 `stopReason: "error"`인 assistant 메시지로 보고할 수 있습니다. Ouroboros는 프로세스 반환 코드에만 의존하지 않고 이런 이벤트를 런타임 오류로 처리합니다.

## Pi에서 `ooo`의 의미

지원되는 진입 경로는 두 가지입니다.

### Ouroboros가 Pi를 실행하는 경우

Ouroboros가 이미 제어 중이고 `runtime_backend: pi`가 선택되어 있으면, Pi 서브프로세스를 시작하기 전에 Ouroboros가 `ooo <skill>`을 처리합니다.

Pi 런타임은 `PiRuntime.execute_task()`의 시작 부분에서 공통 `SkillInterceptor`를 호출합니다. 프롬프트가 `ooo interview` 또는 `/ouroboros:ouroboros-run` 같은 Ouroboros 스킬 단축 명령이면, 인터셉터가 스킬을 찾고 대응하는 Ouroboros MCP 핸들러를 호출합니다. Pi는 해당 프롬프트를 일반 대화 입력으로 받지 않습니다.

이는 다음을 의미합니다.

- Ouroboros가 제어하는 Pi 런타임에서 `ooo interview`는 “설정된 LLM 백엔드로 작성 작업을 수행하며 Ouroboros가 인터뷰 명령을 처리한다”는 뜻입니다.
- 명령 디스패치 경로가 입력을 `ooo` 단축 명령이 아니라고 판정한 뒤에만 Pi가 일반 Seed 실행 프롬프트를 수행합니다.

### Pi 또는 roach-pi가 Ouroboros를 실행하는 경우

`ouroboros setup --runtime pi`는 관리형 전역 Pi 확장도 설치합니다.

```text
~/.pi/agent/extensions/ouroboros-ooo-bridge.ts
```

Pi는 해당 디렉터리의 확장을 자동으로 불러옵니다. Pi를 재시작하거나 `/reload`를 실행한 뒤에는 roach-pi 같은 사용자 지정 Pi 설정을 포함한 대화형 세션에서 다음과 같이 입력할 수 있습니다.

```text
ooo auto build a small CLI
ooo interview clarify this feature
/ooo status auto --resume auto_...
```

확장은 정확한 접두사 `ooo ...` 입력을 가로채 다음을 실행합니다.

```text
ouroboros dispatch --runtime pi --cwd <pi-session-cwd> "ooo ..."
```

이 숨겨진 `dispatch` 진입점은 런타임 어댑터와 같은 공통 스킬 해석기 및 MCP 핸들러 구성을 사용합니다. 이는 의도적으로 roach-pi 전용 어댑터가 아닙니다. roach-pi는 Pi가 불러오는 사용자 지정 설정으로 남고, Ouroboros는 `ooo` 명령을 Ouroboros에 전달하는 브리지만 소유합니다.

브리지는 MCP 기반 스킬 frontmatter를 통해 숨겨진 디스패처가 실행할 수 있는 명령만 소비합니다. `ooo help`나 단독 `ooo`처럼 자체 단축 명령이지만 MCP 디스패치 대상을 선언하지 않은 명령은 결정적인 unsupported-dispatch 종료 코드와 함께 Pi로 반환됩니다. 따라서 브리지가 강제 실패하는 대신 일반 Pi 세션이 입력을 계속 처리할 수 있습니다.

등록된 `/ooo` 명령은 Pi 고유의 `getArgumentCompletions`를 통해 TAB 인자 완성도 제공합니다. `/ooo <TAB>`은 한 줄 설명과 함께 디스패치 가능한 하위 명령을 나열하고, `ooo run <TAB>`은 `~/.ouroboros/seeds/`의 Seed 파일을 절대 저장소 경로로 나열합니다. 따라서 Pi 세션 디렉터리에 관계없이 완성된 값을 실행할 수 있습니다(상대 Seed 경로는 세션 cwd를 기준으로 해석). 완성은 결정적이며 오프라인입니다. 하위 명령 목록은 디스패처가 적격하다고 판단하는 스킬을 반영하며, 단위 테스트가 `resolve_skill_dispatch`로 그 집합을 도출해 불일치를 방지합니다. 디스패처가 공백으로 명령을 토큰화하므로 이름에 공백이 포함된 Seed는 건너뜁니다.

`ooo auto`에서는 디스패처가 백그라운드 작업 수명 주기를 담당합니다. `ouroboros_start_auto`가 `job_id`를 반환하면 디스패치 프로세스가 `ouroboros_job_wait`을 폴링하고 작업이 종료 상태에 도달하면 `ouroboros_job_result`를 가져옵니다. 따라서 Pi로 진입했다는 이유만으로 사용자가 백그라운드 작업을 직접 폴링할 필요 없이 일반 `ooo auto` 계약을 유지합니다.

## `ooo auto --runtime pi`

`ooo auto`에는 서로 다른 두 완료 수준이 있습니다.

| 명령 형태 | 완료되는 범위 | Pi 관여 |
|-----------|---------------|---------|
| `ouroboros auto --runtime pi ...` | 인터뷰, Seed 생성, Seed QA 및 실행 인계 | Pi 런타임의 실행 인계를 시작하며, 최종 제품은 아직 대기 중일 수 있습니다 |

실행 경로에서 모델 override를 전달하지 않으면 Pi 자체 기본값으로 모델을 선택합니다. 재현 가능한 smoke test를 위해 다음을 설정합니다.

```bash
export OUROBOROS_EXECUTION_MODEL=openai-codex/gpt-5.4-mini
```

그러면 Pi 런타임 실행에 다음이 포함됩니다.

```text
pi --mode json --model openai-codex/gpt-5.4-mini <PROMPT>
```

Auto는 보통 Ouroboros가 관리하는 작업 worktree에서 실행됩니다. 따라서 파일을 쓰는 smoke test가 성공해도 파일은 셸의 원래 checkout이 아니라 `~/.ouroboros/worktrees/...` 아래에 생성될 수 있습니다.

## Pi 패키지와 roach-pi

Pi 패키지와 확장은 Pi 자체가 불러옵니다. 사용자가 Pi 설정에 `git:github.com/tmdgusya/roach-pi` 같은 패키지를 설치했다면, Ouroboros가 실행한 Pi 서브프로세스도 Pi의 일반 확장 로더를 통해 해당 패키지를 불러올 수 있습니다.

이는 `roach-pi`가 Ouroboros 런타임 어댑터라는 뜻과는 다릅니다.

| 상황 | 계약 |
|------|------|
| Ouroboros가 `pi --mode json` 실행 | 지원되는 Pi 런타임 경로 |
| Pi가 해당 프로세스에서 설치된 패키지·확장을 로드 | Pi가 허용하며 Pi 실행에 표시됨 |
| 패키지가 Pi 모델 턴에 쓰이는 헤드리스 호환 도구·hook을 추가 | Pi 내부에서 실행되므로 동작할 수 있음 |
| 패키지가 대화형 슬래시 명령 또는 UI 프롬프트를 추가 | 일반 대화형 Pi에서는 동작하지만 Ouroboros가 실행한 JSON 모드에서는 보장되지 않음 |
| 대화형 Pi/roach-pi 사용자가 setup 브리지 설치 후 `ooo ...` 입력 | 관리형 Pi 확장을 통해 지원 |
| `roach-pi` 자체가 선택 가능한 Ouroboros 런타임 백엔드가 됨 | 아니요. 별도 어댑터 또는 브리지가 필요함 |

`runtime_backend: pi`는 Pi CLI를 실행 엔진으로 선택합니다. 사용자 지정 Pi 배포판은 해당 Pi 프로세스 내부 동작에 영향을 줄 수 있지만, 런타임 실행은 Pi의 JSON 모드 서브프로세스 계약에, 대화형 `ooo` 브리지는 Pi가 문서화한 전역 확장 로더에만 의존합니다.

## LLM 백엔드로서의 Pi

작성, 점수 계산, 추출 및 기타 completion 흐름의 LLM 백엔드로 Pi를 선택할 수도 있습니다.

```yaml
llm:
  backend: pi
```

이는 `orchestrator.runtime_backend`와 별개입니다.

Pi LLM 어댑터는 소프트 강제를 통해 구조화된 `response_format` 요청을 지원합니다. Ouroboros가 엄격한 JSON/schema 지시를 삽입하고 Pi 응답에서 JSON 페이로드를 추출해, 반환하기 전에 `json_schema` 페이로드를 검증합니다. Pi JSON 모드는 현재 Codex 스타일의 제공자 고유 `--output-schema` 하드 강제 플래그를 노출하지 않으므로, 잘못된 구조의 응답은 재시도한 뒤 제공자 오류로 보고됩니다.

Pi가 Seed 작업을 실행하게 하려면 Pi를 런타임 백엔드로 사용하세요. 작성·평가 흐름에서 제공자 고유 스키마 강제 대신 어댑터 수준의 JSON 추출과 검증을 받아들일 수 있다면 `llm.backend: pi`를 사용하세요.

## 기능

| 기능 | 상태 |
|------|------|
| 헤드리스 실행 | 예, `pi --mode json` 사용 |
| 스킬 단축 명령 디스패치 | 예, Pi 실행 전 |
| 특정 세션 기본 재개 | 예, `--session <id>` 사용 |
| 구조화 이벤트 스트림 | 예, `PiRuntime`이 JSONL 파싱 |
| LLM 백엔드의 구조화 스키마 응답 | 소프트 강제 및 검증 |
| Pi 확장 로딩 | Pi가 담당하며 헤드리스 JSON 모드와 호환되면 동작 |
| 대화형 Pi의 `ooo` 진입점 | 예, setup이 설치하는 관리형 확장 사용 |

## 문제 해결

**`Pi not found`**

Pi를 설치하고 `pi`를 `PATH`에 추가하거나 `OUROBOROS_PI_CLI_PATH`를 설정하세요.

**Pi에서는 OpenAI OAuth가 동작하지만 Ouroboros는 실패하는 경우**

모델 문자열을 확인하세요. Pi의 구독 기반 OpenAI Codex 모델에는 일반 OpenAI API 키 제공자 경로가 아닌 `openai-codex/...` 모델·제공자 경로를 사용해야 합니다.

**roach-pi 슬래시 명령이 아무 동작을 하지 않는 것처럼 보이는 경우**

명령이 Pi의 대화형 UI 문맥에 의존할 수 있습니다. Ouroboros는 Pi를 일회성 JSON 모드로 실행하므로 대화형 슬래시 명령 UX가 보장되지 않습니다. Ouroboros 워크플로에서는 일반 Seed 실행 프롬프트나 헤드리스 호환 Pi 확장을 사용하세요.

**Pi 안에서 `ooo ...`가 일반 대화로 모델에 전달되는 경우**

`ouroboros setup --runtime pi`를 실행한 뒤 Pi를 재시작하거나 `/reload`를 실행하세요. `~/.pi/agent/extensions/ouroboros-ooo-bridge.ts`가 존재하는지 확인하세요.

## Active Conductor와 Synapse

Pi CLI는 정확히 같은 Pi 프로젝트 세션 ID를 사용하며 Synapse `inform`/`after_turn` 백엔드로 검증됐습니다. 실시간 체크포인트 `redirect`나 하드 `replace` 지원을 주장하지 않습니다.

실행 중에는 단일 독점 읽기 전용 observer가 현재 런타임·모델, 효율성 보장, 제한된 Discover 대상, 의존성·병렬 수준, 처음 예약된 AC, 주의 사항과 최종 보장을 전달합니다. 메인 세션은 계속 사용할 수 있으며 사용자에게 ID를 묻는 대신 관련 AC를 의미를 기준으로 선택합니다. 영어가 정본 지시 언어이고, 호스트가 현재 대화 언어에 맞춰 자연스럽게 UX를 렌더링합니다.

기본 완료 또는 stdout EOF 이후 런타임은 worker가 종료하고 stderr를 닫도록 5초를 부여합니다. 이 종료 단계가 지연되면 worker를 종료하고 `TimeoutError`를 반환합니다. 활성 stdout 시작 및 유휴 예산은 바뀌지 않습니다.
