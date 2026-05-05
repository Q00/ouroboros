<p align="right">
  <a href="./README.md">English</a> | <a href="./README.ko.md">한국어</a> | <strong>简体中文</strong>
</p>

<p align="center">
  <br/>
  ◯ ─────────── ◯
  <br/><br/>
  <img src="./docs/images/ouroboros.png" width="520" alt="Ouroboros">
  <br/><br/>
  <strong>O U R O B O R O S</strong>
  <br/><br/>
  ◯ ─────────── ◯
  <br/>
</p>


<p align="center">
  <strong>别再堆提示词，先把规约写清楚。</strong>
  <br/>
  <sub>面向 AI 编码工作流的 Agent OS —— 可重放、规约优先</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#为什么选-ouroboros">为什么</a> ·
  <a href="#你能得到什么">效果</a> ·
  <a href="#循环">运作原理</a> ·
  <a href="#命令">命令</a> ·
  <a href="#从-wonder-到本体论">理念</a>
</p>

**把一个模糊的想法，跨 Claude Code、Codex CLI、OpenCode、Hermes 变成一份经过验证、可运行的代码库。**

Ouroboros 是面向 AI 编码的 Agent OS：一层本地优先的运行时，把非确定性的 agent 工作转换成一份可重放、可观测、受策略约束的执行契约。它用一套结构化的、规约优先的工作流取代东拼西凑的 prompt：访谈、定型、执行、评估、演化。

---

## 为什么选 Ouroboros？

绝大多数 AI 编码失败在**输入**，不在输出。瓶颈不是 AI 能力不够，而是人没把事情想清楚。

| 问题            | 实际发生的情况          | Ouroboros 的解法                        |
| :-------------- | :---------------------- | :-------------------------------------- |
| 提示词太模糊    | AI 靠猜，你不停返工     | 苏格拉底式访谈把隐藏的假设挖出来        |
| 没有规约        | 写到一半架构开始飘      | 不可变的 seed 规约在写代码前先锁住意图  |
| 全靠手工 QA     | "看起来还行"不算验证    | 三阶段自动评估关卡                      |

---

## 快速开始

**安装** —— 一条命令，环境自动识别：

```bash
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
```

**开始** —— 打开你的 AI 编码 agent，直接上：

```
> ooo interview "I want to build a task management CLI"
```

> 支持 Claude Code、Codex CLI、OpenCode、Hermes。安装脚本会自动检测 Claude Code、Codex CLI 和 Hermes CLI 并注册 MCP server。OpenCode 用户在安装后运行 `ouroboros setup --runtime opencode` 即可。

<details>
<summary><strong>其他安装方式</strong></summary>

**仅安装 Claude Code 插件**（不装系统包）：
```bash
claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros
```
然后在 Claude Code 会话里跑一次 `ooo setup`。

**pip / uv / pipx**：
```bash
pip install ouroboros-ai                # 基础
pip install ouroboros-ai[claude]        # + Claude Code 依赖
pip install ouroboros-ai[litellm]       # + LiteLLM 多 provider
pip install ouroboros-ai[mcp]           # + MCP server / client 支持
pip install ouroboros-ai[tui]           # + Textual 终端 UI
pip install ouroboros-ai[all]           # 全部 (claude + litellm + mcp + tui + dashboard)
ouroboros setup                         # 配置运行时
```

历史兼容：在 extras 迁移期间，`ouroboros-ai[dashboard]` 仍然作为兼容别名保留。

各运行时指南：[Claude Code](./docs/runtime-guides/claude-code.md) · [Codex CLI](./docs/runtime-guides/codex.md) · [Hermes](./docs/runtime-guides/hermes.md) · [OpenCode](./docs/runtime-guides/opencode.md)

</details>

<details>
<summary><strong>卸载</strong></summary>

```bash
ouroboros uninstall
```

清掉所有配置、MCP 注册和数据。详情见 [UNINSTALL.md](./UNINSTALL.md)。

</details>

> **需要 Python >= 3.12**。完整依赖见 [pyproject.toml](./pyproject.toml)。

---

## 你能得到什么

跑完一轮 Ouroboros 循环之后，一个模糊的想法会变成一份经过验证的代码库：

| 阶段          | 之前                  | 之后                                                                  |
| :------------ | :-------------------- | :-------------------------------------------------------------------- |
| **Interview** | *"帮我做个 task CLI"* | 12 条隐藏假设被挖出来，模糊度打分到 0.19                              |
| **Seed**      | 没规约                | 不可变规约：明确写出验收标准、本体、约束                              |
| **Evaluate**  | 人肉 review           | 三阶段关卡：Mechanical（免费）→ Semantic → Multi-Model Consensus      |

<details>
<summary><strong>刚才发生了什么？</strong></summary>

```
interview  ->  苏格拉底式提问揭示了 12 条隐藏假设
seed       ->  把回答凝结成不可变规约（Ambiguity: 0.15）
run        ->  按 Double Diamond 分解执行
evaluate   ->  三阶段验证：Mechanical -> Semantic -> Consensus
```

> 在你的 AI 编码 agent 会话里用 `ooo <cmd>`，或者在终端里直接用 `ouroboros init start`、`ouroboros run seed.yaml` 等命令。

衔尾蛇完成了一次循环。每一圈，它都比上一圈知道得更多。

</details>

---

## 与现有方案对比

AI 编码工具本身很强 —— 但当输入不清晰时，它们解的是**错的问题**。

|                | 普通 AI 编码                     | Ouroboros                                                                       |
| :------------- | :------------------------------- | :------------------------------------------------------------------------------ |
| **模糊提示词** | AI 自己猜意图，基于假设往下做    | 苏格拉底式访谈在写代码*之前*强制澄清                                            |
| **规约校验**   | 没有规约 —— 写到一半架构开始飘   | 不可变的 seed 规约锁住意图；模糊度门槛（≤ 0.2）会拦下提前进入 code 的尝试       |
| **评估**       | "看起来还行" / 人肉 QA           | 三阶段自动关卡：Mechanical → Semantic → Multi-Model Consensus                   |
| **返工率**     | 高 —— 错误假设到后期才暴露       | 低 —— 假设在访谈阶段就暴露，而不是等到 PR review                                |

---

## 循环

衔尾蛇 —— 一条吞食自己尾巴的蛇 —— 不是装饰。它*就是*这个架构本身：

```
    Interview -> Seed -> Execute -> Evaluate
        ^                           |
        +---- Evolutionary Loop ----+
```

每一次循环不是简单重复 —— 它在**演化**。评估阶段的输出会作为下一代的输入，直到系统真正知道自己在做什么。

| 阶段          | 做什么                                                                |
| :------------ | :-------------------------------------------------------------------- |
| **Interview** | 用苏格拉底式提问揭示隐藏假设                                          |
| **Seed**      | 把回答凝结成一份不可变规约                                            |
| **Execute**   | Double Diamond：Discover → Define → Design → Deliver                  |
| **Evaluate**  | 三阶段关卡：Mechanical（$0）→ Semantic → Multi-Model Consensus        |
| **Evolve**    | Wonder *("我们还有什么没搞清楚？")* → Reflect → 进入下一代            |

> *"这就是衔尾蛇吞食尾巴的地方：评估的输出，*
> *变成下一代 seed 规约的输入。"*
> —— `reflect.py`

当本体相似度 ≥ 0.95 时收敛 —— 系统已经把自己问得足够清楚了。

### Ralph：永不停歇的循环

`ooo ralph` 跨会话边界持续地跑这个演化循环，直到收敛为止。每一步都是**无状态**的：EventStore 会重建完整的演化谱系，所以即便机器重启，衔尾蛇也能从断点继续。

```
Ralph Cycle 1: evolve_step(lineage, seed) -> Gen 1 -> action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       -> Gen 2 -> action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       -> Gen 3 -> action=CONVERGED
                                                +-- Ralph 停止。
                                                    本体已经稳定。
```

---

## 命令

在 AI 编码 agent 会话里用 `ooo <cmd>` 技能，在终端里用 `ouroboros` CLI。

| 技能（`ooo`）        | 等效 CLI                                                          | 作用                                                          |
| :------------------- | :---------------------------------------------------------------- | :------------------------------------------------------------ |
| `ooo setup`          | `ouroboros setup`                                                 | 注册运行时并配置项目（一次性）                                |
| `ooo interview`      | `ouroboros init start`                                            | 苏格拉底式提问 —— 把隐藏假设挖出来                            |
| `ooo auto`           | `ouroboros auto`                                                  | 从一个目标 → A 级 Seed → 在有界循环里完成执行交接             |
| `ooo seed`           | *(由 interview 生成)*                                             | 凝结为不可变规约                                              |
| `ooo run`            | `ouroboros run seed.yaml`                                         | 用 Double Diamond 分解执行                                    |
| `ooo evaluate`       | *(经由 MCP)*                                                      | 三阶段验证关卡                                                |
| `ooo evolve`         | *(经由 MCP)*                                                      | 演化循环，直到本体收敛                                        |
| `ooo unstuck`        | *(经由 MCP)*                                                      | 卡住时，5 个横向思维人格替你换个角度                          |
| `ooo status`         | `ouroboros status executions` / `ouroboros status execution <id>` | 会话跟踪 +（仅 MCP）漂移检测                                  |
| `ooo resume-session` | `ouroboros resume`                                                | 列出进行中的会话并给出重新接入命令                            |
| `ooo cancel`         | `ouroboros cancel execution [<id>\|--all]`                        | 取消卡住或孤儿态的执行                                        |
| `ooo ralph`          | *(经由 MCP)*                                                      | 持续循环直到通过验证                                          |
| `ooo tutorial`       | *(交互式)*                                                        | 交互式动手学习                                                |
| `ooo help`           | `ouroboros --help`                                                | 完整命令参考                                                  |
| `ooo pm`             | *(经由 MCP)*                                                      | 面向 PM 的访谈 + PRD 生成                                     |
| `ooo qa`             | *(经由 skill)*                                                    | 通用 QA 评判，可用于任意产物                                  |
| `ooo update`         | `ouroboros update`                                                | 检查更新 + 升级到最新版                                       |
| `ooo brownfield`     | *(经由 skill)*                                                    | 扫描并管理 brownfield 仓库 / worktree 默认值                  |
| `ooo publish`        | *(skill / 运行时；底层用 `gh` CLI)*                               | 把 Seed 发布成 GitHub Epic / Task issue，用于团队协作         |

> 不是所有技能都有直接对应的 CLI 子命令。其中一些（`evaluate`、`evolve`、`unstuck`、`ralph`、`publish`）通过 agent 技能、运行时规则或 MCP 工具暴露，而不是 `ouroboros <subcommand>` 这种 shell 命令。
> `/resume` 是 Claude Code 内置的会话选择器保留指令；要恢复 Ouroboros 进行中的会话，请使用 `ooo resume-session`。

完整细节见 [CLI 参考](./docs/cli-reference.md)。

---

## 九种心智

九个 agent，每一个对应一种思维模式。按需加载，不预加载：

| Agent                    | 角色                       | 核心问题                                       |
| :----------------------- | :------------------------- | :--------------------------------------------- |
| **Socratic Interviewer** | 只问问题。从不动手做。     | *"你正在假设什么？"*                           |
| **Ontologist**           | 找本质，不看表象           | *"这东西到底*是*什么？"*                       |
| **Seed Architect**       | 把对话凝结成规约           | *"够完整、够清楚了吗？"*                       |
| **Evaluator**            | 三阶段验证                 | *"我们做出来的，真的是该做的吗？"*             |
| **Contrarian**           | 对每一个假设提出质疑       | *"如果反过来呢？"*                             |
| **Hacker**               | 找非常规路径               | *"哪些约束其实是真的？"*                       |
| **Simplifier**           | 移除复杂度                 | *"能跑起来的最简方案是什么？"*                 |
| **Researcher**           | 停下编码，去做调查         | *"我们手里到底有什么证据？"*                   |
| **Architect**            | 找结构性根因               | *"如果从头再来，我们还会这么搭吗？"*           |

---

## 内部结构

<details>
<summary><strong>架构总览 —— Python >= 3.12</strong></summary>

```
src/ouroboros/
+-- bigbang/        Interview、模糊度打分、brownfield 探查
+-- routing/        PAL Router —— 三档成本优化（1x / 10x / 30x）
+-- execution/      Double Diamond、分层 AC 分解
+-- evaluation/     Mechanical -> Semantic -> Multi-Model Consensus
+-- evolution/      Wonder / Reflect 循环、收敛判定
+-- resilience/     四种停滞模式检测、5 个横向思维人格
+-- observability/  三要素漂移度量、自动复盘
+-- persistence/    Event sourcing（SQLAlchemy + aiosqlite）、检查点
+-- orchestrator/   运行时抽象层（Claude Code、Codex CLI、OpenCode、Hermes）
+-- core/           类型、错误、seed、本体、安全
+-- providers/      LiteLLM 适配器（100+ 模型）
+-- mcp/            MCP 客户端 / 服务端集成
+-- plugin/         插件系统（技能 / agent 自动发现）
+-- tui/            终端 UI 仪表盘
+-- cli/            基于 Typer 的 CLI
```

**关键内部细节：**
- **PAL Router** —— Frugal（1x）→ Standard（10x）→ Frontier（30x），失败自动升级，成功自动降级
- **Drift** —— Goal（50%）+ Constraint（30%）+ Ontology（20%）加权度量，阈值 ≤ 0.3
- **Brownfield** —— 自动识别多种语言生态的配置文件
- **Evolution** —— 最多 30 代，本体相似度 ≥ 0.95 时收敛
- **Stagnation** —— 检测打转、震荡、无漂移、收益递减四种模式
- **Agent OS runtime** —— 跨能力发现、策略、指令、事件日志、agent 进程的可重放执行契约
- **Runtime backends** —— 可插拔抽象层（`orchestrator.runtime_backend` 配置），原生支持 Claude Code、Codex CLI、OpenCode、Hermes；同一份工作流规约，跑在不同执行引擎上

完整设计文档见 [Architecture](./docs/architecture.md)。

</details>

---

## 从 Wonder 到本体论

<details>
<summary><strong>Ouroboros 背后的哲学引擎</strong></summary>

> *Wonder -> "该怎么活？" -> "'活'到底*是*什么？" -> 本体论*
> —— 苏格拉底

每一个好问题都会带出更深的问题 —— 而那个更深的问题，永远是**本体论**层面的：不是*"我该怎么做？"*，而是*"这东西到底*是*什么？"*

```
   Wonder                          本体论
"我想要什么？"     ->    "我想要的那个东西，到底是什么？"
"做个 task CLI"    ->    "task 是什么？priority 是什么？"
"修一下登录 bug"   ->    "这是根因，还是只是症状？"
```

这不是为了抽象而抽象。当你回答*"task 是什么？"* —— 是可删除还是可归档？单人用还是团队用？—— 你就一次性消除了一整类返工。**本体论问题，恰恰是最实用的问题。**

Ouroboros 通过 **Double Diamond** 把这套思路嵌进了架构里：

```
    * Wonder          * Design
   /  (发散)         /  (发散)
  /    探索          /    创造
 /                 /
* ------------ * ------------ *
 \                 \
  \    定义         \    交付
   \  (收敛)         \  (收敛)
    * 本体论          * 评估
```

第一颗钻石是**苏格拉底式**：先发散成问题，再收敛成清晰的本体。第二颗是**实用层面**：先发散出设计选项，再收敛到经过验证的交付物。每一颗钻石都依赖前一颗 —— 没理解清楚的东西，是设计不出来的。

</details>

<details>
<summary><strong>模糊度分数：Wonder 与代码之间的关卡</strong></summary>

Interview 不会因为你"觉得差不多了"就结束 —— 而是要等**数学**说差不多了才结束。Ouroboros 把模糊度量化为加权清晰度的反值：

```
Ambiguity = 1 - Σ(clarity_i * weight_i)
```

每个维度由 LLM 在 0.0–1.0 区间打分（temperature 设为 0.1 以保证可复现），然后按权重加和：

| 维度                                        | Greenfield | Brownfield |
| :------------------------------------------ | :--------: | :--------: |
| **目标清晰度** —— *目标够具体吗？*          |    40%     |    35%     |
| **约束清晰度** —— *边界定义清楚了吗？*      |    30%     |    25%     |
| **成功标准** —— *结果是可衡量的吗？*        |    30%     |    25%     |
| **上下文清晰度** —— *现有代码库摸清了吗？*  |     —      |    15%     |

**阈值：Ambiguity ≤ 0.2** —— 只有低于这个值，才能生成 Seed。

```
示例（Greenfield）：

  Goal: 0.9 * 0.4  = 0.36
  Constraint: 0.8 * 0.3  = 0.24
  Success: 0.7 * 0.3  = 0.21
                        ------
  Clarity             = 0.81
  Ambiguity = 1 - 0.81 = 0.19  <= 0.2 -> 可以进入 Seed
```

为什么是 0.2？因为在加权清晰度达到 80% 时，剩下的那点不确定性已经小到可以靠代码层面的判断来收尾。再高的话，你还在凭感觉定架构。

</details>

<details>
<summary><strong>本体收敛：衔尾蛇何时停下</strong></summary>

演化循环不会无限跑下去。当连续几代输出本体上等价的 schema 时，循环就停。相似度按 schema 字段加权比较：

```
Similarity = 0.5 * name_overlap + 0.3 * type_match + 0.2 * exact_match
```

| 组件             | 权重 | 衡量什么                                      |
| :--------------- | :--: | :-------------------------------------------- |
| **Name overlap** | 50%  | 两代之间是否有同名字段？                      |
| **Type match**   | 30%  | 共享字段的类型是否一致？                      |
| **Exact match**  | 20%  | 名字、类型、描述是否完全一致？                |

**阈值：Similarity ≥ 0.95** —— 越过这条线，循环就收敛、停止演化。

但相似度不是唯一信号。系统也会检测病态模式：

| 信号               | 条件                                | 含义                          |
| :----------------- | :---------------------------------- | :---------------------------- |
| **停滞**           | 连续 3 代相似度 ≥ 0.95              | 本体已稳定                    |
| **震荡**           | Gen N ≈ Gen N-2（周期为 2 的循环）  | 在两个设计之间反复横跳        |
| **重复反馈**       | 连续 3 代问题重叠率 ≥ 70%           | Wonder 在反复问同一类问题     |
| **硬性上限**       | 达到 30 代                          | 安全阀                        |

```
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     -> similarity 0.78 -> CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     -> similarity 1.00 -> CONVERGED
```

两道数学关卡，一个理念：**没想清楚之前不要写（Ambiguity ≤ 0.2），没稳定之前不要停（Similarity ≥ 0.95）。**

</details>

---

## 参与贡献

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

[Issues](https://github.com/Q00/ouroboros/issues) · [Discussions](https://github.com/Q00/ouroboros/discussions) · [贡献指南](./CONTRIBUTING.md)

---

## Star 历史

<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-light-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=light" alt="Star History Chart" width="100%" />
</a>
<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-dark-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=dark" alt="Star History Chart" width="100%" />
</a>

---

<p align="center">
  <em>"开始即是终结，终结即是开始。"</em>
  <br/><br/>
  <strong>衔尾蛇不会重复 —— 它在演化。</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
