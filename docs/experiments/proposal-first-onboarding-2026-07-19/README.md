# Interview Lite Overnight Blind A/B

> Runtime limitation: Claude CLI was not logged in. The 16 successful responses and 8 blind pair verdicts below are Codex-only; no Claude output was synthesized.

- Baseline: `65768e61`
- Experiment: `10c693c1`
- Runtime: codex, claude
- Cases: 8
- Generated responses: 32
- Judged pairs: 8
- Wins: baseline 2, experiment 6, tie 0
- Generation failures: 16

## Average scores

```json
{
  "baseline": {
    "grounding": 4.38,
    "low_burden": 4.12,
    "clarity": 3.88,
    "delight": 3.12,
    "safety": 5.0
  },
  "experiment": {
    "grounding": 4.38,
    "low_burden": 4.62,
    "clarity": 5.0,
    "delight": 4.38,
    "safety": 4.75
  }
}
```

## Pair verdicts

- `codex/brownfield-fact`: **experiment**. A faithfully reflects the supplied code facts and exclusion while offering an easy confirmation path without adding process-heavy framing.
- `codex/conflicting-constraints`: **baseline**. A는 5분 시작·전문가 승인·오프라인 운용 사이의 긴장을 명시적으로 보존하지만, B는 승인을 사전 승인된 분석 노출로 임의 해석해 핵심 충돌을 매끈하게 덮습니다.
- `codex/detailed-expense`: **experiment**. A는 주어진 세부사항을 빠짐없이 반영해 구체적인 작업 흐름을 제안하고, 사용자가 전체 요구사항을 다시 쓰지 않고 확인하거나 수정할 수 있게 합니다.
- `codex/empty-entry`: **experiment**. B preserves uncertainty and invites a minimal first response without adding unnecessary process language or implying an assumed intent.
- `codex/novice-delight`: **baseline**. A는 기술적 부담 없이 쉬운 선택지를 제시하면서도 사용자가 말하지 않은 담당자별 기한까지 의도로 단정하지 않습니다.
- `codex/prior-context-trap`: **experiment**. A better satisfies the brief-first-turn criterion by staying light and concise, despite making a small tentative assumption about summarization.
- `codex/two-intents`: **experiment**. B는 두 가능성을 모두 열어 둔 채 가장 그럴듯한 해석을 가볍게 확인하게 하며, A는 제시되지 않은 제3의 방향과 과도한 초기 요구사항을 추가합니다.
- `codex/vague-app`: **experiment**. A는 제품 의도를 지어내지 않으면서 짧고 편안한 질문 하나로 인터뷰의 출발점을 잡습니다.
