# DeepSeek Harness 集成指南（DeepSeek Harness integration）

> 这是 DeepSeek Harness 集成指南的中文版。英文原文在同一目录下：[deepseek-harness.md](./deepseek-harness.md)。
> 两份文档描述同一套实现；如果发现不一致，以英文原文和仓库实现为准。

Ouroboros 与 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）通过两个相互独立的方向连接。按你希望所处的位置二选一：

| 你想…… | 方向 | 配置方式 |
|---|---|---|
| 在 **dsh 对话**里工作，并从那里调用 Ouroboros | dsh → Ouroboros | [安装插件](#dsh--ouroboros-插件) |
| 在 **Ouroboros** 里工作，让 DeepSeek 的模型回答 interview/Seed/QA 调用 | Ouroboros → dsh | [配置 `dsh` LLM 后端](#ouroboros--dsh-llm-后端) |

它们在运行时互不相关。安装插件不会改变 Ouroboros 使用哪个 LLM，选择 `dsh` 后端也不会把任何东西装进你的 dsh profile。

---

## dsh → Ouroboros 插件

一条命令，Ouroboros 的所有 MCP 工具就会出现在你的 dsh profile 里：

```sh
dsh plugin --profile <your-profile> add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"
```

`--profile` 是 `dsh plugin` 的必填项；它指定要安装进哪个 profile。之后照常启动（`dsh --profile <your-profile>`），而 `dsh --profile <your-profile> --dump-config` 会显示一个 `# == dsh-ouroboros` 层。

然后直接在对话里输入你想做的事：

```
ooo interview I want a CLI that keeps my dotfiles in sync
ooo auto add retry-with-backoff to the fetch layer
```

模型会自己找到匹配的 `mcp__ouroboros__*` 工具——36 个工具各自都带描述，所以不需要额外提示。

### 它需要什么

- `PATH` 上有 [`uv`](https://astral.sh/uv)。这是唯一的前置条件：插件会拉起 `uvx --from 'ouroboros-ai[mcp]' ouroboros mcp serve`，首次启动时把 Ouroboros 取进一个隔离环境。
- 如果你想让 `ooo auto` 执行它的执行步骤，需要一个可执行的 agent runtime（`claude-cli`、`codex`、`opencode`、……）。要么先跑一次 `ouroboros setup`，要么导出 `OUROBOROS_AGENT_RUNTIME`。MCP 子进程无法承载进程内的 `claude` SDK runtime，所以这里必须用可执行的。

### 凭据不会隐式传递

dsh 会按设计把子进程环境里所有凭据形状的变量——任何匹配 `/KEY|PASSWORD|SECRET|TOKEN/i` 的——全部擦除，以免 harness 的凭据泄漏进被拉起的程序。插件的显式 `env` 层在擦除**之后**才合并，这是凭据到达 `ouroboros mcp serve` 的唯一途径。

因此 bundle 只转发一份刻意精简的允许清单，而不是转发一切：

- `ANTHROPIC_API_KEY` —— Ouroboros 的默认 LLM 后端
- `DEEPSEEK_API_KEY` —— 下文的 `dsh` 后端回环（loopback）

要再多转发一个（`OPENAI_API_KEY`、`OPENROUTER_API_KEY`、……），就在你自己 profile 的 `cordis.patch.yml` 里覆写 `mcp-ouroboros` 这一行，把那个额外的名字放进 `env`。后加的 patch 层会**整体替换**一行的整个 `config`，而不是深度合并，所以把 bundle 的 `config` 块复制过来，再加上你的一行。所有非凭据形状的东西——`PATH`、`HOME`、`OUROBOROS_*` 选择器——原样通过，完全不需要条目。

### 启动失败时

启动失败是非致命的（`failOnStartupError: false`）：没有 `uv` 的机器仍然能启动 dsh，其他插件照常工作，只是没有 Ouroboros 工具。恢复一般不是自动的——`mcp-client` 是否重试取决于你的 dsh 构建，而且就算存在重连循环，也会在有限次尝试后放弃。修好原因之后，重载插件或重启 dsh。

完整 bundle 参考：[`integrations/dsh-plugin/README.md`](../../integrations/dsh-plugin/README.md)。

---

## Ouroboros → dsh LLM 后端

`dsh` 是一个**纯 LLM** 后端：它回答 interview、Seed 撰写和 QA 调用。它不能用作 `orchestrator.runtime_backend`，因为 dsh 的 ACP 表面刻意做成纯文本、fresh-session——对补全（completions）合适，对会使用工具的执行 runtime 则不合适。

选它不是拨一个变量开关那么简单。Ouroboros 会拉起**它自己的** `dsh-acp-demo` 子进程，而那个子进程在你给它一个可加载的 composition 之前会 fail closed，报 `invalid_config`。

### 1. 获取 ACP server 二进制

从源码构建 DeepSeek Harness（`pnpm install && pnpm run build`，Node.js >= 22），并让它的 `dsh-acp-demo` bin 可达——放进 `PATH`，或者用 `OUROBOROS_DSH_CLI_PATH` / `orchestrator.dsh_cli_path` 指名。安装已发布的 `@deepseek-ai/dsh-acp-demo` 包目前仍会在它自己的 `dsh-tool-bash` 依赖链上遇到 peer-dependency 冲突，所以源码构建是当前可用的路径。

### 2. 指向一个 composition

```sh
export OUROBOROS_DSH_CONFIG_PATH=/absolute/path/to/cordis.yml
```

客户端强制或继承的两条规则：

- **只接受绝对路径。** 相对路径会被有意拒绝——它会相对于不可信的项目 cwd 解析，而 composition 决定 Node 进程加载哪些插件，这等于代码执行。
- **它必须位于 dsh 的 `node_modules`（或 workspace）可达的地方。** composition 里的插件包名相对于 composition 文件自身的目录解析。

`OUROBOROS_DSH_CLI_PATH` 和 `OUROBOROS_DSH_CONFIG_PATH` 都在项目 `.env` 的拒绝清单（denylist）上，原因相同：被 checkout 的仓库不得重定向 Ouroboros 要加载哪个可执行文件或插件树。请在你的 shell 里或 `~/.ouroboros/config.yaml` 里设置它们。

### 3. 提供 composition 里指名的凭据

通常是 `DEEPSEEK_API_KEY`。被拉起的子进程继承你的环境，减去 Ouroboros 的选择器变量（这样 dsh 里嵌套的 `ooo` 就不会搞混自己在用哪个后端）。

### 4. 选择后端

```sh
ouroboros mcp serve --runtime claude-cli --llm-backend dsh
# or
export OUROBOROS_LLM_BACKEND=dsh
```

这里 `--runtime` 不是可选项。`dsh` 只回答纯 LLM 的调用，所以执行 runtime 仍然是另一个独立选择——而 MCP 2 server 会拒绝 SDK 支持的 `claude` / `claude-sdk` 默认值，所以要指定一个可执行的（`claude-cli`、`codex`、`opencode`、……）。

或者持久化到 `~/.ouroboros/config.yaml`：

```yaml
llm:
  backend: dsh
orchestrator:
  dsh_cli_path: /absolute/path/to/dsh-acp-demo
  dsh_config_path: /absolute/path/to/cordis.yml
```

`deepseek_harness` 作为 `dsh` 的别名，只在运行时解析后端名的地方被接受——`OUROBOROS_LLM_BACKEND=deepseek_harness` 和编程方式 `create_llm_adapter(backend="deepseek_harness")`。类型化表面会拒绝它：`--llm-backend deepseek_harness` 过不了参数校验，`llm.backend: deepseek_harness` 过不了配置校验。在 CLI 和 `config.yaml` 里请写 `dsh`。

### 模型选择

ACP 线上不携带模型参数——Cordis composition 拥有 provider 和模型。Ouroboros 如实报告 `dsh-composition` 哨兵值（sentinel），而不是编造一个模型名，并且因为协议不返回用量计数而报告零 token 用量。要换模型，就改 composition。

---

## 同时使用两者

两者可以组合：装上插件，让 dsh 对话可以驱动 Ouroboros；再设置 `OUROBOROS_LLM_BACKEND=dsh`，让返回的 interview 问题由 DeepSeek 自己的模型撰写。这个回环正是 bundle 显式转发 `DEEPSEEK_API_KEY` 的原因——没有它，工具列表一切正常，而第一次调用就会失败。

注意，回环会拉起第二个 `dsh-acp-demo` 进程；它不会复用你正在对话的那个 dsh，所以上面的步骤 1–3 仍然适用。

---

## 延伸阅读

- [deepseek-harness.md](./deepseek-harness.md) —— 本文的英文原文
- [`integrations/dsh-plugin/README.md`](../../integrations/dsh-plugin/README.md) —— 完整 bundle 参考（英文）
- [Getting Started](../getting-started.md)（英文）—— 新用户的上手流程
