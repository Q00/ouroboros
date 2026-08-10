<!--
doc_metadata:
  runtime_scope: [all]
-->

# 评估流水线指南（Evaluation Pipeline）

> 这是评估流水线指南的中文版。英文原文在同一目录下：[evaluation-pipeline.md](./evaluation-pipeline.md)。
> 两份文档描述同一套实现；如果发现不一致，以 `src/ouroboros/evaluation/` 的代码为准。

Ouroboros 的 Phase 4 会把每一次执行结果送进一条**三阶段递进式评估流水线**，然后才给出正式的验收标准（acceptance criterion，AC）判定。便宜的检查为昂贵的检查把关：Stage 1 免费，Stage 2 花一次 LLM 调用，Stage 3（多模型共识）只在被明确触发时才跑。

> **术语边界：** worker 报告「任务完成」不等于正式的 AC 判定，任务失败也不等于语义漂移。`TaskResult` 与 `ACResult` 的区分见 [Execution vs. Evaluation Contract](./execution-vs-evaluation.md)（英文）。

```
Artifact ready
      │
      ▼
┌─────────────────────────────┐
│  Stage 1: Mechanical ($0)   │ lint / build / test / static / coverage
│  All checks must pass       │
└────────────┬────────────────┘
             │ passed
             ▼
┌─────────────────────────────┐
│  Stage 2: Semantic ($$)     │ LLM evaluates AC compliance, goal
│  score ≥ 0.8 + ac_compliance│ alignment, drift, uncertainty
└────────────┬────────────────┘
             │ passed
             ▼
        ┌────┴────┐
        │ Trigger │ ← 7 conditions checked
        │ matrix  │
        └────┬────┘
             │ triggered?
        ┌────┴────────────────────────────┐
       YES                               NO
        │                                 │
        ▼                                 ▼
┌───────────────────────┐          ┌───────────────┐
│  Stage 3: Consensus   │          │   APPROVED    │
│  ($$$, 2/3 majority)  │          └───────────────┘
└───────────┬───────────┘
            │
   ┌────────┴────────┐
  YES               NO
   │                 │
   ▼                 ▼
APPROVED          REJECTED
```

（图中保留英文以维持等宽对齐。`Artifact ready` = 产物就绪，`All checks must pass` = 所有检查都必须通过，`passed` = 通过，`Trigger matrix` = 触发矩阵，`7 conditions checked` = 检查 7 个条件，`triggered?` = 触发了吗，`APPROVED` / `REJECTED` = 通过 / 拒绝。）

> 这张图画的是主干路径。有两件事在图外：`trigger_consensus=true` 会从触发矩阵、或者从 Stage 2 的 AC 未通过处直接跳到 Stage 3；而 [reward hacking 否决](#reward-hacking-否决)可以把上面任何一个 `APPROVED` 翻回 `REJECTED`。

---

## Stage 1：机械验证（Mechanical Verification）

机械验证器运行零成本的自动化 shell 命令，只看退出码。它**不调用任何 LLM**。

### 检查项

| 检查 | 运行什么 | 失败条件 |
|-------|-------------|-------------------|
| `lint` | 配置里的 `lint_command` | 退出码非 0 |
| `build` | 配置里的 `build_command` | 退出码非 0 |
| `test` | 配置里的 `test_command` | 退出码非 0 |
| `static` | 配置里的 `static_command` | 退出码非 0 |
| `coverage` | 配置里的 `coverage_command` | 退出码非 0，**或**解析出的覆盖率低于 `coverage_threshold`（默认 **70%**） |

**流水线行为：** 只要**任意一项**检查失败，Stage 2 和 Stage 3 会被整个跳过，产物立即被拒绝。

**被跳过的检查：** 如果某项检查没有配置命令（`None`），它会被静默跳过并**当作通过处理**。在没有于 `PipelineConfig.mechanical` 设置命令时，这就是默认状态。

### Stage 1 失败模式

| 失败模式 | 现象 | 原因 |
|---|---|---|
| **命令不存在** | `Check <type> failed`，附带 "Command not found" | 可执行文件不在 PATH 中；检查环境 |
| **命令超时** | `Check <type> timed out after Ns` | 命令超过 `timeout_seconds`（默认 300 秒）；调大超时或修掉慢测试 |
| **退出码非 0** | `Check <type> failed (exit code N)` | 工具发现了真实错误；查看事件负载里的 `stdout_preview` / `stderr_preview` |
| **覆盖率低于阈值** | `Coverage X.X% below threshold Y.Y%` | 测试套件没达到最低覆盖率要求；补测试或调低 `coverage_threshold` |
| **覆盖率无法解析** | coverage 检查通过，但事件里没有 `coverage_score` | 输出不匹配预期格式（`TOTAL ... XX%`）；确认使用的是 `pytest-cov` 或兼容工具 |
| **OS 错误** | `Check <type> failed`，附带 "OS error" | 权限问题或工作目录不存在；检查 `working_dir` 配置 |

### Stage 1 的命令从哪来

Ouroboros **不再内置**按语言硬编码的预设（preset）。Stage 1 只信任一个来源：项目根目录下的 `.ouroboros/mechanical.toml`。`build_mechanical_config(working_dir)` 只是这个文件的确定性读取器——文件不存在时，所有命令解析为 `None`，Stage 1 优雅跳过，而不是去猜一个工具来跑。

这个文件由 `ouroboros.evaluation.detector` 写入：它发起**一次 AI 调用**，读取项目的清单文件（`pyproject.toml`、`uv.lock`、`package.json`、`Cargo.toml`、`go.mod`、`pom.xml`、`build.gradle`、`Makefile`、`justfile`、`Taskfile.yml`、`build.zig`、`CMakeLists.txt`、`mix.exs`、`Gemfile` 等），为这个具体仓库提出命令。每条被提出的命令在落盘前都要过校验——可执行文件白名单、shell 操作符与绝对路径注入、以及仓库本身（例如只有存在 `Cargo.toml` 时才保留 `cargo` 命令）——所以 toml 里只会留下安全的、且被该仓库声明过的命令。

**校验到此为止。** `_command_is_valid()` **有意不查询主机的 `PATH`**（`evaluation/detector.py:496`），因此它证明的是「命令安全且仓库声明了它」，而不是「这个可执行文件在本机装好了」。清单里声明的 `pytest` 命令即使本地没装 `pytest` 也会被写入，Stage 1 会在运行时报 "Command not found"。

显式生成或刷新：

```bash
ouroboros detect              # 检查当前目录并写入 .ouroboros/mechanical.toml
ouroboros detect --force      # 重新检测并覆盖已有文件
ouroboros detect --backend codex   # 为这次 detect 调用指定 LLM 后端
```

`ensure_mechanical_toml()` 是幂等的：文件已存在且 `force` 为假时，它立即返回，不发起 LLM 调用。

**失败有两种形态，要分开看。** 大多数失败被处理成返回 `False`，Stage 1 因此没有命令可用。但它**并非从不抛异常**：`_ask_llm()` 调用 `tracked_complete()` 时没有异常边界，因此适配器异常会向上传播，而直接执行的 `ouroboros detect` 命令也不捕获这一调用。诊断时请把「处理过的 `False`」和「抛出的异常」当作两件事。

> **如果 Stage 1 永远通过，通常就是这个原因。** 既没有 `.ouroboros/mechanical.toml`，也没有显式配置 `MechanicalConfig` 命令时，五项检查全部被跳过并当作通过，Stage 1 就成了一道什么都不验的空关卡。
>
> **但请注意：缺少 TOML 本身并不足以解释它。** 常规 `run` 与 MCP 评估路径都会先自动尝试生成这个文件。所以在这些路径上出现空关卡，意味着**自动检测失败了**（返回 `False` 或抛出异常），而不是「文件恰好不存在」。跑 `ouroboros detect` 看它到底报什么，或者手写这个 toml。

> **已废弃：** `detect_language()` 现在什么也检测不了。它只是一个兼容垫片，读取 `.ouroboros/mechanical.toml` 并发出 `DeprecationWarning`；请改用 `ensure_mechanical_toml()` 加 `build_mechanical_config()`。

> **Go 覆盖率注意：** `go test -cover` 的输出格式（`ok  ./... coverage: XX.X% of statements`）不被覆盖率解析器匹配（它期望 `TOTAL ... XX%` 或 `Coverage: XX%`）。因此在 Go 项目里，事件负载中的 `coverage_score` 永远是 `None`，**即使覆盖率很低，阈值检查也会被跳过**。如果 Go 项目需要强制阈值，请用 `.ouroboros/mechanical.toml` 覆写一条自定义的 coverage 命令。

### 项目级命令覆写

Stage 1 的命令就住在项目根目录的 `.ouroboros/mechanical.toml` 里。detector 会写它，你也可以直接编辑或手写，不需要改动 Ouroboros 的配置：

```toml
# .ouroboros/mechanical.toml
lint = "ruff check src/"
test = "pytest tests/unit -q"
coverage = "pytest --cov=src --cov-report=term-missing tests/"
coverage_threshold = 0.85
timeout = 120
```

**覆写优先级**（从高到低）：
1. 以编程方式传入的显式 `overrides` 字典（**仅限受信任的 Python 调用方，不存在 MCP 路径**）
2. 项目根目录的 `.ouroboros/mechanical.toml`
3. 全部为 `None`（所有检查优雅跳过）

**TOML 解析错误**会被记为一条警告（`mechanical.toml_parse_error`）后静默忽略。没有预设可以回退，所以所有命令保持 `None`，Stage 1 跳过全部检查。

**安全：可执行文件白名单。** `.ouroboros/mechanical.toml` 中的命令只能使用内置白名单里的可执行文件（如 `pytest`、`ruff`、`cargo`、`go`、`npm`、`make`）。如果命令指定了不在白名单里的可执行文件——或者用了 shell 操作符、绝对路径——它会被静默拦截（记为 `mechanical.blocked_executable`），该项检查被跳过。**这里有两种不同的 Python 机制，不要混为一谈。** 传给 `build_mechanical_config(..., overrides=...)` 的值**仍然会经过** shell 操作符与可执行文件头部的白名单/路径解析（`_apply_overrides()` 对每个值调用 `_parse_command()`，`evaluation/languages.py:247`），只是跳过了针对 TOML 值的仓库入口点与参数包含性校验。**只有直接构造 `MechanicalConfig`** 才会绕过这两层解析，那才是真正的受信任调用方输入。**两者都不是 MCP 请求参数**：`ouroboros_evaluate` 没有暴露任何机械命令参数（`mcp/tools/evaluation_handlers.py:437`），其 handler 调用 `build_mechanical_config(working_dir)` 时也不传 `overrides`（`:742`），也不构造特权 `MechanicalConfig`。这道机制防止不受信任的仓库配置在 CI/CD 环境里执行任意命令。

| 覆写失败模式 | 现象 | 原因 / 处理 |
|---|---|---|
| **TOML 解析错误** | 所有 Stage 1 检查被跳过；不抛错 | `.ouroboros/mechanical.toml` 格式有误；检查 TOML 语法 |
| **可执行文件被拦截** | 该项检查被静默跳过 | 可执行文件不在白名单；换成允许的工具，或直接在 `MechanicalConfig` 里设置命令 |
| **没有 toml 文件** | 所有 Stage 1 检查被跳过 | 常规 `run` 与 MCP 路径会**先自动尝试生成**它，所以在这些路径上出现空关卡意味着**自动检测失败了**——跑 `ouroboros detect` 看它报什么。只有绕过检测的底层调用方才会「单纯没有文件」，那种情况在 `MechanicalConfig` 里显式设置命令即可 |

### Stage 1 配置

```yaml
# 位于 PipelineConfig.mechanical（MechanicalConfig）
mechanical:
  coverage_threshold: 0.7           # 最低 70%（NFR9）；老项目可调低
  timeout_seconds: 300              # 单条命令的超时秒数
  working_dir: /path/to/project     # 省略时默认为进程 cwd
  lint_command: ["ruff", "check", "."]
  build_command: ["python", "-m", "build"]
  test_command: ["pytest", "tests/"]
  static_command: ["mypy", "src/"]
  coverage_command: ["pytest", "--cov=src", "--cov-report=term-missing", "tests/"]
```

> **重要：** 这些字段是编程侧的逃生舱。在正常的 `ouroboros run` 路径上，命令来自 `.ouroboros/mechanical.toml`。**但这条路径会先自动尝试生成该文件**，所以要让 Stage 1 在这里静默跳过每一项检查（并视为通过），需要同时满足：**自动检测失败了**（返回 `False` 或抛异常）、文件不存在、且没有显式配置命令。单纯「文件不在」并不足以解释——那种情况只出现在绕过检测的底层调用方身上。参见上面的失败模式表。

### 诊断 Stage 1 失败

用事件查询看看发生了什么：

```bash
uv run ouroboros status execution <exec_id> --events
```

找类型为 `evaluation.stage1.completed` 的事件，负载里包含：
- `passed`：整体结果
- `checks`：每项检查的 `check_type`、`passed`、`message` 列表
- `coverage_score`：解析成功时的覆盖率数值
- `failed_count`：失败检查的数量

---

## Stage 2：语义评估（Semantic Evaluation）

Stage 2 调用一个 Standard 档位的 LLM（默认取 `OUROBOROS_SEMANTIC_MODEL` / 配置值），针对验收标准评估产物。模型返回一个结构化 JSON 对象。

### 打分字段

| 字段 | 类型 | 范围 | 含义 |
|-------|------|-------|---------|
| `score` | float | 0.0–1.0 | 总体质量分 |
| `ac_compliance` | bool | — | 验收标准是否被满足 |
| `goal_alignment` | float | 0.0–1.0 | 与原始 seed 目标的一致程度 |
| `drift_score` | float | 0.0–1.0 | 偏离 seed 意图的程度（越低越好） |
| `uncertainty` | float | 0.0–1.0 | 模型对自己这次评估的不确定度 |
| `reasoning` | string | — | 自由文本解释 |
| `reward_hacking_risk` | float | 0.0–1.0 | 产物在糊弄评估器而非真正解决任务的嫌疑。与 `drift_score` 是两回事；`>= 0.7` 时否决通过 |
| `questions_used` | list | — | 评估器在验证产物时实际问过的问题——它必须把自己的工作过程摊开 |
| `evidence` | list | — | 判定所依据的文件片段与观察结果 |

### 通过逻辑

```
if ac_compliance == False 且 not trigger_consensus  → REJECTED（不尝试 Stage 3）
if score < 0.8                    → REJECTED（除非 Stage 3 被触发并通过）
if score >= 0.8 且没有触发        → APPROVED
if reward_hacking_risk >= 0.7     → REJECTED（最终否决，压过任何通过结论）
```

> **分数关卡是硬编码的 `0.8`。** `SemanticConfig.satisfaction_threshold`（默认 `0.8`）确实存在，也会被校验，但流水线比较的是字面量 `0.8`，从不读取这个字段。今天改它对通过与否没有任何影响——不要指望用它来放宽或收紧关卡。

> **`ac_compliance=False` 并不总是终局。** 当评估上下文设置了 `trigger_consensus=True` 时，流水线不会就此拒绝，而是继续走到触发矩阵，让 Stage 3 对这条 Stage 2 判失败的 AC 给出第二意见。

> 分数在解析后会被钳制到 0.0–1.0；模型返回的越界值会被自动纠正。

### Reward hacking 否决

`_build_result()` 施加了一道所有通过路径都必经的最终关卡：如果 Stage 2 报告 `reward_hacking_risk >= 0.7`（`REWARD_HACKING_VETO_THRESHOLD`），一个本来会通过的结果会被翻成拒绝。这个阈值定得刻意偏高，好让评估器轻微的疑心不至于挡下一次真实的通过。

这道否决只把「通过」变成「拒绝」——它从不挽救一个已经被拒绝的结果，所以 Stage 3 共识的拒绝仍然是拒绝。

> **它并非总是被点名。** `_build_result()` 里 Stage 2 AC 未通过的分支排在否决分支**之前**。所以当 `trigger_consensus=True` 带着 `ac_compliance=False` 走到一个通过的 Stage 3、再由否决翻成拒绝时，报出来的 `failure_reason` 是 Stage 2 的 AC 未通过，而不是否决本身。判断是否发生了否决，请直接看 `stage2_result.reward_hacking_risk`。

### Stage 2 失败模式

| 失败模式 | 现象 | 原因 / 处理 |
|---|---|---|
| **LLM API 错误** | 返回 `ProviderError` | 网络问题、限流或 API key 无效。检查 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`。错误会向上传播——流水线停止，但不会标记为拒绝。 |
| **响应里没有 JSON** | `ValidationError: Could not find JSON in response` | LLM 的回复里没有 JSON 对象。某些「供应商 + 模型」组合会这样。确认模型对 `json_schema` 响应格式的兼容性。 |
| **JSON 非法** | `ValidationError: Invalid JSON in response` | 模型输出的 JSON 解析失败。可能是输出被截断；试着调大 `max_tokens`。 |
| **缺少必填字段** | `ValidationError: Missing required fields: [...]` | 模型漏掉了必填字段（`score`、`ac_compliance` 等）。通常意味着该模型对结构化输出的支持不稳定。 |
| **AC 未满足** | `Stage 2 failed: AC non-compliance (score=X.XX)` | LLM 判定产物没有满足验收标准。查看 Stage 2 完成事件里的 `reasoning`。 |
| **分数低于阈值** | `ac_compliance=True` 但 `final_approved=False` | 分数落在 0.0–0.79。要么产物质量确实不行，要么这条 AC 写得太宽。 |

### Stage 2 配置

```yaml
# 位于 PipelineConfig.semantic（SemanticConfig）
semantic:
  model: null                            # null = 从 OUROBOROS_SEMANTIC_MODEL / 配置解析
  temperature: 0.2                       # 调低以保持一致性
  max_tokens: 2048                       # 响应 token 预算
  satisfaction_threshold: 0.8           # 目前不生效 —— 关卡硬编码为 0.8
```

### 诊断 Stage 2 失败

找 `evaluation.stage2.completed` 事件，关键字段：
- `score`、`ac_compliance`、`goal_alignment`、`drift_score`、`uncertainty`

如果 `ac_compliance` 是 `false` 但 `score` 看起来不低，可能是 LLM 发现了一个「部分实现」。读完整事件负载里的 `reasoning` 字段看解释。

---

## 共识触发矩阵（Stage 2 → Stage 3 关卡）

触发条件会**按优先级顺序**逐个评估，第一个命中的触发 Stage 3。

**注意这里的门槛不是分数。** `EvaluationPipeline.evaluate()` 只在 `ac_compliance=False` **且没有手动共识请求**时提前返回。也就是说，一个 AC 合规但只拿到 `0.7` 的结果**仍会走到触发评估**；若有条件命中，它会进入 Stage 3，并可能最终被批准。分数关卡（`0.8`）是**更后面**的一道判定，不是进入触发矩阵的前提。

| 优先级 | 触发器 | 条件 |
|----------|---------|-----------|
| 0 | `manual_request` | `manual_consensus_request=True`，由评估上下文的 `trigger_consensus=true` 设置 |
| 1 | `seed_modification` | 上下文中 `seed_modified=True` |
| 2 | `ontology_evolution` | 上下文中 `ontology_changed=True` |
| 3 | `goal_interpretation` | 上下文中 `goal_reinterpreted=True` |
| 4 | `seed_drift_alert` | `drift_score > drift_threshold`（默认 **0.3**） |
| 5 | `stage2_uncertainty` | `uncertainty > uncertainty_threshold`（默认 **0.3**） |
| 6 | `lateral_thinking_adoption` | 上下文中 `lateral_thinking_adopted=True` |

> **`manual_request` 会短路掉其余一切。** 它排在其他六个之前被检查，所以只要显式要求共识，无论漂移、不确定度、还是 Stage 2 的判定如何，都一定会走到 Stage 3。

> **只有第一个命中的触发器会生效。** 如果漂移是 0.5，同时又采纳了 lateral thinking，只会报告 `seed_drift_alert`（优先级 4）。

> **两个阈值比较都是严格的 `>`。** `drift_score` 正好等于 `drift_threshold` 不会触发 Stage 3。当 Stage 2 跑过时，它的 `drift_score` 和 `uncertainty` 优先于 `TriggerContext` 上预填的值。

### 触发器配置

```yaml
# 位于 PipelineConfig.trigger（TriggerConfig）
trigger:
  drift_threshold: 0.3        # 调高可减少 Stage 3 的调用
  uncertainty_threshold: 0.3  # 调高可减少 Stage 3 的调用
```

调高这些阈值能省下 Stage 3 的成本，但也可能让低置信度的输出绕过共识。

### 触发器失败模式

| 失败模式 | 现象 | 原因 / 处理 |
|---|---|---|
| **Stage 3 意外被触发** | 成本意外偏高 | Stage 2 的不确定度超过阈值。查 `evaluation.consensus.triggered` 事件里的 `trigger_type`。 |
| **Stage 3 从不触发** | 质量疑虑无人复核 | 所有触发条件都为假；确认 `drift_score` 和 `uncertainty` 字段确实从 Stage 2 正确传了下来。 |
| **触发器校验错误** | 触发器抛出 `ValidationError` | `TriggerContext` 结构有误；确认 `execution_id` 和各数值字段合法。 |

---

## Stage 3：多模型共识（Multi-Model Consensus）

Stage 3 并发发起多次 Frontier 档位的问询，需要 **2/3 多数**才算通过。**注意措辞**：这些是并发的「投票」，但并不意味着背后有多个独立模型——简单共识的每一票都通过交给 `ConsensusEvaluator` 的**同一个适配器**发出（`ConsensusEvaluator._llm`）。

### 简单共识（默认）

三个模型并行被问询。出厂默认的名单如下，但**这三个名字并不能证明有三个厂商在独立投票**——原因见本节末尾的说明：

```
openrouter/openai/gpt-4o
openrouter/anthropic/claude-opus-4.8
openrouter/google/gemini-2.5-pro
```

> **名单不等于独立性。** 实际生效的名单取决于环境与后端：sentinel 后端会把三个槽位解析成字面量 `"default"`；环境变量或自定义配置名单会被原样保留；reviewer 独立性过滤还可能再改动名单。**更关键的是，所有投票都通过交给 `ConsensusEvaluator` 的那一个适配器发出**——换名单不会换适配器。因此模型标签**不能**证明来自不同厂商的独立评审。要判断实际发生了什么，请读记录下来的投票，而不是配置里的名单。

#### 哨兵模型后端（sentinel-model backends）

没有 `OUROBOROS_CONSENSUS_MODELS` 覆盖时，后端感知的配置/默认解析会把**出厂名单**的三个槽位全部映射成字面量字符串 `"default"`——只要该后端属于 `_SENTINEL_DEFAULT_BACKENDS`（`config/loader.py:101`）。环境变量名单，或非出厂的自定义配置名单，则**原样保留**。哨兵集合为：Codex、**OpenCode**、Kiro、Copilot、Hermes、Pi、GJC、Antigravity、Grok、Zcode。

**OpenCode 很容易被漏掉**：它不是这个并集里的独立成员，而是通过 `_CODEX_LLM_BACKENDS`（即 `frozenset({"codex", "codex_cli", "opencode", "opencode_cli"})`，`config/loader.py:76`）搭车进来的。因此在与 Codex 相同的「无环境覆盖」条件下，它的出厂名单同样会归一化为 `("default", "default", "default")`。位于 `config/loader.py:113` 的 `_OPENCODE_BACKENDS` 是另一回事，用于权限模式解析，与模型默认值无关。

`_should_use_multi_model()` 对任何**不含 `openrouter/` 条目**的名单都会选择 `_evaluate_multi_model()` 分支（`evaluation/consensus.py:313`），所以 `"default"` **不会**触发单模型回退。**分支名并不能证明模型多样性**：在这些后端上，Stage 3 投出三票、标签都是 `default`，而它们全部来自该后端所配置的那**一个**模型。

> **审计提示。** 三张标着 `default` 的票不是三位独立评审。如果你的共识证据长这样，说明名单解析到了哨兵值，而这些票来自同一个模型。

**评审独立性过滤发生在模型档位解析之前。** 多模型路径以配置名单为起点；当 `EvaluationContext.executor_backend` 存在时，会把这些请求标签连同 `available_runtime_backends()` 一起交给 `resolve_reviewer_independence()`。若检测到的、已安装且可运行的后端中**厂商家族少于两个**，过滤就是空操作并报告 `unavailable`。否则，与执行器同厂商的标签只有在「至少还能剩两个」时才会被移除；未知厂商的标签会被保留；如果过滤后不足两位投票者，则保留原名单。`evaluation.stage3.started` 事件与并发投票任务列表用的都是**过滤后**的名单。


每个模型返回 `{ approved, confidence, reasoning }`。

**通过规则：** `approving_votes / total_votes >= 0.66`（即至少 3 票中的 2 票）。这个比例是在**实际收集到**的票数上计算的，而不是配置的模型数量——见[并行共识的失败容忍](#并行共识的失败容忍)。

> **单模型回退。** 当配置的模型是 `openrouter/*`，但 `OPENROUTER_API_KEY` 缺失、或仍是 `YOUR_…` 占位符时，`ConsensusEvaluator` 会静默切换到单模型模式：用 advocate、devil's advocate、judge 三套 system prompt 把会话模型问三次，由这三个视角投票。2/3 规则和「不足 2 票报错」的行为不变，但这三位审阅者在构造上就是同一厂商，所以「审阅者独立性」会被报告为不可用。此时 Stage 3 事件里的模型名是 `session/<perspective>`，触发原因带 `single-model-perspectives:` 前缀——如果 Stage 3 便宜得不合常理，就查这两处。配置非 `openrouter/*` 的模型会跳过这道检查，直接按配置使用。

### 审议式共识（Deliberative Consensus）

一种可选的两轮模式：
1. **第 1 轮（并行）：** Advocate（找出优点）和 Devil's Advocate（用本体论分析做根因验证）**并行**陈述各自的立场（这里的「独立」指立场，不指模型——两者仍走同一个适配器）。
2. **第 2 轮：** Judge 复核两方立场，给出判定：`approved`、`rejected` 或 `conditional`。

> **注意：** `conditional` 是审议模式下 Judge 的合法判定。在 `DeliberationResult.approved` 属性里，`conditional` 映射为**拒绝**（该属性只有 `approved` 时才返回 `True`）。附加条件列在 `JudgmentResult.conditions` 中。

### Stage 3 失败模式

| 失败模式 | 现象 | 原因 / 处理 |
|---|---|---|
| **收集到的票数少于 2** | `ValidationError: Not enough votes collected: N/3` | 多个模型返回了 API 错误。检查所有已配置共识模型的 API key。3 个模型中至少要有 2 个响应。 |
| **各模型意见完全分歧** | `majority_ratio` 在 0.33–0.50 附近 | 真实的分歧。查看事件负载里的 `disagreements` 列表。考虑打磨这条 AC 或产物本身。 |
| **多数比例低于阈值** | `Stage 3 failed: Consensus not reached (XX%)` | 通过票不足 2/3。`ConsensusResult` 的 `disagreements` 元组里有反对者的理由。 |
| **单个模型 API 错误** | 记日志但被容忍 | 一个模型失败，用剩下的票。如果只剩 1 票，抛出 `ValidationError`。 |
| **审议式：Advocate 失败** | `ValidationError: Advocate failed: ...` | Advocate 模型 API 错误。审议模式不容忍这个错误——整个 Stage 3 失败。 |
| **审议式：Devil's Advocate 的 LLM 错误** | Devil 投出 `approved=False` 且置信度很低的一票 | `DevilAdvocateStrategy` 在内部消化 LLM 错误，返回 `AnalysisResult.invalid`（软失败）而不向上传播。Devil 的 LLM 失败**不会**中止 Stage 3；它变成 Devil 投出的一张反对票，这可能导致 Judge 拒绝。 |
| **审议式：Judge 失败** | `ProviderError` 或 `ValidationError` | Judge 模型出错，Stage 3 失败。审议模式对 Judge 没有部分投票容忍。 |
| **投票者返回非法 JSON** | `ValidationError: Could not find JSON in vote from <model>` | 模型返回了格式错误的 JSON。重试，或在 `ConsensusConfig.models` 里换个模型。 |
| **Judge 返回非法判定** | `ValidationError: Invalid verdict '<x>' from <model>` | Judge 回了一个无法识别的判定字符串。可接受的值：`approved`、`rejected`、`conditional`。 |

### Stage 3 配置

**简单共识（`ConsensusConfig`）**

```yaml
# 位于 PipelineConfig.consensus（ConsensusConfig）
consensus:
  models:
    - "openrouter/openai/gpt-4o"
    - "openrouter/anthropic/claude-opus-4.8"
    - "openrouter/google/gemini-2.5-pro"
  temperature: 0.3
  max_tokens: 1024
  majority_threshold: 0.66     # 2/3 多数
  diversity_required: true     # 目前不生效 —— 见下方说明
```

> **`diversity_required` 并未被强制执行。** 这个字段在 `ConsensusConfig` 和配置 schema 里都存在，但没有任何代码读它。如果你把名单换成同一厂商的三个模型，`diversity_required: true` 不会提出任何异议。
>
> **而且名单本身也不提供厂商多样性。** 所有票都经由同一个适配器发出，所以名单里的标签只是**请求标签**，不代表真的联系了那些厂商、模型或传输通道。

**审议式共识（`DeliberativeConfig`）**

配合 `DeliberativeConsensus` 使用（不是 `ConsensusEvaluator`）。每个角色**配置各自的模型标签**——但与简单共识一样，**所有角色都通过传给 `DeliberativeConsensus` 的那一个适配器发出**，标签不代表独立的模型或厂商：

```python
from ouroboros.evaluation.consensus import DeliberativeConfig, DeliberativeConsensus

config = DeliberativeConfig(
    advocate_model="openrouter/anthropic/claude-opus-4.8",  # Advocate 角色
    devil_model="openrouter/openai/gpt-4o",                 # Devil's Advocate（本体论分析）
    judge_model="openrouter/google/gemini-2.5-pro",         # 最终判定
    temperature=0.3,
    max_tokens=2048,
)
evaluator = DeliberativeConsensus(llm_adapter, config)
```

`DeliberativeConfig` 的默认模型读自 `OUROBOROS_CONSENSUS_ADVOCATE_MODEL`、`OUROBOROS_CONSENSUS_DEVIL_MODEL`、`OUROBOROS_CONSENSUS_JUDGE_MODEL` 三个环境变量（或 [Config Reference](../config-reference.md) 里记录的配置值）。

### 诊断 Stage 3 失败

找 `evaluation.stage3.completed` 事件，关键字段：
- `approved`：最终决定
- `votes`：`{ model, approved, confidence, reasoning }` 的列表
- `majority_ratio`：通过票占比
- `disagreements`：反对票的理由

> **审议模式下 `majority_ratio` 的注意事项：** 在审议式共识里，`evaluation.stage3.completed` 事件的 `majority_ratio` 永远是 `1.0`（通过）或 `0.0`（拒绝）——它并不反映真实的票数比例。要看 Advocate 和 Devil's Advocate 的立场，请用 `votes` 列表以及其中每一项的 `approved` 字段。

---

## 产物收集（Artifact Collection）

`ArtifactCollector` 会读取本次执行真正改动过的源文件，让语义评估器拿到真实代码而不只是 agent 的文字摘要。

> **它不是流水线自动跑的。** `EvaluationPipeline.evaluate()` 从不实例化 `ArtifactCollector`。**MCP 评估路径**会收集并挂上 `artifact_bundle`；**直接构造 `EvaluationPipeline` 的 Python 调用方必须自己提供**，否则语义评估退回到 `EvaluationContext.artifact`（即文字产物）。本指南后面也介绍了直接构造的用法，所以这一点要特别当心：**否则你会以为源码被评估了，实际上只评估了那段文字。**

### 收集上限

| 上限 | 值 | 超出后的效果 |
|-------|-------|---------------------|
| 单文件大小上限（`MAX_FILE_SIZE`） | 100 KB | 超过 100 KB 的文件被静默跳过 |
| 内容总量上限（`MAX_TOTAL_CHARS`） | 150,000 字符（约 37K token） | 内容按剩余预算截断，`FileArtifact.truncated=True` |

**文件数量没有上限，但「字符预算是唯一限制器」的说法并不准确。** 实际上有两道独立的上限：超过 100 KB 的单个文件会被整个跳过，而大量小文件累积起来同样会耗尽 150,000 字符的**文件内容**预算并被截断。此外 `text_summary` 存放在该预算之外，不占用它。

> 这两条上限的行为不一样。超过单文件大小上限的文件被**整个跳过**（绝不截断）；只是耗尽了共享字符预算的文件会被**截断**并标记 `truncated=True`。如果某个关键文件总是缺席，先看看它是不是应该被排除在评估之外的生成物、二进制或压缩产物。

### 产物收集失败模式

| 失败模式 | 现象 | 原因 / 处理 |
|---|---|---|
| **未传入 project_dir** | 评估只用到文字摘要 | `EvaluationContext` **没有 `project_dir` 字段**。目录要作为参数传给 `ArtifactCollector.collect(execution_output, project_dir)`，再把返回的 bundle 作为 `artifact_bundle` 挂到 `EvaluationContext` 上。只在「执行上下文里设置 `project_dir`」是做不到的——那样什么文件都不会被收集，语义评估只看 `EvaluationContext.artifact`。 |
| **没有抽取到文件路径** | **仍会收集文件** | 执行输出里没有可识别的 `Write:` / `Edit:` / `file_path:` 模式时，`collect()` 会转而调用 `_scan_directory(project_dir)`，遍历项目里符合条件的源文件（跳过二进制/生成物与依赖、缓存目录）。只有当扫描为空、不可访问或被完全排除时才没有文件可用。 |
| **路径穿越被拦截** | 文件被静默跳过 | 文件路径解析后落在 `project_dir` 之外。这是安全边界，不是 bug。 |
| **权限错误** | 文件被静默跳过 | 执行时用的是另一个用户身份。检查文件权限。 |
| **大文件被跳过** | 评估缺少**那一个**文件 | 文件超过 100 KB 会被整个跳过，但**其余已收集的文件照常参与评估**——跳过一个大文件并不会让评估退化成只看文字摘要。拆分大文件，或接受该文件缺席。 |

---

## 流水线级错误处理

### 错误（Error）与失败（Failure）

Ouroboros 区分**失败**（产物没达到标准）与**错误**（流水线自身跑不完）：

| 结果 | 类型 | 会发生什么 |
|---------|------|-------------|
| Stage 1 检查未通过 | 失败 | `EvaluationResult.final_approved=False`，并设置 `failure_reason` |
| Stage 2 AC 未满足 | 失败 | 同上 —— `EvaluationResult.final_approved=False` |
| Stage 3 少数票 | 失败 | 同上 —— `EvaluationResult.final_approved=False` |
| LLM API 错误（Stage 2/3） | 错误 | `Result.err(ProviderError)` 向上传播 —— runner 收到的是错误，而不是一个失败结果 |
| 票数不足（Stage 3） | 错误 | `Result.err(ValidationError)` —— 共识根本无从尝试 |
| JSON 解析失败（Stage 2/3） | 错误 | `Result.err(ValidationError)` —— 本次评估被放弃 |

**错误**会让这条 AC 处于未定状态。orchestrator 的 runner 通过档位升级（换更强的模型重试）来处理；重试耗尽后则交给停滞检测。

### 关闭某些阶段

各阶段可以在 `PipelineConfig` 里单独关闭：

```python
from ouroboros.evaluation.pipeline import PipelineConfig

# 跳过机械验证（例如产物是文档类的）
config = PipelineConfig(stage1_enabled=False)

# 跳过共识（成本受限的运行）
config = PipelineConfig(stage3_enabled=False)
```

> **警告：** 关闭 Stage 1 意味着坏掉的代码可以直接进入语义评估。关闭 Stage 3 意味着高漂移、高不确定度的输出永远不会被送去多模型复核。

> **先说清楚这些开关在哪里生效。** 本节讨论的 `stage1_enabled` / `stage2_enabled` / `stage3_enabled` 是**直接构造 `PipelineConfig` 时的 Python 运行时控制项**。`~/.ouroboros/config.yaml` 里**同名**的顶层键 `evaluation.stage1_enabled`、`stage2_enabled`、`stage3_enabled` 目前只会通过 schema 校验，**运行时构造器并不会把它们写进 `PipelineConfig`**。同理，顶层 `evaluation.uncertainty_threshold` 也不会写入 `TriggerConfig.uncertainty_threshold`。改 YAML 里的这几个键不会改变行为。

> **关闭 Stage 2 并不会关闭 Stage 3。** 触发上下文被刻意构建在 Stage 2 代码块之外，正是为了让 `trigger_consensus=True` 在 `stage2_enabled=False` 时依然有效——它会触发 `manual_request`，Stage 3 独立跑起来。真正会悄悄关掉 Stage 3 的是这个组合：`stage2_enabled=False`、`trigger_consensus=False`、且没有外部 `trigger_context`——此时所有触发字段都是默认值，没有一条能命中。要在不跑 Stage 2 的情况下到达 Stage 3，要么设置 `trigger_consensus=True`，要么向 `EvaluationPipeline.evaluate()` 传入一个预填好的 `TriggerContext`。

### failure_reason 对照表

`EvaluationResult.failure_reason` 返回一个人类可读的字符串：

| 条件 | `failure_reason` 的值 |
|-----------|------------------------|
| Stage 1 未通过 | `"Stage 1 failed: lint, test"`（逗号分隔的失败检查名） |
| Stage 2 AC 未满足（`ac_compliance=False`） | `"Stage 2 failed: AC non-compliance (score=0.62)"` |
| Stage 2 分数低于阈值（`ac_compliance=True` 但 `score < 0.8`） | `"Unknown failure"` —— 分数检查发生在 Stage 2 之后，但 `failure_reason` 属性只判断 `ac_compliance`。要区分这种情况，请直接查 `stage2_result.score`。 |
| Stage 3 未达成共识 | `"Stage 3 failed: Consensus not reached (44%)"` |
| Reward hacking 否决（`reward_hacking_risk >= 0.7`） | 通常是一条点名风险分与 0.70 阈值的消息。**但当同一次结果里 Stage 2 的 `ac_compliance=False` 时，AC 分支排在前面，报出来的会是 AC 未通过** —— 此时请查 `stage2_result.reward_hacking_risk` |
| 所有阶段都通过或跳过，但 `final_approved=False` | `"Unknown failure"` |

---

## 评估中的边界情况

### 按 AC 独立评估

AC 树里的每一条 AC 都被**独立**评估。`EvaluationContext` 只携带一个 `current_ac` 字符串。即使一个产物包（artifact bundle）引用了跨多条 AC 的文件，语义评估器也只针对当前这一条 AC 打分。

### 数值分数的钳制

无论 LLM 返回什么，Stage 2 的分数都会被自动钳制到 [0.0, 1.0]。模型给出的越界值不会报错，而是被静默纠正。如果你看到分数正好是 0.0 或 1.0，检查一下模型是不是在返回越界值。

### Stage 2 不确定度的传播

如果外部传入的 `TriggerContext` 已经设置了 `uncertainty_score`，但 `semantic_result` 字段同时也有值，那么在漂移和不确定度的触发判断中，**`semantic_result`** 的值优先。预填的 `TriggerContext` 字段只在没有 `semantic_result` 时才被使用。

### 审议模式的 `conditional` 判定

在审议式共识里，Judge 可以返回 `conditional`。这表示 Judge 认为有价值，但要求先做出特定修改才能通过。这些条件列在 `JudgmentResult.conditions` 里。**在流水线中 `conditional` 被当作拒绝处理**（`DeliberationResult.approved == False`）。这些条件应该作为可执行的反馈呈现给用户；它们出现在 `evaluation.stage3.completed` 事件负载的 `votes` 列表中。

### 覆盖率分数的解析

Stage 1 从 `pytest-cov` 的输出里按 `TOTAL  N  N  XX%` 或 `Coverage: XX%` 的模式解析覆盖率。如果你的覆盖率工具输出的是别的格式，`coverage_score` 会是 `None`，并且**即使覆盖率是零，coverage 检查也会通过**。请配置一个格式兼容的覆盖率命令，或者查事件负载里的 `coverage_score` 字段来确认解析是否成功。

### 并行共识的失败容忍

在**简单共识**下，只要至少有 2 个模型成功响应，个别模型的失败是被容忍的。`majority_ratio` 只在收集到的票上计算（`approving / len(votes)`），而不是按配置的模型数量。这意味着：
- 2 个模型响应，1 个通过 → `majority_ratio = 0.5` → **拒绝**（低于 0.66）
- 2 个模型响应，都通过 → `majority_ratio = 1.0` → **通过**

在**审议式共识**下，Advocate 和 Judge 两个角色必须成功完成——任一失败都会让 Stage 3 返回错误。Devil's Advocate 角色在内部消化 LLM 错误（返回一张反对票而非向上传播错误），所以单是 Devil 模型失败不会中止 Stage 3。

---

## 完整配置参考

```python
from ouroboros.evaluation.pipeline import PipelineConfig
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.semantic import SemanticConfig
from ouroboros.evaluation.consensus import ConsensusConfig
from ouroboros.evaluation.trigger import TriggerConfig

config = PipelineConfig(
    # 启用 / 关闭各阶段
    stage1_enabled=True,
    stage2_enabled=True,
    stage3_enabled=True,

    # Stage 1：机械验证
    mechanical=MechanicalConfig(
        coverage_threshold=0.7,       # NFR9 最低要求；0.0 表示关闭阈值
        lint_command=("ruff", "check", "."),
        build_command=None,           # None = 跳过这项检查
        test_command=("pytest", "tests/"),
        static_command=("mypy", "src/"),
        coverage_command=("pytest", "--cov=src", "--cov-report=term-missing", "tests/"),
        timeout_seconds=300,          # 单条命令超时
        working_dir=None,             # 默认为进程 cwd
    ),

    # Stage 2：语义评估
    semantic=SemanticConfig(
        model=None,                  # None = 由 semantic_evaluation 角色解析
        temperature=0.2,
        max_tokens=2048,
        satisfaction_threshold=0.8,  # 目前不生效 —— 关卡硬编码为 0.8
    ),

    # Stage 3：简单共识评估
    #
    # models=None 保留后端感知的默认解析：sentinel 后端得到 ("default",) * 3，
    # 非 sentinel 后端保留出厂的 OpenRouter 名单。**非 sentinel 不等于会走
    # OpenRouter** —— 所有投票仍旧使用之后交给 ConsensusEvaluator 的那一个适配器。
    # 显式写死一个 tuple 会设置 models_are_explicit 并跳过上述归一化，因此下面
    # 这组 id 只在「当前适配器确实能路由它们」时才正确；在本地或 sentinel 后端上
    # 反而可能退回同会话模型，或送出该后端不支持的模型标签。
    consensus=ConsensusConfig(
        models=None,  # 或者，仅当活动适配器能路由这些 id 时：
        # models=(
        #     "openrouter/openai/gpt-4o",
        #     "openrouter/anthropic/claude-opus-4.8",
        #     "openrouter/google/gemini-2.5-pro",
        # ),
        temperature=0.3,
        max_tokens=1024,
        majority_threshold=0.66,     # 需要 2/3 多数
        diversity_required=True,     # 目前不生效
    ),

    # 共识触发阈值
    trigger=TriggerConfig(
        drift_threshold=0.3,         # stage2 的 drift_score 高于此值触发 Stage 3
        uncertainty_threshold=0.3,   # stage2 的 uncertainty 高于此值触发 Stage 3
    ),
)
```

审议式共识（独立于 `EvaluationPipeline`）：

```python
from ouroboros.evaluation.consensus import DeliberativeConfig, DeliberativeConsensus

deliberative_config = DeliberativeConfig(
    advocate_model="openrouter/anthropic/claude-opus-4.8",  # Advocate 角色
    devil_model="openrouter/openai/gpt-4o",                 # Devil's Advocate（本体论分析）
    judge_model="openrouter/google/gemini-2.5-pro",         # 最终判定
    temperature=0.3,
    max_tokens=2048,
)
# 直接使用，不经由 EvaluationPipeline
evaluator = DeliberativeConsensus(llm_adapter, deliberative_config)
result = await evaluator.deliberate(context, trigger_reason="seed_drift_alert")
```

---

## 事件审计轨迹

每个阶段都会向 SQLite 事件存储发出事件。用它们可以还原任何一次评估到底发生了什么：

| 事件类型 | 何时发出 | 关键负载字段 |
|---|---|---|
| `evaluation.stage1.started` | Stage 1 开始 | `checks_to_run` |
| `evaluation.stage1.completed` | Stage 1 结束 | `passed`、`checks`、`coverage_score`、`failed_count` |
| `evaluation.stage2.started` | Stage 2 开始 | `model`、`current_ac` |
| `evaluation.stage2.completed` | Stage 2 结束 | `score`、`ac_compliance`、`goal_alignment`、`drift_score`、`uncertainty` |
| `evaluation.consensus.triggered` | 触发矩阵命中 | `trigger_type`、`trigger_details` |
| `evaluation.stage3.started` | Stage 3 开始 | `models`、`trigger_reason` |
| `evaluation.stage3.completed` | Stage 3 结束 | `approved`、`votes`、`majority_ratio`、`disagreements` |
| `evaluation.pipeline.completed` | 整条流水线跑完 | `final_approved`、`highest_stage`、`failure_reason` |

查询某次执行的事件：

```bash
uv run ouroboros status execution <exec_id> --events
```

---

## 延伸阅读

- [evaluation-pipeline.md](./evaluation-pipeline.md) —— 本文的英文原文
- [隐藏清单收敛（Hidden-Checklist Convergence）](../hidden-checklist-convergence/README.zh-CN.md) —— run → 评估 → 有预算的 Ralph 链，以及判分用的断言为什么对 worker 无条件隐藏
- [Architecture Guide](../architecture.md)（英文）—— 六阶段架构里的 Phase 4
- [Seed Authoring Guide](./seed-authoring.md)（英文）—— 验收标准写得好，AC 未满足的情况就少
- [Getting Started](../getting-started.md)（英文）—— 新用户的首次上手流程
- [Config Reference](../config-reference.md)（英文）—— 模型覆写用的环境变量（`OUROBOROS_SEMANTIC_MODEL`、`OUROBOROS_CONSENSUS_MODELS`）
