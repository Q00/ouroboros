# Cursor Platform Support — 설계 노트

> 2026-03-20 ~ 2026-03-24 논의 정리
> 새 세션에서 이어서 작업할 때 참고용

---

## 1. 목표

Cursor IDE에서 ouroboros를 사용할 수 있게 한다.
Claude Code 플러그인으로 이미 동작하지만, Cursor 네이티브 환경에서의 최적화가 필요.

---

## 2. 아키텍처 결정사항

### 2.1 MCP 설계 철학 (확정)

```
MCP = Context OS / Stateful Workflow Service
MCP ≠ Agent Runtime
```

- **LLM은 하나만 둔다 (Main session)** — MCP tool 안에서 별도 LLM 호출하지 않음
- **MCP는 state + deterministic logic만** — 세션 관리, 이벤트 저장, seed 파싱 등
- **task는 항상 structured packet으로 전달**
- **orchestration은 code, reasoning은 LLM**

### 2.2 Worker 패턴 (확정)

```
Main Session (사용자 대화)
    │
    ├─ MCP tool 호출 → ouroboros가 Worker 세션 생성
    │
    └─ Worker (별도 claude 프로세스)
         → 파일 직접 수정 (Write/Edit)
         → IDE file watcher가 자동 반영
         → 진행상황은 폴링으로 확인
```

- Worker가 만든 코드 변경은 **파일 시스템에 직접 기록** → IDE가 알아서 반영
- diff를 메인 세션에 전달하는 방식은 불필요 (IDE file watcher가 처리)


## 3. Cursor 환경 기술 분석

### 3.1 Cursor에서 MCP 서버 실행 경로

```
Cursor IDE
  ├─ Plugin MCP Server (Claude Code 플러그인 경유)
  │    → .claude-plugin/plugin.json → .mcp.json
  │    → 심링크: ~/.claude/plugins/cache/ouroboros/... → /local/path
  │
  └─ Installed MCP Server (~/.cursor/mcp.json 등록)
       → 직접 등록, 중복 가능
```

**문제**: 두 경로가 동시에 존재하면 tool 중복 발생

### 3.2 환경변수 충돌

Cursor IDE 안에서 Claude Code 확장이 실행될 때:
```
CURSOR_EXTENSION_HOST_ROLE=user          ← Cursor가 설정
CLAUDE_AGENT_SDK_VERSION=0.2.81          ← Claude Code가 설정
```
→ 둘 다 존재하면 런타임 자동감지가 항상 cursor를 선택
→ Claude Code 확장이 claude backend를 못 씀

### 3.3 ACP (Agent Client Protocol)

Cursor의 `cursor-agent` 프로세스와 통신하는 프로토콜:
- JSON-RPC over stdin/stdout (NDJSON 스트리밍)
- persistent session 기반
- 테스트 결과:
  - LLM 호출 가능 (chat completion)
  - IDE 모델 정보 직접 조회 불가 (ACP에서 노출 안 함)
  - `auto` 모델 사용 시 IDE 설정 모델 사용
  - 대용량 응답 시 버퍼 오버플로우 발생 가능 (`Separator is found, but chunk is longer than limit`)

### 3.4 로컬 개발 시 MCP 연결

`.mcp.json` 변경만으로 충분 (`.claude-plugin/plugin.json` 변경 불필요):
```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ouroboros", "mcp", "serve"]
    }
  }
}
```
심링크: `~/.claude/plugins/cache/ouroboros/ouroboros/0.25.1 → /local/workspace/ouroboros`

**주의**: 테스트 후 반드시 원복 (`uvx --from ouroboros-ai ouroboros mcp serve`)

---

## 4. 이전 시도 및 실패

### PR #182 (feat/cursor-platform-support-v2) — 닫음

구현 내용:
- ACP 기반 런타임 (`CursorACPRuntime`)
- ACP 기반 LLM adapter (`CursorACPLLMAdapter`)
- Model selection gate (cursor 감지 시 모델 선택 UI)
- `ooo setup` cursor 지원

실패 이유:
- 아키텍처 방향 변경 (MCP 안에 LLM 넣지 않기로)
- ACP 모델 직렬화 버그 반복
- 환경변수 충돌로 런타임 감지 불안정

---

## 5. 남은 작업 방향

### 5.1 Cursor에서 ouroboros 동작시키기 (최소 목표)

현재도 Claude Code 플러그인으로 Cursor에서 동작함. 개선할 점:
- 환경변수 충돌 해결 (runtime 감지 로직)
- 플러그인 중복 방지 (setup 시 안내)
- `session_status` 응답 경량화 (`tool_catalog` 등 bloat 제거)

### 5.2 Cursor 네이티브 최적화 (선택)

ooo 개발자 피드백: "opt-in 느낌으로 지원, 추상화보다 composition 형태"
- Cursor 전용 런타임은 composition으로 조합
- 기존 인터페이스를 상속하지 말고 필요한 것만 조합
- ACP는 Cursor-specific이므로 core에 넣지 않음

---

## 6. upstream 최신 변경 (2026-03-24 기준)

`release/0.26.0-beta`에 최근 병합된 주요 PR:
- **#191** — PM interview engine + brownfield management
- **#187** — interview deadlock fix (ambiguity score gate)
- **#178** — interview redesign (MCP question generator + main session router, max_turns=1)
- Codex CLI runtime 지원 추가
- 버전 0.26.0b4

새 브랜치는 반드시 `origin/release/0.26.0-beta` 최신에서 생성할 것.

---

## 7. 로컬 브랜치 정리 상태

| 브랜치 | 상태 |
|--------|------|
| `feat/cursor-platform-support-v2` | 남아있음 (PR #182 닫힘, 참고용) |
| `feat/cursor-platform-support-v3` | 빈 브랜치, 삭제 가능 |
| `backup/cursor-v2-pre-rebase` | 백업, 삭제 가능 |
| `feat/execution-progress-notifications` | 삭제됨 |
| `feat/execution-progress-notifications-v2` | 삭제됨 |
