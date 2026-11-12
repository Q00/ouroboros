# Megacode Harness 런타임

[megacode harness](https://megacode-harness.vercel.app/docs/)(`harness` 바이너리) 안에서
Ouroboros를 실행하고, Ouroboros 자체 모델 호출과 작업자도 호스팅된 `mega:*` 모델을
포함해 harness에서 실행합니다.

설정은 다음 양방향을 모두 연결합니다.

- **Harness → Ouroboros.** Harness 세션에 Ouroboros 스킬, Ouroboros MCP 서버,
  `AGENTS.md`의 `ooo` 라우팅 표를 제공합니다. Harness에는 훅이 없으므로 라우팅 표가
  키워드 훅을 대신합니다.
- **Ouroboros → harness.** `megacode` 백엔드는 각 작업자 작업과 LLM 완성을 각각
  헤드리스 `harness -p` 턴으로 실행합니다. 따라서 작성, 평가, 실행에 harness 세션의
  모델과 로그인을 사용합니다.

## 사전 요구 사항

| 요구 사항 | 용도 |
|---|---|
| `harness` CLI | Harness 문서에서 설치하고 `harness`를 `PATH`에 둡니다. |
| Harness 로그인 | 호스팅된 `mega:*` 모델을 쓰려면 `harness login`을 사용하거나 provider 키를 사용합니다. |
| `uv` | Ouroboros MCP 서버를 실행합니다(`uvx`). |

## 빠른 시작

```bash
ouroboros setup --runtime megacode
harness            # 실행 후 입력: ooo interview build me a small CLI
```

설정은 harness 설정 폴더(`harness paths` → `CONFIG`) 안에만 기록합니다.
`~/.ouroboros/config.yaml`은 변경하지 않으므로 다른 호스트의 런타임은 유지됩니다.
설정은 네이티브 `CONFIG` 출력이 불완전하거나 모호하면 어떤 파일도 쓰기 전에 중단합니다.
Harness 0.23은 제어 문자를 이스케이프로 표시하는데, 이 표기는 경로 이름에 문자 그대로
존재할 수도 있습니다(예: `\x09`, `\r`). 따라서 이 출력만으로는 디렉터리를 안전하게
선택할 수 없습니다. 공백, 이모지, 유니코드 줄 구분자와 문단 구분자는 계속 지원합니다.
Ouroboros CLI에서도 백엔드를 사용하려면 실행마다 선택하세요.

```bash
ouroboros run workflow seed.yaml --runtime megacode
OUROBOROS_LLM_BACKEND=megacode ouroboros run workflow seed.yaml --runtime megacode
```

## 설정이 기록하는 항목

| 경로 (`CONFIG` 아래) | 내용 |
|---|---|
| `config.toml` | 하나의 관리 블록: `[skills] extra_roots`, `[modes.ouroboros-worker]`, `[modes.ouroboros-llm]`, `[mcp.servers.ouroboros]` |
| `ouroboros/skills/` | 패키지에 포함된 스킬의 복사본. 각 폴더 이름은 frontmatter의 `name`과 같습니다(harness는 폴더와 `name`이 다른 스킬을 건너뜁니다). |
| `AGENTS.md` | `ooo <command>`를 스킬에 연결하는 하나의 관리 블록 |

관리 블록 밖의 텍스트는 변경하지 않으며, 설정을 다시 실행하면 해당 위치의 블록을
교체합니다. 사용자의 `config.toml`에 이미 `[skills]`, 두 모드 중 하나, 또는
`[mcp.servers.ouroboros]`가 정의돼 있으면 설정은 중복으로 기록하는 대신 중단하고 해당
테이블을 알려줍니다. Ouroboros를 업그레이드한 뒤 설정을 다시 실행해 복사된 스킬을
갱신하세요.

MCP 서버는 `sandbox = false`로 실행됩니다. 서버는 `uvx`로 스스로 설치하고,
`~/.ouroboros` 아래에 상태를 쓰며, 자체 모델 호출을 위해 `harness -p`를 실행합니다.

## 헤드리스 실행 계약

| | 작업자 (`MegacodeRuntime`) | LLM 완성 (`MegacodeLLMAdapter`) |
|---|---|---|
| 명령 | `harness -p --output-format stream-json --mode ouroboros-worker` | `harness -p --output-format stream-json --mode ouroboros-llm --max-iterations N` |
| 프롬프트 | stdin | stdin |
| 도구 | read, write, edit, bash, glob, search, exec | read, glob, search |
| 답변 | `result` 레코드 | `result` 레코드 |

- 두 모드 모두 `ask = []`로 설정합니다. 헤드리스 턴은 누구에게도 질문할 수 없으며,
  어느 모드에도 `mcp`나 `skill` 도구를 제공하지 않으므로 작업자가 Ouroboros에 다시
  진입할 수 없습니다.
- `--model`은 `mega:full` 같은 명시적 ID에만 전달합니다. 그렇지 않으면 harness의
  기본값을 사용합니다.
- `permission_mode: bypassPermissions`는 `--sandbox off`를 추가합니다. 그 외의 값은
  harness에 설정된 샌드박스를 유지합니다(기본적으로 켜져 있으며 네트워크 없음).
- 실행된 명령이 0이 아닌 코드로 종료돼도 harness는 `status: ok`와 `output.code`를
  보고합니다. 런타임은 0이 아닌 `code`를 실패한 도구 호출로 처리합니다.

## 제한 사항

- **재개 불가.** 헤드리스 harness 실행은 세션을 이어갈 수 없습니다(`resume -p`는
  문서화된 harness 제한). 따라서 백엔드는 특정 세션 재개를 지원한다고 선언하지 않으며,
  재시도마다 새 세션을 시작합니다.
- **중첩 호출마다 MCP 시작.** 각 `harness -p`는 `mcp` 도구가 없는 모드에서도
  Ouroboros 자체 서버를 포함해 harness 설정의 MCP 서버를 시작합니다. 따라서 매 작업자와
  LLM 호출에 서버 시작 시간이 추가됩니다.
- **실행 파일 증명 없음.** Megacode CLI는 아직 Codex 계열 런타임이 사용하는 CLI 실행 파일
  신원 증명 대상에 포함되지 않습니다.

## CLI 경로 확인 순서

1. `OUROBOROS_MEGACODE_CLI_PATH` (프로젝트 `.env`에서 온 값은 무시)
2. `~/.ouroboros/config.yaml`의 `orchestrator.megacode_cli_path`
3. `PATH`의 `harness`

설정, 모델 검색, 작업자, LLM 완성은 명시적 팩토리 및 생성자 경로를 포함해 이 규칙을
공유합니다. 선택한 값은 실행 가능한 절대 경로(`~/bin/harness`는 운영자의 홈을 기준으로
확장) 또는 `harness` 같은 디렉터리 없는 실행 파일 이름이어야 합니다. 디렉터리 없는
이름은 절대 경로인 `PATH` 항목만 검색하며 프로젝트에서 실행하기 전에 절대 경로로
고정됩니다. 비어 있는 항목, `.`, 상대 `PATH` 항목은 무시합니다. 선택한 값이 유효하지
않으면 안전하게 실패합니다. `bin/harness`, `./harness`처럼 디렉터리 구성 요소가 있는
상대 경로와 `C:harness` 같은 Windows 드라이브 상대 경로는 지원하지 않습니다. 대신
`/home/you/bin/harness` 또는 `~/bin/harness`를 사용하세요. 절대 경로는 실행 파일 신원
증명 없이 운영자가 선택합니다.
