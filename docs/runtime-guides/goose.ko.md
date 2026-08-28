# Goose로 Ouroboros 실행하기

> English: [goose.md](./goose.md)
>
> 영문 원문 전체를 옮긴 완역본입니다. 원문이 갱신되면 이 문서도 함께 갱신해야 합니다.

Ouroboros는 `runtime_backend: goose`를 설정하면 Goose CLI를 통해 오케스트레이터 워커를 실행할 수 있습니다.

Goose 런타임은 Goose가 문서화한 헤드리스 작업 인터페이스를 사용합니다:

```bash
goose run --output-format stream-json -i -
```

Ouroboros는 조합한 워커 프롬프트를 표준 입력으로 쓰고, `stream-json` 이벤트를 읽은 다음, 오케스트레이터가 사용하는 공통 런타임 메시지 모델로 정규화합니다.

## 설정

런타임 백엔드와 선택적 Goose CLI 경로를 설정합니다:

```yaml
orchestrator:
  runtime_backend: goose
  goose_cli_path: /path/to/goose
```

인터뷰, 평가, 합의, Seed 생성처럼 LLM 호출만 필요한 작업도 Goose를 통해 라우팅하려면 다음을 설정합니다:

```yaml
llm:
  backend: goose
```

환경변수를 사용할 수도 있습니다:

```bash
OUROBOROS_GOOSE_CLI_PATH=/path/to/goose
```

경로를 설정하지 않으면 Ouroboros가 실행 시점에 `PATH`에서 `goose`를 찾습니다.

## Setup

`goose`가 `PATH`에 있다면 다음 명령으로 Ouroboros를 설정합니다:

```bash
ouroboros setup --runtime goose
```

기존 설정의 백엔드만 바꾸려면 다음을 사용합니다:

```bash
ouroboros config backend goose
```

## MCP 서버

MCP 서버는 Goose를 오케스트레이터 런타임과 LLM 백엔드로 모두 받을 수 있습니다:

```bash
ouroboros mcp serve --runtime goose --llm-backend goose
```

## 중첩된 Goose/Ouroboros 실행 안전장치

Ouroboros가 Goose를 자식 런타임으로 실행하면 `_OUROBOROS_NESTED=1`을 설정하고, 자식 프로세스에서 Ouroboros 런타임 선택 관련 환경변수를 제거합니다. 부모 세션 자체가 Goose 안에서 실행 중일 때 `Ouroboros → Goose → Ouroboros` 식의 재귀가 실수로 발생하는 것을 막기 위한 동작입니다.

가장 안정적으로 사용하려면, 중첩 오케스트레이션을 의도한 경우가 아니라면 자식 Goose 세션이 Ouroboros MCP 확장을 자동으로 불러오지 않게 하세요.

## 능력

현재 런타임은 다음 능력을 선언합니다:

- `skill_dispatch=True`
- `targeted_resume=True`
- `structured_output=True`

여기서 `targeted_resume`은 이전 호출에서 사용한 Goose 세션을 지정해 재개할 수 있다는 뜻이고, `structured_output`은 이벤트 스트림을 구조화된 런타임 메시지로 변환할 수 있다는 뜻입니다.

재개는 Ouroboros가 생성한 안정적인 Goose 세션 이름을 사용합니다. 이후 호출에서 이 이름을 `goose run -n <name> --resume`으로 넘겨 세션을 재개합니다.

## 실험적 상태와 제한 사항

Goose 지원은 실험적인 CLI 백엔드로 구현되어 있습니다. Claude Code나 Codex CLI 백엔드와 완전히 같은 수준이라고 주장하지 않습니다:

- 도구 제한은 프롬프트 안내와 사후 이벤트 감사로 느슨하게 적용됩니다. Goose CLI는 현재 동일한 수준의 강제 도구 허용 목록이나 샌드박스 표면을 제공하지 않습니다.
- 구조화된 완료 출력은 협력 방식으로 적용됩니다. Ouroboros가 엄격한 JSON/schema 지시문을 주입하고 Goose 출력에서 유효한 JSON을 추출하지만, Goose CLI에는 Codex의 `--output-schema`와 같은 강제 플래그가 없습니다.
- 런타임 재개는 Ouroboros가 생성한 Goose 세션 이름을 사용합니다. 따라서 Ouroboros 워커에 결정적인 지정 재개(targeted resume)를 제공하지만, Goose CLI의 세션 이름 동작에 의존합니다.
- 중첩된 Ouroboros-in-Goose 재귀는 환경변수 정리와 `_OUROBOROS_NESTED=1`로 방어합니다. 그래도 중첩 오케스트레이션을 의도한 경우가 아니라면 자식 Goose 세션에 Ouroboros MCP 확장을 자동으로 로드하지 않는 것이 좋습니다.

## 참고 사항

이 런타임은 Goose CLI의 `run` 명령과 `--output-format stream-json` 지원에 의존합니다. Goose Desktop에 포함된 `goosed` 서버 바이너리는 여기서 사용하는 `goose` CLI와 같은 인터페이스가 아닙니다.

## LLM 백엔드 지원

`llm.backend: goose`는 완료형 호출에 `goose run --output-format stream-json --no-session -i -`를 사용합니다. 런타임 백엔드와 같은 `goose_cli_path`/`OUROBOROS_GOOSE_CLI_PATH` 경로 해석을 공유합니다.

구조화된 `response_format` 요청은 협력 방식으로 적용됩니다. Ouroboros가 엄격한 JSON/schema 지시문을 주입하고 Goose 출력에서 JSON을 추출합니다. Goose CLI에는 현재 Codex의 `--output-schema`와 같은 강제 플래그가 없으므로, 형식이 잘못된 구조화 응답은 재시도된 뒤 프로바이더 오류로 표면화될 수 있습니다.

## Active Conductor와 Synapse

Goose CLI는 안정적인 하나의 Ouroboros 생성 세션 이름과 명시적 재개를 사용하는 검증된 Synapse `inform`/`after_turn` 백엔드입니다. 라이브 체크포인트 `redirect`나 하드 `replace`는 선언하지 않습니다.

배타적인 읽기 전용 관찰자 하나가 현재 런타임/모델, 효율·절약 보증, 범위가 정해진 Discover 대상, 의존성/병렬 수준, 처음 스케줄된 AC, 중요한 경로 변경, 주의 사항, 종료 시 보증을 보고합니다. 메인 세션은 계속 대화 가능한 상태로 남고, 사용자 의도를 관련 AC에 매핑할 때 ID를 노출하지 않습니다. 정본 안내는 영어이며, 사용자에게 보여 주는 표현은 현재 대화 언어를 따릅니다.

설치·실행 세부사항은 Goose CLI의 현재 문서와 함께 확인하세요.
