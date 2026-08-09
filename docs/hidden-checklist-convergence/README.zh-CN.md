# 隐藏清单收敛（Hidden-Checklist Convergence）

> 这是设计说明的中文版。完整的英文文档在同一目录下：
> [requirements.md](./requirements.md) · [architecture.md](./architecture.md) · [implementation.md](./implementation.md)。
> RFC 原文：[#1917](https://github.com/Q00/ouroboros/issues/1917)，实现：[#1916](https://github.com/Q00/ouroboros/pull/1916)。

这份改动把 `ooo run` 从「执行一次然后报告」变成一个循环：run → 正式评估 → 有预算上限的 Ralph 演化，全部由一次 `ooo run` 驱动。循环的目标是让 seed 的隐藏清单全部通过，但它是有边界的：收敛、震荡、等级回退、墙钟、世代预算——这些是主要的几条，Ralph 的公开契约里还有迭代超时、取消、终态演化动作、QA 失败等出口。任何一条先触发，循环就会提前停止（见下文「3. 被拒绝的评估进入有预算的 Ralph 循环」）。

## 动机：两个让单次执行不可靠的结构性问题

### 一、答案泄露，等于邀请 reward hacking

worker 曾经能直接看到用来给它判分的 `verify_command` 和 `Expected output: <assertion>`（渲染自 `_build_success_contract_block`）。还有第二条泄露路径更隐蔽：断言的 `repr()` 会顺着 `result.error` 的尾巴，经由重试提示流回下一轮。

一个卡住的 worker 最省力的路径，于是变成糊弄那个断言字符串，而不是实现验收标准本身（参见 `seed_2be2907edc07` 的复盘）。

### 二、失败是死路

verify gate（[#1591](https://github.com/Q00/ouroboros/issues/1591)）、run→eval 链、`evolve_step`、Ralph driver、`focus.select_evolution_focus`——每个零件都已经存在，但没有任何东西把它们连起来。失败没有被消化，只是以 BLOCKED 的形式上报。

## 三条已确认的原则

1. **无条件隐藏。** worker 只能看到验收标准的描述和预期产物。`verify_command` / `output_assertion` 永不展示。不提供任何披露级别的配置项（该提案被显式否决）。这里的「无条件」指的是**没有配置项可以把它打开**，而不是说存在一个文件系统沙箱——保密边界的确切范围见下文「保密边界的范围」。
2. **用提示循环替代公开答案。** 验证失败时，下一轮的指令根据这次会话实际做了什么来修正——工具调用轨迹、证据清单、验证器结论——并且提示本身也要过滤掉断言字符串。
3. **run → eval → evolve 是一条链。** 只有未通过的验收标准进入聚焦重执行（第 2 代起），已通过的保持冻结。BLOCKED 只在演化预算耗尽后作为最后手段出现。

## 设计

### 1. 按 AC 隐藏答案（两条路径都堵）

- **正向**：`_build_success_contract_block` 现在只渲染描述和 `expected_artifacts`，验证由 harness 独立完成。
- **反向**：verify gate 的失败原因不再内嵌断言的 `repr()`。重试提示改由断言安全的构造器（`orchestrator/retry_hints.py`）生成，它复用只读的证据清单（`deliver_gate.load_ac_evidence_manifest`），并从每一个片段中过滤断言字符串，**包括那段 2000 字符的命令输出尾巴**。

#### 保密边界的范围（重要）

这个保密边界覆盖的是 **Ouroboros 自己传递给 worker 的那些数据**：worker prompt、context、事件、产物、以及重试面。在这个范围内，验证器的键会被移除，它们的值——原样的、被引号包裹的、被转义的——都会由与重试提示同一个「最长优先」的合约脱敏器抹掉。

**它不是文件系统沙箱。** 如果操作者把原始 seed 文件放在 worker 的 Read / Bash 能力够得着的位置，worker 仍然可以自己发现那个文件。因此，真正强的 holdout 隔离还需要额外做一件事：把原始 seed 放到 worker 可见的工作区之外，或者给 worker 套一个合适的文件系统沙箱。

（英文正文对应处：[`requirements.md`](./requirements.md) 的 Clarified Specification 第一条，以及 [`implementation.md`](./implementation.md) 的 confidentiality boundary 段落。）

### 2. 失败的 run 也进入评估

放宽 `_run_succeeded` 的门槛：**只要 run 留下了可评估的证据**，即使 AC 执行失败，也会链接到正式的 evaluate 作业。「可评估」有明确定义（`is_evaluable_run_result()`）：终态是 `completed` 或 `failed`，并且文本内容非空。暂停（paused）和取消（cancelled）的会话被显式排除，不会入队。保持 fail-open——入队失败绝不反过来改写 run 的结论。`deadline_seconds` 现在显式传入（0 表示无限等待，该陷阱已在文档中说明）。

### 3. 被拒绝的评估进入有预算的 Ralph 循环

收敛循环**没有被重新实现**，而是委派给已有的演化机制：

- evaluate 作业的终态路径在 `final_approved is False` 时入队 `ouroboros_start_ralph`。
- **Gen1 bridge（真正新增的部分）**：把 run 的 seed 和链式评估的多 AC 清单投影成谱系事件（`lineage_created` + 带 `seed_json` 和完整 `EvaluationSummary` 的 `lineage_generation_completed`），使 `evolve_step` 把这次普通的 run 当作第 1 代回放，并从第 2 代开始带着焦点继续。
- 新的 checklist→`ACResult` 转换器（`mcp/tools/evaluate_ralph_chain.py`）满足 `validate_seed_ac_coverage` 的严格要求：完整的索引覆盖、逐字一致的 `ac_content`、`semantic_ac_key` 同一性。这正是 `focus.select_evolution_focus` 能够冻结已通过 AC 的前提。
- Ralph 已有的停止条件为循环设定边界，主要的几条是：QA 通过、收敛、震荡、等级回退、墙钟；此外还有迭代超时、取消和终态演化动作。`execution.auto_evolve_max_generations` 默认为 3（钳制在 1..10）。

## 保障与决策

- **幂等与持久性**：谱系标识使用完整规范元组 `(seed_id, session_id)` 的 SHA-256 的 120 位，产生 36 字符的键以适配生产事件 schema。第 1 代发布使用持久化的谱系认领加上原子 `append_batch`；并发的 Ralph 入队使用独立的持久化单胜者认领，其回执发布权威的 `job_id`。
- **诚实的评估深度**：多 AC 元数据记录每条 AC 流水线完成的最高阶段（保守聚合），第 1 代投影该值而不是虚构一个固定的拒绝阶段。
- **不递归**：演化世代在进程内评估（绝不经由 `StartEvaluateHandler`），外加 `delegation_depth` 拒绝。
- **auto 流水线保护**：`ooo auto` 以 `auto_evolve: false` 派发执行——auto 拥有自己的 RALPH_HANDOFF 谱系；没有这个保护，auto 会话内一次失败的链式评估会与另一条谱系上的第二个 Ralph 循环竞争。
- **降级**：单 AC 评估（无清单）以空 `ac_results` 继续（只有一条 AC 时冻结没有意义）。seed 无法解析则 fail-closed 跳过 Ralph 链（第 2 代重建必须依赖 `seed_json`）。
- **修复静默的评估丢失**：`evolution/loop.py` 的裸 `except`（三条静默通往 `evaluation_summary=None` 的路径）现在会记录一份带失败原因的 rejected summary，既保持 fail-closed 的焦点语义，又让失败成为持久化的事实。
- **行为变更**：`execution.auto_evolve` 默认为 `true`。正式评估被拒绝的普通 `ooo run`，现在会**尝试**自动接上演化链，最多 3 个聚焦世代。这是尝试而不是保证：seed 解析失败或 Ralph 入队失败时，一个世代都不会启动；单 AC 评估走的是全图聚焦而不是逐 AC 的聚焦世代（这两种降级见上面两条）。链路被跳过或失败时，**先前的评估结论仍然是权威的**——fail-open 的方向始终是不改写已有判定。可按调用退出（`auto_evolve: false`）或通过配置关闭。

## 不在本次范围内

- 披露级别配置项（显式否决，隐藏没有配置开关）
- 文件系统沙箱本身（本 RFC 只管 Ouroboros 传递的数据，见上文「保密边界的范围」）
- 对重试指令做 LLM「教练式改写」（先交付确定性提示，观察效果后再议）
- 单 AC 评估的元数据增强（链式路径实际上都是多 AC）

## 与既有决策的关系

- PR #174 得出的结论是「评估深度优先于信息不对称」，那是**评估器层**的决定；本 RFC 移除的是**执行 worker 层**的答案暴露。层次不同，不冲突。
- 直接建立在 [#1591](https://github.com/Q00/ouroboros/issues/1591)（verify gate 的权威属于 orchestrator）和 reflect 聚焦重执行（`externally_satisfied_acs`）之上。
