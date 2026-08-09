<!--
doc_metadata:
  runtime_scope: [codex]
-->

# Codex CLI로 Ouroboros 실행하기

> English: [codex.md](./codex.md)
>
> **번역 진행 상황**: 이 문서는 설치까지(시작하기 · 독립 설치 · Codex CLI 설치 · 플랫폼 · 설정 · 빠른 시작)를 옮긴 1부입니다. 그 뒤 절(Command Surface, How It Works, CLI 옵션, 문제 해결, 비용, Active Conductor)은 아직 영문입니다 — [codex.md](./codex.md)를 보세요. 진행은 [#1988](https://github.com/Q00/ouroboros/issues/1988)에서 추적합니다.

> 설치와 첫 실행 흐름 전체는 [Getting Started](../getting-started.md)(영문)를 보세요.

Ouroboros는 **OpenAI Codex**를 런타임 백엔드로 쓸 수 있습니다. [Codex CLI](https://github.com/openai/codex)가 어댑터와 실제로 대화하는 로컬 실행 표면입니다. macOS에서는 `PATH`에 없을 때 ChatGPT 앱에 딸려 오는 실행 파일도 setup이 찾아냅니다. Ouroboros 안에서 이 백엔드는 **세션 지향 런타임**으로 다뤄지며, 규약 우선 워크플로우 하네스(검수 기준, 평가 원칙, 결정적 종료 조건)는 동일하게 적용됩니다. 어댑터 자체는 로컬 `codex` 실행 파일과 통신할 뿐입니다. 기본값으로 Ouroboros는 **Codex가 지금 선택하고 있는 모델**을 그대로 쓰고, 역할별 추론 강도(reasoning effort)만 넘깁니다.

기본 `ouroboros-ai` 패키지 외에 추가 Python SDK는 필요 없습니다.

> **모델 권장**: Codex의 현재 기본 모델로 시작하세요. Ouroboros는 호출마다 역할별 추론 강도를 적용합니다. 특정 단계에 모델을 의도적으로 고정해야 할 때만 Ouroboros 설정에서 모델을 지정하세요.

## 시작하기 (권장 경로)

Codex 플러그인으로 시작하는 게 가장 짧습니다.

시작하기 전에 두 가지가 필요합니다. **`codex`가 `PATH`에 있어야 하고**(아래 명령을 셸에서 치기 때문입니다), 호스트에 **`uvx`가 있어야 합니다**(플러그인의 MCP 서술자가 `uvx`로 서버를 띄웁니다 — [`.mcp.codex.json`](../../.mcp.codex.json)). `uvx`가 없으면 다음 중 하나로 uv를 설치하세요:

```bash
pipx install uv
pip install --user uv
brew install uv          # macOS / Linuxbrew
```

**Python은 직접 깔지 않아도 됩니다.** `uvx`가 패키지의 `requires-python = ">=3.12"`를 보고 맞는 인터프리터를 확보합니다.

**터미널:**

```bash
codex plugin marketplace add Q00/ouroboros
codex plugin add ouroboros@ouroboros
```

새 Codex 세션을 열고 `ooo`를 입력하세요. setup이 아직 안 돌았다면, Ouroboros가 **무언가를 바꾸기 전에 먼저 런타임을 준비할지 물어봅니다.** 준비가 끝나면 Codex의 현재 기본 모델을 자동으로 씁니다. **직접 모델 설정하기**(Directly configure models)는 특정 파이프라인 단계의 모델을 고르거나 고정하고 싶을 때만 선택하세요 — Codex에서는 임시 `localhost` 주소로 브라우저에 로컬 설정 화면이 열립니다.

### 사전 조건 (권장 경로)

- **Codex CLI**가 설치돼 있고 **`PATH`에 있을 것.** 위 `codex plugin ...` 명령을 셸에서 실행하므로 이 경로에서는 `PATH` 등록이 필수입니다. macOS ChatGPT 앱에 딸린 실행 파일만 있고 `PATH`에는 없다면, 그 실행 파일을 `PATH`에 올리거나([설치 절차](#codex-cli-설치) 참고) 아래 [독립 설치](#독립-설치-플러그인-없이) 경로를 쓰세요. Ouroboros setup은 앱 번들 실행 파일을 찾아낼 수 있지만, **셸은 `codex plugin` 명령을 해석하지 못합니다.**
- 로그인된 **Codex CLI** 계정. API 키 인증도 됩니다: `printenv OPENAI_API_KEY | codex login --with-api-key`. 파일 기반 키 관리는 [`credentials.yaml`](../config-reference.md#credentialsyaml) 참고
- **`uvx`** (uv에 포함) — 위에서 설명한 대로 플러그인 MCP 서술자가 이걸로 서버를 띄웁니다

아래 독립 설치 경로의 `Python >= 3.12` 요구사항은 **이 경로에는 해당하지 않습니다.**

## 독립 설치 (플러그인 없이)

플러그인 없이 Codex CLI를 직접 붙이는 경로입니다. `codex`가 `PATH`에 없고 macOS ChatGPT 앱 번들만 있는 경우에도 이 경로를 씁니다 — setup은 번들 실행 파일을 찾아낼 수 있습니다.

### 사전 조건 (독립 설치 경로)

**setup을 돌리기 전에** 다음이 필요합니다:

- 로그인된 **Codex CLI** 계정 (위와 동일). `codex`가 `PATH`에 있으면 좋지만, 이 경로에서는 필수가 아닙니다
- **Python >= 3.12**
- **Ouroboros 설치**
- **MCP launcher** — 아래 셋 중 **하나**. `_codex_release_mcp_launcher()`가 이 순서로 찾고, 셋 다 없으면 `_register_codex_mcp_server()`가 중단합니다. 메시지는 `Could not find a launchable Ouroboros MCP command. Install uv, or install Ouroboros with the [mcp] extra, then rerun setup.` 인데, **화면에서는 `[mcp]`가 Rich 마크업으로 소비되어 `with the extra`로 보입니다** — 터미널 출력과 소스 문자열이 다릅니다:
  1. `uvx` (uv에 포함) ← 가장 간단
  2. `ouroboros mcp serve --help`가 성공하는 설치, 즉 **`[mcp]` extra 포함**
  3. `mcp` 패키지가 들어 있는 Python 환경

**터미널:**

```bash
# 1. Ouroboros 설치 (launcher 선택과 무관하게 항상 필요)
pip install ouroboros-ai

# 2. MCP launcher 확보 — 둘 중 하나
pipx install uv                          # uvx. brew install uv도 가능
pip install 'ouroboros-ai[mcp]'          # 또는 extra로 갈아끼우기 (1단계를 대체)

# 3. 연결
ouroboros setup --runtime codex
```

> 1단계와 2단계는 별개입니다. `pipx install uv`는 `uvx`만 설치하고 **`ouroboros` 실행 파일은 설치하지 않습니다.** `pip install 'ouroboros-ai[mcp]'`만이 우연히 둘 다 해결합니다. 다른 설치 방법(한 줄 설치, 소스)은 [Getting Started](../getting-started.md)(영문) 참고

> **`[mcp]` extra는 "필요 없음"이 아닙니다.** 기본 `ouroboros-ai` 패키지에 Codex CLI 런타임 어댑터는 들어 있지만, **MCP 등록에는 위 launcher 중 하나가 반드시 있어야 합니다.** `uvx`가 있으면 extra 없이도 되고, `uvx`가 없으면 `[mcp]` extra가 사실상 필수입니다. 설치 방법 전체(pip, 한 줄 설치, 소스)는 [Getting Started](../getting-started.md)(영문) 참고

## Codex CLI 설치

Codex CLI 자체는 npm 패키지로 배포됩니다. 전역으로 설치하세요.

```bash
npm install -g @openai/codex
```

설치를 확인합니다.

```bash
codex --version
```

다른 설치 방법과 셸 자동완성은 [Codex CLI README](https://github.com/openai/codex#readme)를 보세요.

## 플랫폼

| 플랫폼 | 상태 | 비고 |
|----------|--------|-------|
| macOS (ARM/Intel) | 지원 | 주 개발 플랫폼 |
| Linux (x86_64/ARM64) | 지원 | Ubuntu 22.04+, Debian 12+, Fedora 38+ 에서 검증 |
| Windows (WSL 2) | 지원 | Windows 사용자에게 권장하는 경로 |
| Windows (네이티브) | 실험적 | WSL 2를 강력히 권장합니다. 네이티브 Windows에서는 경로 처리와 프로세스 관리에 문제가 있을 수 있습니다. **Codex CLI 자체가 네이티브 Windows를 지원하지 않습니다.** |

## 설정

런타임 백엔드로 Codex CLI를 고르려면 설정에 이렇게 씁니다.

```yaml
orchestrator:
  runtime_backend: codex
```

명령줄에서 넘길 수도 있습니다.

```bash
uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

### Codex 사용자는 무엇을 어디에 설정하나

**Ouroboros 런타임 설정은 `~/.ouroboros/config.yaml`입니다.** 평소 모델 선택은 `ouroboros config` 또는 `ouroboros config --web`으로 여세요. 둘 다 같은 설정 화면입니다.

**Use Codex default model**을 고르면 Codex의 현재 기본 모델을 그대로 씁니다. 이게 권장 설정입니다 — Ouroboros가 Codex 호출마다 역할별 추론 강도만 넘기므로, Codex App이나 CLI에서 새 모델로 바꾸면 자동으로 그게 쓰입니다. 목록에서 모델을 고르거나 **Enter another model ID…**를 쓰는 건, 특정 단계(Execute 포함)에 모델을 의도적으로 고정하고 싶을 때만 하세요.

**Codex의 MCP/env 연결과 사용자가 직접 관리하는 네이티브 Codex 프로필은 `$CODEX_HOME/config.toml`입니다.** `CODEX_HOME`이 설정돼 있지 않으면 Codex는 `~/.codex/config.toml`을 씁니다.

Codex 기반 Ouroboros 역할들이 Codex CLI의 활성 기본값/프로필을 물려받는 대신 명시적 모델을 쓰게 하려면, `config.yaml` 키를 직접 지정하세요.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex   # codex가 이미 PATH에 있으면 생략

llm:
  backend: codex
  qa_model: gpt-5.4

clarification:
  default_model: gpt-5.4

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
  # 선택: 단순 투표 로스터도 여기에 `consensus.models`로 둡니다
```

이 키들을 기본값 그대로 두면, Codex setup이 프로바이더 중립적인 `llm_profiles`와 `llm_role_profiles` 매핑을 추가합니다. 그 Codex 매핑은 **모델이나 생성된 Codex 프로필을 고르지 않고** 호출별 추론 강도만 정합니다(fast: low, standard: medium, deep: high, frontier: xhigh). `config.yaml`에 명시한 모델 값이 있으면 그쪽이 이깁니다.

> **주의**: `~/.codex/config.toml`은 Ouroboros 단계별 모델을 고정하는 자리가 **아닙니다.** 설정 화면이나 `~/.ouroboros/config.yaml`의 해당 값을 쓰세요. 명시적 `--profile`이 필요하면 사용자가 관리하는 네이티브 Codex 프로필을 그대로 두면 됩니다.
>
> 오래 떠 있는 URL 기반 Ouroboros MCP 서버를 운영한다면 그 URL 항목을 `~/.codex/config.toml`에 두세요. `ouroboros setup --runtime codex`는 **기본적으로 그 항목을 보존합니다.** setup이 그 항목을 관리형 command-spawned 서버로 **교체하기를 의도적으로 원할 때만** `--mcp-mode stdio`를 쓰세요.

## 빠른 시작

> 첫 실행 흐름 전체(인터뷰 → seed → 실행)는 **[Getting Started](../getting-started.md)**(영문)를 보세요.

### 설치 확인

```bash
ouroboros --help
codex --version
```

> `codex --version`이 `command not found`로 나와도 **독립 설치 경로에서는 고장이 아닙니다.** 이 경로는 macOS ChatGPT 앱 번들 실행 파일만 있고 `PATH`에는 없는 사용자를 지원하며, setup은 그 번들을 찾아냅니다. 그 경우 setup이 기록한 경로로 확인하세요:
>
> ```bash
> ouroboros config show
> ```
>
> 출력의 **`CLI path:`** 줄이 setup이 실제로 해석한 실행 파일입니다(`cli/commands/config.py:696-701`). `codex_cli_path`라는 문자열은 출력에 나오지 않으니 그걸로 grep하지 마세요.
>
> `codex`를 `PATH`에 올린 경우와 권장(플러그인) 경로에서는 `codex --version`이 정상 확인 방법입니다.

---

## 이후 절

아래 내용은 아직 영문입니다. [codex.md](./codex.md)에서 이어 보세요.

- Command Surface (`ouroboros setup --runtime codex`가 실제로 건드리는 것 11가지, 워커 서브프로세스 격리, `ooo` 스킬 가용성)
- How It Works (실행 파일 버전 증명 포함)
- Codex CLI의 강점 / 런타임 차이
- CLI 옵션 / Seed 파일 레퍼런스
- 문제 해결 (Codex CLI를 못 찾을 때, 인증 오류, 헬스체크 경고, EventStore 미초기화)
- 비용
- Active Conductor와 Synapse
