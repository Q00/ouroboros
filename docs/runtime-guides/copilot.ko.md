# GitHub Copilot CLI 런타임

로컬에 설치된 [GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) 위에서 Ouroboros 워크플로를 돌립니다.

> English: [copilot.md](./copilot.md)
>
> 영문 원문 전체를 옮긴 완역본입니다. 원문이 갱신되면 이 문서도 함께 갱신해야 합니다.

Copilot 런타임은 Codex · Gemini · Hermes · OpenCode · Kiro 런타임의 형제입니다. Ouroboros가 오케스트레이션 루프를 소유하고, 호스팅 SDK와 대화하는 대신 작업마다 `copilot -p`를 셸로 호출합니다. 인증은 기존 `gh auth` 세션을 타므로 **따로 관리할 API 키가 없습니다.**

> **이 런타임만 다른 점**: Copilot은 Ouroboros 백엔드 중 유일하게 **모델 목록을 실시간으로 발견합니다.** `ouroboros setup --runtime copilot`이 setup 시점에 GitHub Copilot models API를 조회해서, 하드코딩된 모델 ID를 외우게 하는 대신 **지금 당신 구독이 실제로 허용하는 것** 중에서 기본값을 고르게 합니다. GitHub가 새 모델을 내놓는 순간부터 쓸 수 있고, 갱신하려면 setup을 다시 돌리면 됩니다.

## 사전 조건

| 요구사항 | 이유 |
|------------------|--------------------------------------------------------------------|
| `copilot` CLI | 공급자 — [Copilot CLI 설치 가이드](https://docs.github.com/copilot/concepts/agents/about-copilot-cli)를 따르세요 |
| `gh` CLI | 실시간 Copilot 모델 목록 발견에 사용 (`gh auth token`) |
| GitHub 인증 | 첫 사용 전에 `gh auth login` 한 번 |
| Ouroboros (mcp) | `pipx install 'ouroboros-ai[mcp]'` 또는 `uv tool install 'ouroboros-ai[mcp]'` |

> Copilot은 **기본** Ouroboros 패키지에 `[mcp]` extra만 얹어서 돕니다. `[claude]` extra는 필요 없고, MCP 항목은 `ouroboros-ai[mcp]`로 등록됩니다. **호스트 등록에는 패키지가 격리된 `uvx` 또는 `pipx run` launcher가 필요합니다.** 평범한 `pip install`은 이미 격리된 환경에 끼워 넣을 때는 적합하지만, 그것만으로는 이 호스트 launcher 요구사항을 만족하지 못합니다. 둘 다 없으면 setup은 fail-closed로 종료합니다.

## 빠른 시작

```bash
# 1. Copilot CLI 설치 후 인증 (한 번)
gh auth login                            # gh auth token 접근 권한이 생깁니다

# 2. MCP extra와 함께 Ouroboros 설치
pipx install 'ouroboros-ai[mcp]'         # 또는: uv tool install 'ouroboros-ai[mcp]'

# 3. Ouroboros를 Copilot에 연결
ouroboros setup --runtime copilot
#   - PATH에서 copilot 자동 탐지 (또는 OUROBOROS_COPILOT_CLI_PATH 존중)
#   - gh 토큰으로 https://api.githubcopilot.com/models 호출
#   - 실시간 모델 목록을 출력하고 기본값을 고르게 함
#   - ~/.ouroboros/config.yaml + ~/.copilot/mcp-config.json 작성
#   - Ouroboros 실행용 ~/.copilot/ouroboros-instructions/AGENTS.md 설치

# 4. Copilot 세션을 재시작한 뒤 ooo 스킬 사용
copilot
> ooo interview Add a CLI flag to skip eval
```

## CLI 경로 해석

런타임은 다음 순서로 바이너리를 찾습니다:

1. 생성자 인자 `cli_path=...`
2. `OUROBOROS_COPILOT_CLI_PATH` 환경변수
3. `~/.ouroboros/config.yaml`의 `orchestrator.copilot_cli_path`
4. `$PATH`의 `copilot`

덕분에 `$PATH` 밖에 설치되는 경우(예: Windows에서 winget이나 scoop 설치)에도 셸 초기화 파일을 고치지 않고 동작합니다.

Ouroboros가 띄우는 Copilot 자식 세션은 setup이 소유하는 `~/.copilot/ouroboros-instructions` 디렉터리를 Copilot CLI의 콤마 구분 목록 형식으로 `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`에 덧붙입니다. **기존 커스텀 지시 디렉터리는 보존됩니다.**

## 실시간 모델 발견

`ouroboros setup --runtime copilot`은 마법사 시작 시 **항상** 실시간 모델 목록을 조회합니다. 흐름:

1. `GH_TOKEN` → `GITHUB_TOKEN` → `COPILOT_TOKEN` → `gh auth token` 순서로 토큰 해석
2. 그 토큰으로 `GET https://api.githubcopilot.com/models`
3. `data[].id`와 `capabilities.family`를 타입 있는 목록으로 파싱
4. setup 실행이 끝날 때까지 프로세스 안에 캐시
5. 위 중 무엇이든 실패하면(`gh` 없음, 네트워크 다운, 레이트 리밋, 파싱 오류) 경고를 출력하고 잘 알려진 ID 번들 스냅숏으로 폴백해서 setup이 끝까지 진행됩니다

setup은 고른 기본 모델을 출력하고, `~/.ouroboros/config.yaml`의 지원되는 모델 필드들에 반영합니다. 예를 들어 `clarification.default_model`, `llm.qa_model`, 평가/복원력 모델 필드, 그리고 해당 필드가 비어 있거나 아직 Ouroboros 기본값 상태일 때의 consensus 모델 기본값입니다. **설정 계약에 `llm.default_model` 키는 없습니다.** GitHub가 새 모델을 내놓은 뒤 새 기본값을 고르고 싶으면 `ouroboros setup --runtime copilot`을 언제든 다시 돌리세요.

### 하이픈 표기와 점 표기 모델 ID

Ouroboros 기본값은 Anthropic SDK 형식(`claude-opus-5`, `claude-sonnet-4-6`)을 씁니다. Copilot CLI는 catalog 형식(`claude-opus-5`, `claude-sonnet-4.6`)을 기대합니다. 어댑터는 임의의 모델 이름을 바꾸지 않고 발견된 Copilot catalog를 기준으로 이 형식들을 해석합니다.

`map_to_copilot_model()`([`copilot/model_discovery.py`](../../src/ouroboros/copilot/model_discovery.py))은 명시적인 점 표기 Copilot ID를 그대로 통과시키고, 알려진 `openrouter/anthropic/` 접두사 제거, 기존의 정확한 정적 별칭, 또는 마지막 숫자 버전 구분자만 점으로 바꾸는 방식으로 후보를 만듭니다. 예를 들어 `claude-opus-4-8`은 `claude-opus-4.8` 후보가 됩니다. `claude-opus` 안의 하이픈은 건드리지 않습니다. 접두사를 제거했거나 정적 매핑으로 만든 값을 포함한 모든 변환 후보는 발견된 catalog나 번들 catalog에 정확히 같은 ID가 있을 때만 반환됩니다.

따라서 현재 `DEFAULT_OPUS_MODEL`인 `claude-opus-5`와 `openrouter/anthropic/claude-opus-5`는 모두 catalog에 공개된 `claude-opus-5`로 해석됩니다. 앞으로 나올 Anthropic 버전도 정적 맵을 더하지 않고 같은 catalog 확인 규칙을 씁니다. 알 수 없는 모델이나 현재 catalog에 없는 변환 후보는 OpenRouter 접두사를 포함한 원래 ID를 보존합니다. 다른 모델을 조용히 고르는 대신 기존 Copilot unavailable-model 오류가 명확히 드러나게 하기 위해서입니다.

Copilot이 모르는 모델을 지정하면 서브프로세스가 `Model "<id>" from --model flag is not available.`로 실패합니다. 발견된 목록에 있는 모델을 넘기거나 setup을 다시 돌려 갱신하세요.

## 설정

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: copilot
  copilot_cli_path: C:\Users\you\AppData\Local\Programs\copilot\copilot.exe   # 선택
llm:
  backend: copilot
  qa_model: claude-opus-4.6                     # setup이 씁니다
clarification:
  default_model: claude-opus-4.6                # setup이 씁니다
```

> `llm`에는 `default_model` 필드가 없습니다(`config/models.py`의 `LLMConfig`). setup은 발견한 모델을 **실제로 존재하는** 필드들에 씁니다. `clarification.default_model`, `llm.qa_model`, 평가/복원력 모델 필드 같은 것들입니다.

백엔드 이름을 받는 모든 CLI 표면이 같은 `copilot` 값을 받습니다:

- `ouroboros setup --runtime copilot`
- `ouroboros config backend copilot`
- `ouroboros mcp serve --runtime copilot --llm-backend copilot`
- `ouroboros init --llm-backend copilot`

## 헤드리스 계약

작업마다 비대화형 Copilot 프롬프트를 하나씩 띄웁니다:

```text
copilot --no-color --log-level none \
        --add-dir <CWD> \
        --available-tools=<TOOLS> --allow-tool=<TOOLS> \
        [--model <DOTTED_ID> | --agent <NAME>] \
        -p <PROMPT>
```

| 플래그 | 이유 |
|---------------------|--------------------------------------------------------------|
| `--no-color` | 안정적인 JSONL 파싱 |
| `--log-level none` | 이벤트가 아닌 로그 줄 억제 |
| `--add-dir` | 샌드박스 쓰기 경계. Ouroboros가 넘긴 CWD에 고정 |
| `--available-tools` | 강한 도구 봉투(allowlist) — 밖에 있는 것은 모델에게 보이지 않음 |
| `--allow-tool` | 호출마다 확인 프롬프트 건너뛰기 (`-p`에 필수) |
| `--model` | 작업별 모델 오버라이드 (하이픈 형식에서 자동 변환) |
| `--agent` | 커스텀 에이전트 프로파일. `--model`보다 우선 |
| `-p` | 원샷 프롬프트 (대화형 REPL 없음) |

이 명령을 띄우기 전에 런타임은 **Codex와 같은 실행 파일 버전 증명 정책**을 적용합니다. 초기화 시점 프로브 실패는 긍정적 기준선이 없으므로 런타임을 차단합니다. 이후의 타임아웃이나 실행 실패는 현재 시도만 차단하고 **실행 파일 변조로 보고하지 않습니다.** 버전 출력 변화는 성공한 증명 두 개가 서로 달라야만 인정됩니다.

런타임은 `copilot --version`을 돌리기 **전에** 실행하지 않는 방식의 경로·콘텐츠·device/inode·심링크 증거가 초기화 때와 다르면 거부하고, 시작된 프로브를 전부 사후 샘플링해서 **변조가 동시 발생한 타임아웃·실행 실패보다 우선**하게 합니다.

상위 디렉터리의 generation만 바뀐 경우, 증거만으로는 무관한 형제 항목의 변동과 실행 파일 항목의 교체 후 복원을 구분할 수 없습니다. 그 시도는 실행 파일 변조가 확인됐다고 주장하지 않고 **재시도 가능한 불확정 권위**로 fail-closed 처리됩니다. 경로·콘텐츠·심링크·device/inode·프로브 구간 generation 변조는 두 번째 성공 버전 프로브 전에 fail-closed될 수 있습니다. 이로써 `copilot --version` 결과가 두 번 없는 상태가 **부하 중에 실행을 승인하는 일은 결코 없습니다.**

### MCP 등록

`ouroboros setup --runtime copilot`은 마법사가 감지한 설치 방식을 가리키는 항목을 `~/.copilot/mcp-config.json`에 씁니다:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
      "env": {
        "OUROBOROS_AGENT_RUNTIME": "copilot",
        "OUROBOROS_LLM_BACKEND": "copilot"
      }
    }
  }
}
```

`uvx`가 없으면 setup은 동등한 `pipx` 항목을 씁니다:

```json
{
  "command": "pipx",
  "args": ["run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
}
```

전역 바이너리 직접 실행이나 `python -m` 폴백은 **절대 등록하지 않습니다.** 그런 환경은 MCP 2를 보장할 수 없기 때문입니다. 마법사는 멱등적이고, setup이 관리하는 항목을 현재의 격리 launcher로 갱신합니다.

> **재시작 필요**: Copilot CLI는 세션 시작 시점에 MCP 자식을 바인딩합니다. 처음 등록한 뒤(또는 항목을 바꾼 뒤)에는 `copilot` 세션을 닫았다 다시 여세요. 그래야 새 MCP 서버가 뜹니다.

## 능력

| 능력 | 상태 |
|-------------------------|--------------------------------------------------------|
| 헤드리스 실행 | O |
| 도구 봉투 | O (`--available-tools` allowlist + `--allow-tool`) |
| 샌드박스 경계 | O (`--add-dir <CWD>`) |
| 실시간 모델 발견 | O (**이걸 하는 유일한 런타임**) |
| 에이전트 프로파일 선택 | O (`runtime_profile` 매핑에서 `--agent`) |
| 재귀 가드 | O (`_OUROBOROS_DEPTH`. Claude/Codex와 동일) |
| 응답 절단 | O (`InputValidator` 경유) |
| 구조화 출력 플래그 | X (`--output-schema` 미지원. 프롬프트 지시문 + 사후 JSON 추출로 대체. Gemini와 같은 우회) |
| 세션 재개 | X (Copilot CLI에 resume API가 없음. 체크포인팅은 Ouroboros lineage 계층에서 일어남) |

## 문제 해결

**`Model "claude-opus-4-6" from --model flag is not available.`**
하이픈 ID를 점 표기 Copilot 형식으로 자동 변환하지 못하는 옛 Ouroboros 빌드입니다. 모델 발견 모듈이 포함된 릴리스로 올리거나, `ouroboros setup --runtime copilot`을 다시 돌려 실시간 목록에서 점 표기 ID를 고르세요. **`OUROBOROS_DEFAULT_MODEL` 환경변수는 없습니다.** 역할별 오버라이드는 각자의 변수를 씁니다(예: `OUROBOROS_CLARIFICATION_MODEL`).

**`copilot CLI not found.`**
GitHub 문서를 따라 Copilot CLI를 설치한 뒤, setup이 자동 탐지하게 두거나 `OUROBOROS_COPILOT_CLI_PATH=/abs/path/to/copilot`을 설정하세요.

**`MCP dependencies not installed: mcp package not installed.`**
격리된 MCP launcher가 없거나 `[mcp]` extra를 로드하지 못했습니다. `pipx install 'ouroboros-ai[mcp]'` 또는 `uv tool install 'ouroboros-ai[mcp]'`로 설치하세요. 로컬 개발 설치라면 `uv tool install --with mcp --from . ouroboros-ai`를 쓰세요.

**`ouroboros-ouroboros_*` 도구가 `Error: Not connected`를 반환.**
MCP 자식이 죽었거나 종료됐습니다. `~/.copilot/logs/<session>/...`에서 spawn 오류를 확인하고 고친 뒤(대개 위의 `[mcp]` extra 누락), **Copilot 세션을 재시작하세요.** CLI는 세션 도중에 죽은 MCP 자식을 자동으로 다시 연결하지 않습니다.

**setup 중 `Could not reach the GitHub Copilot models API`.**
setup은 번들 모델 스냅숏으로 폴백해서 마법사를 끝낼 수 있게 합니다. `gh auth login`을 실행하거나 `GH_TOKEN`/`GITHUB_TOKEN`을 설정한 뒤, `ouroboros setup --runtime copilot`을 다시 돌려 실시간 목록으로 갱신하세요.

**최종 응답이 비어 있음.**
Copilot 어댑터는 JSONL 이벤트 스트림에서 어시스턴트 답변을 재구성합니다. 도구 호출이 허용된 턴 예산을 소진하면 답변이 빌 수 있습니다. `--max-turns`(또는 대응하는 설정 필드)를 올리고 다시 돌리세요.
