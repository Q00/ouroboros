# OMP CLI 런타임

LLM 완료 사용량은 실행별 최종 `agent_end` 스냅샷의 assistant 메시지를 합산합니다. 앞선 메시지·턴 완료 이벤트는 같은 메시지를 반복하므로 다시 세지 않습니다. 합산 가능한 캐시 토큰은 `unallocated_tokens`로 보존하며, 캐시 보존 및 추론 세부 항목을 중복해서 더하지 않습니다.

로컬에 설치된 OMP("Oh My Pi", `omp` 바이너리) CLI를 사용해 Ouroboros 워크플로를 실행합니다.

OMP는 Pi 계열 코딩 에이전트로, Pi와 같은 JSON 이벤트 프로토콜을 사용합니다. 따라서 OMP 런타임은 Pi 런타임과 같은 방식의 서브프로세스 어댑터입니다. 워크플로 엔진, Seed 분해, 체크포인트, 평가 인계와 `ooo` 스킬 디스패치는 Ouroboros가 담당합니다. 각 런타임 작업에서 OMP JSON 모드를 실행하고 OMP의 JSONL 이벤트를 Ouroboros `AgentMessage` 값으로 정규화합니다.

## 동작 구조

세 계층이 구분됩니다.

```text
사용자 / CLI / MCP
      |
      | 1. runtime_backend: omp 선택 또는 ooo 단축 명령 전달
      v
Ouroboros 런타임 어댑터
      |
      | 2a. ooo 단축 명령? OMP 시작 전에 Ouroboros가 처리
      | 2b. 일반 작업? OMP JSON 모드 실행
      v
omp --mode json <prompt>
      |
      | 3. OMP가 자체 설정, 확장, 도구, 모델 설정을 로드
      v
OMP 모델 턴과 JSONL 이벤트
```

"OMP가 Ouroboros 런타임이다"는 2b 경로가 존재하고 선택 가능하다는 뜻입니다. OMP 확장을 Ouroboros에 가져온다는 뜻은 아닙니다. setup이 관리형 OMP 브리지 확장을 설치하지 않았다면 OMP의 대화형 명령 인터페이스가 Ouroboros 명령 라우터에 포함된다는 뜻도 아닙니다.

## 사전 요구 사항

| 요구 사항 | 이유 |
|-----------|------|
| `omp` CLI | 제공자 런타임입니다. Oh My Pi를 설치해 `omp`를 `PATH`에 두거나 명시적 경로를 설정하세요. |
| OMP 모델 인증 | OMP는 구성된 모델 역할(`smol`, `slow`, `plan`)에 따라 모델을 선택합니다. 처음 사용하기 전에 OMP 자체 모델 설정을 완료하세요. |
| Ouroboros 기본 패키지 | `pip install ouroboros-ai` |

Ouroboros 어댑터 안에는 OMP 제공자 로그인 인터페이스가 없습니다. 모델 선택과 자격 증명은 OMP 자체 설정에 있습니다. 실행 경로가 명시적 재정의를 제공할 때만 Ouroboros가 `--model`을 전달합니다.

## 빠른 시작

```bash
# 1. Oh My Pi를 설치해 omp가 PATH에 있도록 합니다.
#    (또는 아래 OUROBOROS_OMP_CLI_PATH / orchestrator.omp_cli_path 설정)

# 2. Ouroboros에서 OMP를 선택하고 OMP 측 ooo 브리지를 설치합니다.
ouroboros setup --runtime omp

# 3. 설정된 런타임으로 워크플로를 실행합니다.
ouroboros run workflow seed.yaml

# 4. 대화형 OMP에서 브리지 확장이 로드되도록 OMP를 재시작한 뒤 실행합니다.
ooo auto build a small CLI
```

`omp`가 `PATH`에 있으면 설치 프로그램과 `ouroboros update`는 `omp config set extensionHandlers.toolCallTimeoutMs 60000`을 통해 OMP의 MCP 도구 호출 시간 제한도 60초로 늘립니다.

OMP가 `PATH` 밖에 설치됐다면 다음을 설정하세요.

```bash
export OUROBOROS_OMP_CLI_PATH=/absolute/path/to/omp
```

또는 다음과 같이 구성합니다.

```yaml
orchestrator:
  runtime_backend: omp
  omp_cli_path: /absolute/path/to/omp
```

경로 확인 우선순위는 `OUROBOROS_OMP_CLI_PATH`, `orchestrator.omp_cli_path`, `PATH` 순서입니다.

## 런타임 계약

일반 실행 작업에서 Ouroboros는 다음을 실행합니다.

```text
omp --mode json [--model <MODEL>] [--resume <SESSION_ID>]
  [--append-system-prompt <SYSTEM>] [--tools <TOOLS>] [--no-tools] <PROMPT>
```

| 인자 | 용도 |
|------|------|
| `--mode json` | OMP의 헤드리스 JSONL 이벤트 스트림을 요청합니다. |
| `--model` | 호출자가 전달한 선택적 모델 재정의입니다. OMP의 `--model`은 유사 모델명을 매칭하고 `default` ID가 없으므로, Ouroboros는 범용 `default` 센티널을 생략하고 OMP의 자체 설정 모델을 사용하게 합니다. |
| `--resume` | 지정된 OMP 세션을 재개하기 위한 선택적 네이티브 세션 ID입니다. Pi는 같은 목적으로 `--session`을 쓰지만 OMP는 `--resume`을 씁니다. |
| `--append-system-prompt` | Ouroboros `system_prompt`를 네이티브 방식으로 전달해 OMP의 기본 코딩 프롬프트에 덧붙입니다. |
| `--tools` | 네이티브 도구 허용 목록입니다. OMP 자체 플래그가 나열된 도구만 활성화합니다. Claude 형식 이름은 OMP의 소문자 내장 도구로 매핑됩니다(`Read`→`read`, `Bash`→`bash`, `Glob`→`glob` 등). OMP의 디렉터리 열거 도구는 `glob`입니다(Pi의 `find`는 OMP에서 Pi 호환 별칭일 뿐). OMP에는 `ls` 내장 도구가 없으므로 `Glob`과 `LS` 모두 `glob`으로 매핑합니다. 알 수 없는 이름은 그대로 전달해 확장 도구(`mcp__*`, `task`, `todo`, `web_search`, `eval`, `hub` 등)가 동작하도록 합니다. |
| `--no-tools` | 도구를 전혀 쓰지 않는 명시적 모드입니다. Ouroboros가 `tools=[]`를 요청하면 출력합니다. 기본값 사용(`tools=None`, 플래그 생략)과 모든 도구 비활성화를 구분합니다. |
| `<PROMPT>` | Ouroboros가 구성한 작업 프롬프트를 위치 메시지 인자로 전달합니다. OMP JSON 모드는 작업을 위치 인자로 받으므로 stdin에도 전달하면 중복됩니다. 따라서 stdin은 사용하지 않습니다. |

`--append-system-prompt`, `--tools`, `--no-tools`는 `omp` CLI에 문서화된 플래그이므로 Pi와 달리 `--help` 기능 탐색 없이 항상 네이티브 방식으로 전달합니다. OMP가 시작 시 실제로 알 수 없는 도구 이름을 거부하면 CLI 사용 오류가 일반 런타임 오류 결과로 나타납니다.

Ouroboros는 초기 `session` 이벤트를 `RuntimeHandle`로 파싱하고, `message_update`의 `text_delta` 이벤트를 assistant 출력으로 스트리밍하며, `message_end`, `turn_end`, `agent_end` 이벤트에서 최종 assistant 텍스트를 읽습니다. `tool_execution_start`와 `tool_execution_end`는 서로 연결된 도구 호출과 결과가 됩니다. `tool_execution_update`는 완료 증거가 아닙니다.

### 검증 지원

OMP 도구 실행 증거를 지원합니다(OMP 18.7.0으로 검증). 네이티브 `bash` 호출은 명령 입력과 `toolCallId`를 유지한 채 `Bash`로 정규화합니다. 완료 메시지는 결과 텍스트, 오류 판정, 명령 종료 상태를 실행 저널의 `execution.tool.completed`까지 전달합니다. 검증기는 작업자의 최종 요약을 신뢰하는 대신 이 런타임 메시지와 `commands_run`, 지원되는 `tests_passed` 주장을 대조할 수 있습니다. 대화 기록에 없는 명령은 여전히 근거가 없습니다.

OMP는 성공한 동기 Bash 완료에서 `details.exitCode`를 생략합니다. `result.details` 객체가 존재하고 유효한 `isError: false`가 명시된 완료는 종료 상태 0으로 정규화합니다. 명시적으로 존재하는 빈 객체는 유효하지만 `result.details`가 없거나 객체가 아니면 증거가 불완전하므로 성공을 입증할 수 없습니다.

0이 아닌 종료 코드, 오류 플래그, 잘못된 판정은 성공한 테스트 증거가 되지 않습니다. 백그라운드 작업 시작은 명령 완료 증거가 아닙니다. 시작 자체가 `isError: false`를 보고해도 `details.async` 결과는 성공 종료를 부여하지 않습니다. 명령 완료로 테스트 주장을 뒷받침해야 한다면 검증 명령을 동기식으로 실행하세요.

검사 패키지 생성자 호출은 `omp --mode json --no-session ...`을 사용해 비공개 입력과 기대값이 OMP 세션에 저장되지 않도록 합니다. 어댑터는 응답 텍스트를 로그에 남기지 않습니다. 이 스위치는 생성자의 런타임 인스턴스에만 적용됩니다. 일반 작업자는 세션 영속성과 지정 재개 기능을 유지합니다. 다른 런타임과 마찬가지로 검사 패키지를 만들려면 생성자가 유효한 구조화 응답을 받아야 합니다.

## OMP에서 `ooo`의 의미

지원되는 진입 경로는 두 가지입니다.

### Ouroboros가 OMP를 실행하는 경우

Ouroboros가 이미 제어 중이고 `runtime_backend: omp`가 선택돼 있으면 OMP 서브프로세스가 시작되기 전에 Ouroboros가 `ooo <skill>`을 처리합니다.

OMP 런타임은 `OmpRuntime.execute_task()` 시작 시 공통 `SkillInterceptor`를 호출합니다. 프롬프트가 `ooo interview` 같은 Ouroboros 스킬 단축 명령이면 인터셉터가 스킬을 해석해 해당 Ouroboros MCP 핸들러를 호출합니다. OMP는 해당 프롬프트를 일반 채팅 입력으로 받지 않습니다.

이는 다음을 뜻합니다.

- Ouroboros가 제어하는 OMP 런타임의 `ooo interview`는 "설정된 LLM 백엔드로 작성하면서 Ouroboros가 인터뷰 명령을 처리한다"는 뜻입니다.
- 명령 디스패치 경로가 입력을 `ooo` 단축 명령이 아니라고 판단한 뒤에만 OMP가 일반 Seed 실행 프롬프트를 처리합니다.

### OMP가 Ouroboros를 실행하는 경우

`ouroboros setup --runtime omp`는 관리형 전역 OMP 확장도 설치합니다.

```text
~/.omp/agent/extensions/ouroboros-ooo-bridge.ts
```

OMP는 이 디렉터리의 확장을 자동으로 발견합니다(Pi의 `~/.pi/agent/extensions`와 같은 구조). OMP를 재시작하면 대화형 OMP 세션에서 다음을 입력할 수 있습니다.

```text
ooo auto build a small CLI
ooo interview clarify this feature
/ooo status auto --resume auto_...
```

확장은 정확한 `ooo ...` 접두사를 가진 입력을 가로채 다음을 실행합니다.

```text
ouroboros dispatch --runtime omp --cwd <omp-session-cwd> "ooo ..."
```

이 숨겨진 `dispatch` 진입점은 런타임 어댑터와 동일한 공통 스킬 해석기 및 MCP 핸들러 구성을 사용합니다. 등록된 `/ooo` 명령은 TAB 인자 자동완성도 제공하며, `OUROBOROS_OMP_BRIDGE_TIMEOUT_MS`로 디스패치 시간 제한을 조절할 수 있습니다.

Pi 브리지와 마찬가지로 이 확장은 숨겨진 디스패처가 실행할 수 있는 명령만 소비합니다. MCP 디스패치 대상이 없는 내장 단축 명령은 결정적인 미지원 디스패치 종료 코드와 함께 OMP 세션으로 반환하므로 세션에서 입력 처리를 계속할 수 있습니다.

## `ooo auto --runtime omp`

`ooo auto`에는 서로 다른 두 완료 수준이 있습니다.

| 명령 형식 | 완료되는 범위 | OMP 관여 |
|-----------|---------------|----------|
| `ouroboros auto --runtime omp ...` | 인터뷰, Seed 생성, Seed QA, 실행 인계 | OMP 런타임을 위한 실행 인계를 시작합니다. 최종 제품은 아직 완료되지 않았을 수 있습니다. |

실행 경로가 모델을 재정의하지 않으면 OMP 자체 모델 역할 설정에서 모델을 선택합니다. 재정의가 있으면 OMP 런타임 실행에 다음이 포함됩니다.

```text
omp --mode json --model <MODEL> <PROMPT>
```

범용 `default` 모델 센티널은 OMP에 전달하지 않습니다. OMP의 `--model` 유사 매칭에 `default` ID가 없으므로 센티널을 생략하고 OMP가 자체 모델을 선택하게 합니다.

Auto는 보통 Ouroboros가 관리하는 작업 worktree에서 실행됩니다. 따라서 파일을 쓰는 스모크 테스트가 성공해도 셸의 원래 체크아웃이 아닌 `~/.ouroboros/worktrees/...` 아래에 파일이 생성될 수 있습니다.

## OMP 확장과 보안

OMP는 전역 확장 디렉터리에서 확장을 직접 로드합니다. Setup은 여기에 관리형 파일 하나, 즉 `ooo` 브리지만 기록하므로 OMP 세션은 자체 확장을 함께 유지할 수 있습니다.

OMP가 실행된 CLI의 탐색 환경을 통해 에이전트 디렉터리(세션, 확장, 생성된 모든 세션에서 로드되는 규칙)를 결정하기 때문에 신뢰할 수 없는 저장소의 `.env` 파일에서는 해당 탐색 변수(`PI_CODING_AGENT_DIR`, Pi와 공유)를 허용하지 않습니다. 신뢰할 수 없는 저장소가 실행된 `omp`를 공격자 제공 확장으로 가리키게 할 수 없습니다.

## LLM 백엔드로서의 OMP

작성, 점수 산정, 추출과 그 밖의 completion 흐름에서 OMP를 LLM 백엔드로 선택할 수도 있습니다.

```yaml
llm:
  backend: omp
```

이는 `orchestrator.runtime_backend`와 별도입니다. CLI에서도 `ouroboros init --llm-backend omp`와 `ouroboros mcp serve --llm-backend omp`로 선택할 수 있고, `omp`를 허용하는 모든 곳에서 `omp_cli` 별칭도 사용할 수 있습니다.

`OmpLLMAdapter`는 LLM 전용 흐름에서 같은 `omp --mode json` 스트림을 사용합니다. 구조화된 `response_format` 요청은 소프트 강제로 지원합니다. Ouroboros가 엄격한 JSON/스키마 지시를 삽입하고 OMP 응답에서 JSON 페이로드를 추출하며, 반환 전에 `json_schema` 페이로드를 검증합니다. OMP JSON 모드에는 강제 `--output-schema` 플래그가 없으므로 잘못된 구조화 응답은 재시도한 뒤 제공자 오류로 표시합니다. Pi LLM 어댑터와 같은 계약입니다. 범용 교차 제공자 기본 모델은 백엔드에 안전한 `default` 센티널로 정규화되며 OMP는 이를 자체 설정 모델로 해석합니다.

OMP가 Seed 작업을 실행해야 한다면 런타임 백엔드로 사용하세요. 제공자 네이티브 스키마 강제 대신 어댑터 수준의 JSON 추출과 검증을 작성·평가 흐름에서 수용할 수 있다면 `llm.backend: omp`를 사용하세요.

## 기능

| 기능 | 상태 |
|------|------|
| 헤드리스 실행 | 지원, `omp --mode json` 사용 |
| 스킬 단축 명령 디스패치 | 지원, OMP 실행 전 처리 |
| 네이티브 지정 재개 | 지원, `--resume <id>` 사용 |
| 구조화 이벤트 스트림 | 지원, `OmpRuntime`이 JSONL 파싱 |
| 네이티브 시스템 프롬프트와 도구 허용 목록 | 지원, 문서화된 `--append-system-prompt` / `--tools` / `--no-tools` 플래그 사용 |
| LLM 백엔드의 구조화 스키마 응답 | 소프트 강제 및 검증 |
| OMP 확장 로드 | OMP 담당; 관리형 브리지는 전역 확장 디렉터리에 설치 |
| 대화형 OMP `ooo` 진입점 | 지원, setup이 설치하는 관리형 확장 사용 |

## 문제 해결

**`OMP not found`**  
Oh My Pi를 설치해 `omp`가 `PATH`에 있도록 하거나 `OUROBOROS_OMP_CLI_PATH` / `orchestrator.omp_cli_path`를 설정하세요.

**`--tools`가 CLI 사용 오류를 보고함**  
OMP는 시작 시 실제로 알 수 없는 도구 이름을 거부합니다. 요청한 이름이 OMP 내장 도구(`read`, `write`, `edit`, `bash`, `grep`, `glob`, `find`, `lsp`), `mcp__*` 같은 확장 도구 또는 Ouroboros가 매핑하는 Claude 형식 이름(`Read`, `Glob`, `LS` 등)인지 확인하세요. `ls` 내장 도구는 없으며 디렉터리 열거에는 `glob`을 사용합니다.

**모델이 잘못됐거나 무시되는 것 같음**  
OMP는 자체 모델 역할(`smol`/`slow`/`plan`) 설정으로 모델을 선택하고 `--model`을 유사 매칭합니다. Ouroboros는 범용 `default` 센티널을 생략합니다. 모델을 고정하려는 경우에만 명시적 모델을 전달하세요.

**OMP 안에서 `ooo ...`가 일반 채팅으로 모델에 전달됨**  
`ouroboros setup --runtime omp`를 실행한 뒤 OMP를 재시작하세요. `~/.omp/agent/extensions/ouroboros-ooo-bridge.ts`가 존재하는지, 디스패치 시간 제한 환경 변수 `OUROBOROS_OMP_BRIDGE_TIMEOUT_MS`가 사용할 수 없는 값으로 설정되지 않았는지 확인하세요.

## Active Conductor와 Synapse

OMP는 동일한 런타임 세션 핸들에서 Synapse의 `inform`/`after_turn` 신호 기능(백그라운드 응답 포함)을 선언합니다. 라이브 체크포인트 `redirect`나 강제 `replace`는 지원한다고 주장하지 않습니다.

실행 중에는 하나의 배타적 읽기 전용 관찰자가 현재 런타임/모델, 효율성 보증, 제한된 Discover 대상, 의존성/병렬 수준, 처음 예약된 AC, 주의 사항, 종료 보증을 전달합니다. 주 세션은 계속 사용할 수 있으며 ID를 사용자에게 묻는 대신 의미에 따라 관련 AC를 선택합니다. 정본 지시 언어는 영어이고 호스트가 현재 대화 언어에 맞게 UX를 자연스럽게 렌더링합니다.

네이티브 완료 또는 stdout EOF 뒤 런타임은 작업자가 종료하고 stderr를 닫을 때까지 5초를 부여합니다. 이 종료 단계가 멈추면 작업자를 종료하고 `TimeoutError`를 반환합니다. 실행 중인 stdout의 시작 및 유휴 시간 제한은 바뀌지 않습니다.
