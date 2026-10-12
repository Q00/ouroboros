# Zcode CLI 런타임

로컬에 설치한 Z.ai ZCode 코딩 에이전트를 사용해 Ouroboros 워크플로와 완성 모델 기반 작성 작업을 실행합니다. 런타임은 macOS 앱 번들의 `zcode.cjs` 진입 스크립트 또는 PATH에 있는 `zcode` 실행 파일을 지원합니다.

> 공급업체는 현재 안정적인 CLI 또는 JSON 출력 계약을 공개하지 않았습니다. 이 어댑터는 Zcode CLI 0.15.0 및 0.15.2에서 확인한 동작과 `tests/fixtures/zcode/`에 저장된 fixture에 맞춰져 있습니다. ZCode 앱을 업그레이드하면 호환성 변경으로 취급하고 업그레이드 후 Zcode 런타임 테스트를 다시 실행하세요.

## 사전 요구 사항

| 요구 사항 | 이유 |
|---|---|
| ZCode 데스크톱 앱 또는 `zcode` 실행 파일 | 코딩 에이전트 런타임을 제공합니다. |
| 설정된 Z.ai 공급자/모델 | Zcode는 자체 설정에서 공급자와 모델을 읽습니다. |
| 유효한 `model.main`이 있는 `~/.zcode/cli/config.json` | 값이 없으면 헤드리스 CLI가 `Model config is missing` 오류로 종료합니다. [모델 공급자 설정](#모델-공급자-설정)을 참고하세요. |
| 독립 실행 `.cjs` 경로에 호환되는 Node.js | 공식 앱 번들은 포함된 Electron/Node 런타임을 사용하고 독립 실행 스크립트는 시스템 Node를 사용합니다. |
| Ouroboros 기본 패키지 | 공급자 전용 Python 추가 패키지는 필요하지 않습니다. |

## 빠른 시작

```bash
ouroboros setup --runtime zcode
ouroboros run workflow seed.yaml --runtime zcode
```

`~/.zcode/cli/config.json`에 유효한 `model.main`이 없으면 setup이 경고하고 `ouroboros status health`도 경고를 표시합니다. 그렇지 않으면 Ouroboros가 CLI를 실행한 뒤에야 실패할 수 있습니다.

setup이 ZCode를 자동으로 찾지 못하면 다음 중 하나를 설정합니다.

```bash
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

```yaml
orchestrator:
  runtime_backend: zcode
  zcode_cli_path: /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

## CLI 경로 확인 순서

런타임은 다음 순서로 CLI를 확인합니다.

1. 생성자 인수 `cli_path=...`
2. `OUROBOROS_ZCODE_CLI_PATH`
3. `~/.ouroboros/config.yaml`의 `orchestrator.zcode_cli_path`
4. `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`
5. PATH의 `zcode`

공식 앱 번들에는 `runtime: electron-node`가 들어 있는 `.node-bundle-meta.json`이 있습니다. Ouroboros는 이 메타데이터를 읽고 `ZCode.app/Contents/MacOS/ZCode`를 실행하면서 `ELECTRON_RUN_AS_NODE=1`을 설정합니다. 이는 ZCode 자체 실행기와 동일한 방식입니다. `.app` 번들 내부 스크립트를 설정했는데 메타데이터, plist 또는 번들 실행 파일이 없거나 유효하지 않으면 실행을 거부합니다. 관련 없는 시스템 Node로 대체 실행하지 않습니다. 독립 `.cjs`, `.js`, `.mjs` 경로는 시스템 Node.js를 사용합니다. 다른 경로는 실행 가능한 wrapper 또는 바이너리로 보고 직접 호출합니다. 두 Node 실행 방식 모두에서 `NODE_OPTIONS`를 제거해 프로젝트나 상위 프로세스가 공급업체 CLI에 JavaScript를 미리 불러오지 못하게 합니다.

## 모델 공급자 설정

CLI 경로 외에도 Zcode CLI는 실행할 때마다 `~/.zcode/cli/config.json`에서 모델 공급자를 확인합니다. 여기에는 Ouroboros가 실행하는 헤드리스 `--prompt --json` 호출도 포함됩니다. 유효한 `model.main`이 없으면 CLI는 다음 메시지와 함께 즉시 종료합니다.

```text
Error: Model config is missing. Create /Users/<you>/.zcode/cli/config.json
with an explicit model provider before running ZCode.
```

`model.main`에는 반드시 `"provider-id/model-id"` **문자열**을 지정해야 합니다. 객체 형식 항목은 CLI의 설정 파서에서 조용히 버려지고, 같은 설정 누락 오류가 발생합니다. 참조한 공급자 ID는 같은 파일의 `provider` 레지스트리에 `kind`, `options.baseURL`, `options.apiKey` 항목이 있어야 합니다.

```json
{
  "model": { "main": "builtin:zai-coding-plan/GLM-5.3" },
  "provider": {
    "builtin:zai-coding-plan": {
      "name": "Z.ai - Coding Plan",
      "kind": "anthropic",
      "options": {
        "baseURL": "https://api.z.ai/api/anthropic",
        "apiKey": "<your key>",
        "apiKeyRequired": true
      },
      "enabled": true
    }
  }
}
```

데스크톱 앱의 `~/.zcode/v2/config.json`도 같은 `provider` 레지스트리 형식을 사용하므로 현재 활성화된 공급자 항목을 복사하면 CLI 설정을 가장 빠르게 맞출 수 있습니다. `~/.zcode/v2/credentials.json`의 OAuth 인증 정보는 데스크톱 앱과 CLI가 공유하지만 모델 참조는 공유하지 않습니다. 데스크톱 앱에서 모델을 골라도 `~/.zcode/cli/config.json`은 생성되지 않습니다.

`ouroboros setup --runtime zcode`는 이 파일에 유효한 `model.main`이 없으면 경고합니다. zcode 런타임과 LLM 백엔드의 `ouroboros status health` 항목도 같은 검사를 표시합니다.

## 런타임과 LLM 백엔드

Zcode는 에이전트 실행과 Ouroboros 완성 역할 모두를 구동할 수 있습니다. `ouroboros setup --runtime zcode`로 런타임을 별도로 설정하거나 다음과 같이 인터뷰, Seed 생성, 평가, QA에서 Zcode를 선택하세요.

```bash
export OUROBOROS_LLM_BACKEND=zcode
```

백엔드 플래그를 받는 명령은 `--llm-backend zcode`도 지원합니다. 완성 어댑터는 런타임과 동일하게 측정된 `--prompt --json` 요약 계약을 사용하고 최상위 `response`를 반환합니다. 결과를 승인하기 전에 요청된 `json_object` 또는 `json_schema` 출력이 유효한지 확인합니다.

Zcode가 설정에서 모델 선택을 관리하며 `--model` 플래그는 제공하지 않습니다. 따라서 신뢰 가능한 Zcode 출력이나 설정 원본이 확인해 주지 않으면 Ouroboros가 요청한 모델 이름은 실제 사용 모델로 보고되지 않습니다.

## 헤드리스 실행 계약

공식 macOS 앱 번들 스크립트에서는 작업마다 다음 명령 형식을 사용합니다.

```text
ELECTRON_RUN_AS_NODE=1 <ZCode.app/Contents/MacOS/ZCode> <zcode.cjs> \
  --json \
  --prompt <PROMPT> \
  --mode <edit|yolo> \
  [--cwd <PATH>] \
  [--resume <SESSION_ID>]
```

측정한 `--json` 동작은 턴이 끝날 때 출력되는 들여쓰기된 요약 객체 하나이며 NDJSON 이벤트 스트림이 아닙니다. 런타임은 stdout 전체를 모은 뒤 파싱하고 최상위 `response`를 최종 assistant 메시지 하나로 변환합니다. 최상위 `sessionId`는 재개 핸들이 됩니다.

## 실제 호환성 근거

공식 서명된 macOS ARM 패키지를 로컬 OpenAI 호환 모델 엔드포인트로 처음부터 끝까지 실행했습니다. 따라서 공급업체 프로세스, 스트리밍 모델 어댑터, JSON 요약, 저장된 세션, `--resume` 경로를 실제로 확인했습니다.

| ZCode 앱 | CLI | 결과 |
|---|---|---|
| 3.2.5 | 0.15.0 | 헤드리스 JSON 응답 및 동일 세션 재개 통과 |
| 3.3.5 | 0.15.2 | 헤드리스 JSON 응답 및 동일 세션 재개 통과 |

두 번들 모두 `runtime: electron-node`를 선언하고 Electron 41 / Node 24를 포함합니다. `zcode.cjs`를 Node 20으로 직접 실행하면 공급업체 코드의 `node:sqlite` 가져오기에서 실패합니다. 위 앱 번들 실행기를 선택하면 이 환경별 실패를 피할 수 있습니다.

어댑터는 `--non-interactive`, `--approval-mode`, `--model`을 전달하지 않습니다. Zcode 0.15.0 및 0.15.2에서 허용되지 않는 플래그입니다. `~/.zcode/cli/config.json`의 `model.main` 등을 사용해 Zcode 자체 설정에서 모델을 선택합니다.

## 권한 모드 대응

| Ouroboros 모드 | Zcode `--mode` | 동작 |
|---|---|---|
| `acceptEdits` | `edit` | 기본 비대화형 편집 모드 |
| `bypassPermissions` | `yolo` | 명시적인 전체 권한 우회 |
| `default` | `edit` | 안전한 비대화형 기본값으로 정규화 |

## 버퍼링 출력의 시간 초과 동작

Zcode는 최종 JSON 요약이 준비될 때까지 출력하지 않습니다. 따라서 기존 60초 첫 출력 감시기를 기본적으로 비활성화했습니다. 그대로 두면 전체 작업이 60초 제한에 걸리기 때문입니다. 출력이 없을 때의 시간 제한이 필요한 호출자는 `startup_output_timeout_seconds`를 명시적으로 전달할 수 있습니다.

이는 운영상 절충입니다. 바깥 제한 시간을 지정하지 않았는데 공급업체 프로세스가 요약을 출력하지 않으면 계속 대기할 수 있습니다. 운영 환경에서는 워크플로 수준의 제한 시간을 유지하거나 예상 작업 시간에 맞는 startup 출력 제한을 설정하세요.

## 기능과 한계

| 기능 | 상태 |
|---|---|
| 헤드리스 실행 | 가능. `--prompt --json` 사용 |
| 구조화된 최종 출력 | 가능. 턴마다 요약 객체 하나 |
| 중간 도구 이벤트 | 측정한 stdout에 없음 |
| 특정 세션 재개 | 가능. `--resume <sessionId>` 사용 |
| 호출별 모델 재정의 | 불가. Zcode가 자체 설정으로 모델을 선택 |
| LLM 완성 백엔드 | 가능. 버퍼링된 JSON 요약 어댑터 사용 |
| setup이 관리하는 지침 산출물 | 없음. 기능 가이드 렌더링이 대체 경로 |

## 문제 해결

**Zcode를 감지하지 못합니다.** `OUROBOROS_ZCODE_CLI_PATH`, `orchestrator.zcode_cli_path`를 설정하거나 `zcode` 실행 파일을 PATH에 추가합니다.

**헤드리스 실행이 `Model config is missing`으로 종료됩니다.** Zcode CLI가 `~/.zcode/cli/config.json`에서 확인할 수 있는 `model.main`이 없습니다. [모델 공급자 설정](#모델-공급자-설정)을 참고하세요. 주된 원인은 파일이 없거나 객체 형식 `model.main`을 지정한 경우입니다. CLI가 객체를 조용히 버리므로 참조는 반드시 `"provider-id/model-id"` 문자열이어야 합니다.

**독립 스크립트에서 `node:sqlite` 같은 Node 기본 모듈이 없다고 합니다.** Ouroboros가 번들 Electron/Node 런타임을 선택할 수 있도록 손상되지 않은 ZCode 앱 번들 경로를 사용하거나, 해당 Zcode 빌드와 호환되는 Node 버전을 설치하거나, 직접 실행할 수 있는 `zcode` wrapper를 설정합니다.

**모델 재정의가 적용되지 않습니다.** Zcode에는 호출별 `--model` 플래그가 없습니다. Zcode 자체 공급자/모델 설정에서 모델을 선택하세요.

**ZCode 업데이트 후 파싱이 깨집니다.** `tests/unit/orchestrator/test_zcode_cli_runtime.py`를 실행하고 파서 계약을 변경하기 전에 공급업체 요약 fixture를 다시 저장하세요.
