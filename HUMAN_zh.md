# Hermes Agent 开发指南（中文完整版）

> 本文是 `AGENTS.md`（英文权威开发指南）的完整中文版，面向人类阅读，方便快速理解项目。
> 内容覆盖：架构、贡献准则、plugin / skill 体系、配置、测试、已知陷阱等全部章节。
> **当英文 `AGENTS.md` 与本文出现差异时，以 `AGENTS.md` 为准**（它是自动化流程实际读取的文件）。
> 文末附「当前任务：企业微信多租户改造」一节，方便你随时回到主线。

**永远不要在正确的方案上轻易放弃。**

---

## 一、Hermes 是什么

Hermes 是一个**个人 AI agent**，它用同一套 agent 内核同时运行在：

- CLI（交互式命令行）
- 消息网关（Telegram、Discord、Slack，以及约 20 个其他平台，包括企业微信）
- TUI（基于 Ink/React 的终端 UI）
- Electron 桌面 App

它能跨会话学习（memory + skills）、把任务委派给子 agent（subagent）、运行定时任务（cron），并能真正驱动一个终端和浏览器。它主要通过 **plugin 和 skill** 来扩展，而不是把内核做大。

有两条设计铁律，几乎决定了每一个设计取舍，也是 review 任何改动的视角：

1. **每轮会话 prompt 缓存神圣不可破。**
   一个长会话每一轮都复用一段缓存好的前缀（cached prefix）。任何「改动过去的上下文 / 中途切换工具集 / 中途重建 system prompt」的行为，都会让缓存失效，从而成倍放大用户的成本。我们绝不这么做（唯一例外是上下文压缩 context compression）。

   > 给初学者的解释：可以把 prompt 缓存理解成"上次算过的开头部分，这次直接拿来用"。只要开头一个字都没变，就能省钱省时间；一旦你在中间偷偷改了一个字，整段都得重算。

2. **内核是一条"窄腰"，能力长在边缘。**
   我们加的每一个 model tool，都会随**每一次** API 调用一起发送出去。所以新增一个**核心工具**的门槛非常高。绝大多数新能力应该以「CLI 命令 + skill」「service-gated 工具」「plugin」的形式到达，而不是变成核心工具的一部分。

---

## 二、贡献准则——我们想要什么 / 不想要什么

这是项目的"意图层"，有两种用法：

1. **对人类、对你自己的工作**——什么会被合并、什么会被拒绝，让贡献对准靶心。
2. **对自动化 review（triage sweeper）**——指导何时可以安全地以三个允许的理由关闭 PR（`implemented_on_main` 已在主线实现、`cannot_reproduce` 无法复现、`incoherent` 不自洽），以及同样重要的——**何时不该关闭**。基于品味的"我们不想要 / 超出范围"这类关闭**不是**自动化能做的决定，要留给人类维护者。

要把分寸理解对：Hermes 出货量**很大**——大多数合并都是针对真实上报行为的 bug 修复，而产品面（平台、渠道、provider、模型、桌面/TUI 功能）是有意地、激进地扩张的。下面所讲的"克制"，瞄准的是**核心 agent + model 工具 schema**，也就是那个"每次 API 调用都要付费"的唯一地方。"最小足迹"管的是**一个能力如何接入内核**，而**不是**产品能不能长大。我们在边缘扩张，在窄腰保守。

### 我们想要的

- **把真实 bug 修好。** 落地的大部分是针对真实上报症状的 `fix(...)`。一个好的修复：在当前 `main` 上复现症状、指出它具体在哪一行发作、并修掉整个 bug 类别（包括同类的兄弟调用路径），而不只是修上报者碰到的那一处。
- **在边缘扩张产品的触达面。** 新平台适配器、渠道、provider、模型、桌面/TUI/dashboard 功能都欢迎，且经常落地（包括很大的，比如一个新消息渠道、一个 session 上限功能、一个 Windows PTY 桥）。产品的广度是目标，不算足迹问题——只要它接入现有的 setup/config 体验（`hermes tools`、`hermes setup`、自动安装），而不是直接糊一个原始 env var。
- **把"上帝文件"重构成干净模块。** 从 `cli.py` / `run_agent.py` / `gateway/run.py` 里抽出几千行的聚簇，做成聚焦的 mixin 或模块，这是想要的工作，即使 diff 巨大且机械（大的 `+N/-N` 重构会定期合并）。"每行都能追溯到需求"这条标准针对的是**功能** PR；一个声明过的重构，它的"需求"就是这次抽取本身。
- **保持内核窄。** 新增 *model 工具* 是昂贵的例外——每个工具随每次 API 调用出货。优先级从高到低：扩展现有代码 → CLI 命令 + skill → service-gated 工具（`check_fn`）→ plugin → MCP catalog 里的 MCP server → 新核心工具（最后手段）。见下文「足迹阶梯」。
- **扩展，而非重复。** 加模块/manager/hook 之前，先看现有基础设施是否已覆盖这个用例。当多个 PR 集成同一**类别**东西时，设计一个共享接口，而不是一个一个合。
- **行为契约优于快照。** 测试应断言两份数据**必须如何关联**（不变量），而不是冻结一个当前值（模型列表、配置版本字面量、枚举计数）。见「不要写变更探测器测试」。
- **端到端验证，而不仅是绿色的 mock 单测。** 凡是涉及解析链、配置传播、安全边界、远程后端、文件/网络 I/O 的，都要用真实路径、真实 import、对着一个临时 `HERMES_HOME` 跑通。Mock 会掩盖集成 bug。
- **缓存安全、交替安全、不变量安全。** 保护 prompt 缓存；保护严格的消息角色交替（绝不连续两条同角色消息；绝不在循环中途注入一条合成的 user 消息）；保证 system prompt 在一个会话的生命周期内逐字节稳定。
- **保留贡献者署名。** 通过 cherry-pick（rebase-merge）来抢救外部工作，让作者身份在 git 历史中存活；能在别人基础上构建时，不要从零重写。

### 我们不想要的（即使做得很好也会被拒）

- **投机性基础设施。** 没有具体消费者的 hook、callback、扩展点。加 hook 容易，等 plugin 依赖上它之后再删就难了。但如果贡献者有真实、明确的用例，即使消费者单独出货，这个 hook 就**不算**投机。
- **为非密钥配置新增 `HERMES_*` env var。** `.env` 只放密钥（API key、token、密码）。所有行为设置——超时、阈值、feature flag、显示偏好——都进 `config.yaml`。如果机制内部需要 env var，可以从 `config.yaml` 桥接，但面向用户的文档要指向 `config.yaml`。叫用户"在 .env 里设 X"的 PR（除非 X 是凭证）会被拒。
- **在 terminal + file 已能搞定、或一个 skill 就能搞定时，新增核心工具。** 如果唯一障碍是远程后端上的文件可见性，那就修挂载，而不是修工具集。
- **在"指令型工具"上加偷懒读取的逃生口。** 对那些 agent 必须读全的内容工具（skills、prompts、playbooks）不加 `offset`/`limit` 分页。模型会读第 1 页然后跳过其余。
- **以摧毁所保护功能为代价的"修复"。** 一个把功能本身意义干掉的缓解措施，就是错的缓解。在限制行为之前，先读原始 commit 的意图（`git log -p -S`），找一个既修问题又保留功能的方案。
- **没有 opt-in 门控的出站遥测 / 使用归因。** 在通用的、面向用户的 opt-in（配置门 + setup 提示 + `hermes tools` 开关）存在之前，不加新的分析、第三方标识标记、归因标签。先打标签挂起，不要合并。
- **变更探测器测试、中途破坏缓存、没有 E2E 证明就接入死代码、以及碰核心文件的 plugin。** Plugin 活在自己的目录里，在我们提供的 ABC/hook 范围内工作；如果 plugin 需要更多，去拓宽通用 plugin 面，不要在核心里特判它。

### 在你称之为 bug 之前——先验证前提（以及何时不该关闭）

一个写得很好的 PR 被关闭，最常见的原因不是代码质量——而是这个改动建立在**错误的前提**上，或者把一个**有意的设计**当成了缺口。这些模式两边都管用：告诉人类 reviewer 该审什么，也告诉自动 sweeper 何时**不**安全把 PR 关成 `implemented_on_main` / `cannot_reproduce`（拿不准时，留给人类）。

- **"这是有意的设计，不是缺口。"** 一个看似疏漏的限制，往往是刻意的。修一个"缺失的链接"或"限制"之前，先问这种隔离本身是不是设计。例子：profile 是有意互相独立的孤岛——一个"让 profile 从默认 profile 实时继承配置"的 PR 被关，因为把 profile 耦合在一起正是设计要防止的（`--clone` 这种创建时拷贝的路径已经覆盖了"从我的默认开始"的合法需求）。
- **"前提与 X 的真实工作方式不符。"** 一个 PR 的理由常常建立在对现有机制的错误心智模型上。接受理由前先追真实代码/运行时。两个真实关闭案例：一个"在冷却期重新探测"的限流 PR（断路器只在**确认为空**的账号桶上跳闸，所以重新探测只是再捶一个已证明为空的桶）；一个使用量累积修复，它的新分支**运行时永不执行**，因为更早的 guard 已经把它依赖的状态弹出了。如果你不能指出 bug 发作的确切行、并证明你的修复改变了那一行的行为，你就还没验证前提。
- **"这个修复是错的——那个缺失/省略是有意的。"** 加上"看似缺失的那块"可能破坏被省略所保护的东西。例子：把"缺失"的 `__init__.py` 补回去，让一个测试树作为带点号的 package 可被导入，从而遮蔽了真正的 plugin，在 import 时删掉了它的 `register()`。这个"缺失"是承重的。
- **"越界 / 复活了我们已放弃的方向。"** 超出范围地取代一个商定的基线，或复活维护者刻意关掉的方向，即使代码能跑也会被拒。把改动收窄到真正商定的那一小块，其余作为聚焦的后续提出。

主线思想：**写或合并修复之前，对照代码库验证主张 AND 意图。** 一次在当前 `main` 上的确认复现 + 一段行级的"修复作用在哪"的说明，永远胜过一个听起来合理的理由。拿不准意图时，问一下比发一个跟设计对着干的修复要便宜。

### 足迹阶梯（新能力决策）

每一级比上一级增加更多永久性面。选能正确解决问题的**最高**（最小足迹）那一级：

1. **扩展现有代码**——这个能力是已有东西的变体。零新增面。
2. **CLI 命令 + skill**——可用 shell 命令表达的配置/状态/基础设施管理。Agent 在一个 skill 引导下运行 `hermes <subcommand>`。零 model-tool 足迹。订阅、定时任务、服务安装的默认选择。例：`hermes webhook`、`hermes cron`、`hermes tools`。
3. **Service-gated 工具（`check_fn`）**——需要结构化参数/返回值，AND 只在某个前提配置好时才出现。否则零足迹。例：Home Assistant 工具（gated on token）、memory-provider 工具。
4. **Plugin**——第三方/小众/用户专属、不随核心出货的能力。活在 `~/.hermes/plugins/` 或 pip 包里，运行时发现。
5. **MCP server（进 catalog）**——如果能力确实需要是个工具（agent 调用的结构化 I/O）但又不是核心根本，优先把它做成 MCP server 加进 MCP catalog，而不是把核心工具集做大。Agent 通过内置 MCP client 连接；零永久核心 schema 足迹，且任何 MCP host 可复用。
6. **新核心工具**——只有当能力是根本性的、对几乎每个用户都广泛有用、且通过 terminal + file（或 MCP server）无法触达时。正确的核心工具例子：terminal、read_file、web_search、browser_navigate。

当 3 个以上 open PR 试图集成同一**类别**的东西（memory 后端、provider、notifier），不要一个一个合——设计一个 ABC + orchestrator，把现有内置实现包成第一个 provider，再把竞争的 PR 变成针对该接口的 plugin。

---

## 三、开发环境

```bash
# 优先用 .venv；如果你的 checkout 里是 venv 就退回 venv。
source .venv/bin/activate   # 或：source venv/bin/activate
```

`scripts/run_tests.sh` 会依次探测 `.venv`、`venv`、`$HOME/.hermes/hermes-agent/venv`（用于与主 checkout 共享 venv 的 worktree）。

---

## 四、项目结构

文件数量一直在变——下面这棵树别当成完整清单，**真相在文件系统里**。注释标出了你真正会去改的承重入口。

```
hermes-agent/
├── run_agent.py          # AIAgent 类——核心对话循环（约 12k 行）
├── model_tools.py        # 工具编排，discover_builtin_tools()，handle_function_call()
├── toolsets.py           # 工具集定义，_HERMES_CORE_TOOLS 列表
├── cli.py                # HermesCLI 类——交互式 CLI 编排器（约 11k 行）
├── hermes_state.py       # SessionDB——SQLite 会话存储（FTS5 搜索）
├── hermes_constants.py   # get_hermes_home()、display_hermes_home()——profile 感知路径
├── hermes_logging.py     # setup_logging()——agent.log / errors.log / gateway.log（profile 感知）
├── batch_runner.py       # 并行批处理
├── agent/                # Agent 内部（provider 适配器、memory、缓存、压缩等）
├── hermes_cli/           # CLI 子命令、setup 向导、plugins 加载器、skin 引擎
├── tools/                # 工具实现——通过 tools/registry.py 自动发现
│   └── environments/     # 终端后端（local、docker、ssh、modal、daytona、singularity）
├── gateway/              # 消息网关——run.py + session.py + platforms/
│   ├── platforms/        # 每个平台一个适配器（telegram、discord、slack、whatsapp、
│   │                     #   homeassistant、signal、matrix、mattermost、email、sms、
│   │                     #   dingtalk、wecom、weixin、feishu、qqbot、bluebubbles、
│   │                     #   yuanbao、webhook、api_server...）。见 ADDING_A_PLATFORM.md。
│   └── builtin_hooks/    # 始终注册的网关 hook 扩展点（默认没出货任何）
├── plugins/              # Plugin 系统（见下文「Plugin」）
│   ├── memory/           # Memory-provider plugin（honcho、mem0、supermemory...）
│   ├── context_engine/   # Context-engine plugin
│   ├── model-providers/  # 推理后端 plugin（openrouter、anthropic、gmi...）
│   ├── kanban/           # 多 agent 看板 dispatcher + worker plugin
│   ├── hermes-achievements/  # 游戏化成就追踪
│   ├── observability/    # 指标 / trace / 日志 plugin
│   ├── image_gen/        # 图像生成 provider
│   └── <others>/         # disk-cleanup、google_meet、platforms、spotify...
├── optional-skills/      # 较重/小众、随仓库出货但默认不激活的 skill
├── skills/               # 仓库自带的内置 skill
├── ui-tui/               # Ink（React）终端 UI——`hermes --tui`
│   └── src/              # entry.tsx、app.tsx、gatewayClient.ts + app/components/hooks/lib
├── tui_gateway/          # TUI 的 Python JSON-RPC 后端
├── acp_adapter/          # ACP server（VS Code / Zed / JetBrains 集成）
├── cron/                 # 调度器——jobs.py、scheduler.py
├── scripts/              # run_tests.sh、release.py、辅助脚本
├── website/              # Docusaurus 文档站
└── tests/                # Pytest 套件（约 17k 测试、约 900 文件，2026 年 5 月数据）
```

**用户配置：** `~/.hermes/config.yaml`（设置）、`~/.hermes/.env`（仅 API key）。
**日志：** `~/.hermes/logs/`——`agent.log`（INFO+）、`errors.log`（WARNING+）、跑网关时还有 `gateway.log`。通过 `get_hermes_home()` 做 profile 感知。用 `hermes logs [--follow] [--level ...] [--session ...]` 浏览。

---

## 五、TypeScript 风格

适用于 Hermes 里所有 TypeScript：桌面、TUI、网站及未来 TS 包。

- 当状态被共享、复用或被远处 UI 读取时，优先用小的 nanostore 而非组件 state。
- 让每个 feature 拥有自己的 atom。聊天状态放在聊天附近，shell 状态放在 shell 附近，共享状态放 `src/store`。
- 从 atom 渲染的组件用 `useStore`；非渲染的动作用 `$atom.get()` 读。
- 别把 state 穿过三层组件传递，叶子组件能直接订阅 atom 就订阅。
- 持久化放在拥有它的 atom 旁边。
- 路由根保持轻薄。它们组合路由和 shell，不该变成控制器。
- 没有巨石 hook。一个 hook 只负责一件窄事。
- 优先用 colocated 的 action 模块，而非隐藏的上帝 hook。
- 纯副作用回调用简洁的 void 形式：`onState={st => void setGatewayState(st)}`。
- 异步 UI handler 要让意图显式：`onClick={() => void save()}`。
- 公共 props 和共享对象形状优先用 interface，避免用 `type X = { ... }` 写对象 props。
- props 扩展 React 原语：`React.ComponentProps<'button'>`、`React.ComponentProps<typeof Dialog>`、`Omit<...>`、`Pick<...>`。
- 映射 id/路由/视图时，表驱动优于条件阶梯。
- `src/app` 拥有路由、页面、页面专属组件；`src/store` 拥有共享 atom；`src/lib` 拥有共享纯函数 helper。

---

## 六、文件依赖链

```
tools/registry.py  （无依赖——被所有工具文件 import）
       ↑
tools/*.py  （每个在 import 时调用 registry.register()）
       ↑
model_tools.py  （import tools/registry + 触发工具发现）
       ↑
run_agent.py、cli.py、batch_runner.py、environments/
```

---

## 七、AIAgent 类（run_agent.py）

真实的 `AIAgent.__init__` 接收约 60 个参数（凭证、路由、callback、会话上下文、预算、凭证池等）。下面的签名只是你通常会碰的最小子集，完整列表读 `run_agent.py`。

```python
class AIAgent:
    def __init__(self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,              # "chat_completions" | "codex_responses" | ...
        model: str = "",                   # 空 → 之后从 config/provider 解析
        max_iterations: int = 90,          # 工具调用迭代数（与 subagent 共享）
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,              # "cli"、"telegram" 等
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        credential_pool=None,
        # ...还有 callback、thread/user/chat ID、iteration_budget、fallback_model、
        # checkpoints 配置、prefill_messages、service_tier、reasoning_config 等
    ): ...

    def chat(self, message: str) -> str:
        """简单接口——返回最终响应字符串。"""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """完整接口——返回含 final_response + messages 的 dict。"""
```

### Agent 循环

核心循环在 `run_conversation()` 内——完全同步，带中断检查、预算追踪和一次一轮的宽限调用：

```python
while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0) \
        or self._budget_grace_call:
    if self._interrupt_requested: break
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

消息遵循 OpenAI 格式：`{"role": "system/user/assistant/tool", ...}`。推理内容存在 `assistant_msg["reasoning"]`。

---

## 八、CLI 架构（cli.py）

- 用 **Rich** 画 banner/面板，用 **prompt_toolkit** 做带自动补全的输入。
- **KawaiiSpinner**（`agent/display.py`）——API 调用时的动画表情，`┊` 活动流展示工具结果。
- cli.py 里的 `load_cli_config()` 合并硬编码默认值 + 用户 config YAML。
- **Skin 引擎**（`hermes_cli/skin_engine.py`）——数据驱动的 CLI 主题；启动时从 `display.skin` 配置键初始化；skin 自定义 banner 颜色、spinner 表情/动词/翅膀、工具前缀、响应框、品牌文案。
- `process_command()` 是 `HermesCLI` 上的方法——按从中央 registry 经 `resolve_command()` 解析出的规范命令名分派。
- Skill 斜杠命令：`agent/skill_commands.py` 扫描 `~/.hermes/skills/`，作为 **user 消息**（不是 system prompt）注入，以保护 prompt 缓存。

### 斜杠命令 registry（`hermes_cli/commands.py`）

所有斜杠命令定义在一个中央 `COMMAND_REGISTRY`（`CommandDef` 对象列表）里。每个下游消费者都自动从这个 registry 派生：

- **CLI**——`process_command()` 经 `resolve_command()` 解析别名，按规范名分派。
- **网关**——`GATEWAY_KNOWN_COMMANDS` frozenset 用于 hook 触发，`resolve_command()` 用于分派。
- **网关 help**——`gateway_help_lines()` 生成 `/help` 输出。
- **Telegram**——`telegram_bot_commands()` 生成 BotCommand 菜单。
- **Slack**——`slack_subcommand_map()` 生成 `/hermes` 子命令路由。
- **自动补全**——`COMMANDS` 扁平 dict 喂给 `SlashCommandCompleter`。
- **CLI help**——`COMMANDS_BY_CATEGORY` dict 喂给 `show_help()`。

### 添加一个斜杠命令

1. 在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY` 加一条 `CommandDef`：
```python
CommandDef("mycommand", "这个命令做什么", "Session",
           aliases=("mc",), args_hint="[arg]"),
```
2. 在 `cli.py` 的 `HermesCLI.process_command()` 加 handler：
```python
elif canonical == "mycommand":
    self._handle_mycommand(cmd_original)
```
3. 若该命令在网关可用，在 `gateway/run.py` 加 handler：
```python
if canonical == "mycommand":
    return await self._handle_mycommand(event)
```
4. 持久化设置用 `cli.py` 里的 `save_config_value()`。

**CommandDef 字段：**
- `name`——不带斜杠的规范名（如 `"background"`）
- `description`——人类可读描述
- `category`——`"Session"` / `"Configuration"` / `"Tools & Skills"` / `"Info"` / `"Exit"` 之一
- `aliases`——别名元组（如 `("bg",)`）
- `args_hint`——help 里显示的参数占位符（如 `"<prompt>"`、`"[name]"`）
- `cli_only`——仅交互式 CLI 可用
- `gateway_only`——仅消息平台可用
- `gateway_config_gate`——配置 dotpath（如 `"display.tool_progress_command"`）；当它设在一个 `cli_only` 命令上时，若配置值为真，该命令在网关变为可用。`GATEWAY_KNOWN_COMMANDS` 始终包含 config-gated 命令以便网关分派；help/菜单只在门开时才显示它们。

**加别名**只需在已有 `CommandDef` 的 `aliases` 元组里加一项。其他文件不用改——分派、help 文本、Telegram 菜单、Slack 映射、自动补全全部自动更新。

---

## 九、TUI 架构（ui-tui + tui_gateway）

TUI 是经典（prompt_toolkit）CLI 的完整替代，通过 `hermes --tui` 或 `HERMES_TUI=1` 激活。

### 进程模型

```
hermes --tui
  └─ Node (Ink)  ──stdio JSON-RPC──  Python (tui_gateway)
       │                                  └─ AIAgent + tools + sessions
       └─ 渲染 transcript、composer、prompts、activity
```

TypeScript 拥有屏幕。Python 拥有 session、工具、模型调用和斜杠命令逻辑。

### 传输

stdio 上的换行分隔 JSON-RPC。请求来自 Ink，事件来自 Python。完整方法/事件目录见 `tui_gateway/server.py`。

### 关键界面

| 界面 | Ink 组件 | 网关方法 |
|---|---|---|
| 聊天流 | `app.tsx` + `messageLine.tsx` | `prompt.submit` → `message.delta/complete` |
| 工具活动 | `thinking.tsx` | `tool.start/progress/complete` |
| 审批 | `prompts.tsx` | `approval.respond` ← `approval.request` |
| Clarify/sudo/secret | `prompts.tsx`、`maskedPrompt.tsx` | `clarify/sudo/secret.respond` |
| Session 选择器 | `sessionPicker.tsx` | `session.list/resume` |
| 斜杠命令 | 本地 handler + fallthrough | `slash.exec` → `_SlashWorker`、`command.dispatch` |
| 补全 | `useCompletion` hook | `complete.slash`、`complete.path` |
| 主题 | `theme.ts` + `branding.tsx` | `gateway.ready` 带 skin 数据 |

### 斜杠命令流

1. 内置客户端命令（`/help`、`/quit`、`/clear`、`/resume`、`/copy`、`/paste` 等）在 `app.tsx` 本地处理。
2. 其余 → `slash.exec`（在常驻 `_SlashWorker` 子进程跑）→ `command.dispatch` 兜底。

### 开发命令

```bash
cd ui-tui
npm install       # 首次
npm run dev       # watch 模式（重建 hermes-ink + tsx --watch）
npm start         # 生产
npm run build     # 完整构建（hermes-ink + tsc）
npm run typecheck # 仅类型检查（tsc --noEmit）
npm run lint      # eslint
npm run fmt       # prettier
npm test          # vitest
```

### Dashboard 里的 TUI（`hermes dashboard` → `/chat`）

Dashboard 嵌入真正的 `hermes --tui`——**不是**重写。见 `hermes_cli/pty_bridge.py` + `hermes_cli/web_server.py` 里的 `@app.websocket("/api/pty")` 端点。

- 浏览器加载 `web/src/pages/ChatPage.tsx`，挂载 xterm.js 的 `Terminal`（WebGL 渲染器、`@xterm/addon-fit` 容器驱动 resize、`@xterm/addon-unicode11` 现代宽字符宽度）。
- `/api/pty?token=…` 升级为 WebSocket；auth 用与 REST 相同的临时 `_SESSION_TOKEN`，通过 query 参数（浏览器无法在 WS 升级上设 `Authorization`）。
- server 通过 `ptyprocess`（POSIX PTY——WSL 可以，原生 Windows 不行）spawn `hermes --tui` 会 spawn 的东西。
- 帧：每个方向是原始 PTY 字节；resize 通过 `\x1b[RESIZE:<cols>;<rows>]` 在 server 拦截并用 `TIOCSWINSZ` 应用。

**不要在 React 里重新实现主聊天体验。** 主 transcript、composer/输入流（含斜杠命令行为）、PTY 后端终端都属于嵌入的 `hermes --tui`——你给 Ink 加的任何东西会自动出现在 dashboard。如果你发现自己在为 dashboard 重建 transcript 或 composer，停下来，去扩展 Ink。

**当不是第二个聊天界面时，围绕 TUI 的结构化 React UI 是允许的。** 侧边栏 widget、inspector、摘要、状态面板等支撑视图（如 `ChatSidebar`、`ModelPickerDialog`、`ToolCall`）在补充嵌入式 TUI 而非替代 transcript/composer/terminal 时是可以的。让它们的 state 独立于 PTY 子进程的 session，并以非破坏方式暴露失败，使终端面板继续无损工作。

### Electron 桌面聊天 App（`apps/desktop/`）

一个**独立**的聊天界面，区别于经典 CLI 和 dashboard 的嵌入式 TUI。它是 Electron + React + nanostore 渲染器（`@assistant-ui/react`），通过 JSON-RPC（`requestGateway(method, params)`）与一个 `tui_gateway` 后端通信。它**不**嵌入 `hermes --tui`——它有自己的 composer、transcript 和斜杠命令管线。桌面 bug 路由到 `hermes-desktop-app-work` skill，不是 `hermes-dashboard-work`。

**桌面 App 的斜杠命令在客户端被精选，然后分派到后端。** 管线：

- **后端已提供一切。** `tui_gateway/server.py` 的 `commands.catalog`（空查询列表）和 `complete.slash`（带查询补全）都包含内置命令、用户 `quick_commands`、AND skill 派生命令（`scan_skill_commands()` / `get_skill_commands()`）。桌面 App 不需要新 RPC 就能看到 skill。
- **渲染器在 `apps/desktop/src/lib/desktop-slash-commands.ts` 精选。** 这是承重文件。它持有 `DESKTOP_COMMANDS`（palette 里显示的约 19 个内置）加上 terminal-only / messaging-only / picker-owned / settings-owned / advanced 命令的黑名单（这些不该塞满桌面 popover）。
  - `isDesktopSlashCommand(name)`——门控**执行**。对内置 AND 任何非内置（skill / quick command）返回 true，所以输入的扩展命令能运行。
  - `isDesktopSlashSuggestion(name)`——门控**发现/补全**。被 `app/chat/composer/hooks/use-slash-completions.ts` 的两条补全路径用，也被 `filterDesktopCommandsCatalog` 用。
  - `isDesktopSlashExtensionCommand(name)`——当命令不是已知 Hermes 内置（即 skill 或 quick command）时为 true。suggestion 和 catalog-filter 两条路径都放行扩展，让 skill 命令在 palette 出现。
- **分派**在 `app/session/hooks/use-prompt-actions.ts`（`runSlash`）：桌面自己拥有的内置（`/skin`、`/help`、`/new`…）本地处理或经 `commands.catalog`；其余走 `slash.exec`，兜底 `command.dispatch`（网关解析成 skill / 别名 / exec 指令）。skill 命令解析成 `{type: "skill", message}` 并作为普通 prompt 提交。

**规则：** 桌面斜杠 palette 的精选是为了隐藏噪音（terminal-only / messaging-only 内置），**不是**隐藏用户激活的扩展。skill 命令和 `quick_commands` 是后端暴露的扩展——它们该出现在补全里。如果你收紧 `desktop-slash-commands.ts`，保持 `isDesktopSlashExtensionCommand` 流入 suggestion 和 catalog-filter 两条路径。测试：`apps/desktop/src/lib/desktop-slash-commands.test.ts`（经仓库根 `vitest` 跑，因为 `apps/desktop` 从根 workspace 安装解析依赖）。

---

## 十、添加新工具

加任何工具前，先定足迹问题（见「足迹阶梯」）：绝大多数能力**不应该**是核心工具。对自定义或仅本地的工具，**不要**改 Hermes 核心。走 plugin 路线：建 `~/.hermes/plugins/<name>/plugin.yaml` 和 `~/.hermes/plugins/<name>/__init__.py`，用 `ctx.register_tool(...)` 注册工具。Plugin 工具集自动发现，可启用/禁用而不碰 `tools/` 或 `toolsets.py`。

只有当用户明确在贡献一个应随基础系统出货的新核心 Hermes 工具时，才用下面的内置路线。

内置/核心工具需改 **2 个文件**：

**1. 建 `tools/your_tool.py`：**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. 加进 `toolsets.py`**——要么 `_HERMES_CORE_TOOLS`（所有平台），要么一个新工具集。**这步必须做：** 自动发现会 import 工具并注册其 schema，但工具只有在名字出现在某个工具集里时才会**暴露给 agent**。`_HERMES_CORE_TOOLS` 不是死代码——它是每个平台基础工具集继承的默认包。

自动发现：任何含顶层 `registry.register()` 调用的 `tools/*.py` 文件都会被自动 import——没有手动 import 列表要维护。但接入工具集仍是刻意的、手动的一步。

registry 处理 schema 收集、分派、可用性检查、错误包装。所有 handler **必须**返回 JSON 字符串。

**schema 里的路径引用**：若 schema 描述提到文件路径（如默认输出目录），用 `display_hermes_home()` 让它 profile 感知。schema 在 import 时生成，那在 `_apply_profile_override()` 设好 `HERMES_HOME` 之后。

**状态文件**：若工具存持久状态（缓存、日志、checkpoint），用 `get_hermes_home()` 作为基目录——绝不用 `Path.home() / ".hermes"`。这保证每个 profile 有自己的状态。

**Agent 级工具**（todo、memory）：在 `handle_function_call()` 之前被 `run_agent.py` 拦截。模式见 `tools/todo_tool.py`。

---

## 十一、依赖钉版策略

所有依赖必须有上界，以限制供应链攻击面。此策略在 litellm 被攻陷后确立（PR #2796、#2810），并在 Mini Shai-Hulud 蠕虫活动后加强（2026 年 5 月）。

| 来源类型 | 处理 | 例子 |
|---|---|---|
| PyPI 包 | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | commit SHA | `git+https://...@<40字符sha>` |
| GitHub Actions | commit SHA + 注释 | `uses: actions/checkout@<sha>  # v4` |
| 仅 CI 的 pip | `==exact` | `pyyaml==6.0.2` |

**向 `pyproject.toml` 加新依赖时：**
1. 1.0 后钉到 `>=current_version,<next_major`（如 `>=1.5.0,<2`）。
2. 1.0 前的包用 `<0.(当前minor + 2)`（如 `>=0.29,<0.32`）。
3. 绝不提交没上界的裸 `>=X.Y.Z`——CI 和 reviewer 会拒。
4. 跑 `uv lock` 重生成带 hash 的 `uv.lock`。

参考：#2810（上界 pass）、#9801（SHA 钉版 + 审计 CI）。

---

## 十二、添加配置

### config.yaml 选项：
1. 加进 `hermes_cli/config.py` 的 `DEFAULT_CONFIG`。
2. **只有**当你需要主动迁移/转换现有用户配置（重命名键、改结构）时，才 bump `_config_version`（当前值看 `DEFAULT_CONFIG` 顶部）。向现有 section 加新键由 deep-merge 自动处理，**不**需要 bump 版本。

### 顶层 `config.yaml` section（非穷尽）：

`model`、`agent`、`terminal`、`compression`、`display`、`stt`、`tts`、`memory`、`security`、`delegation`、`smart_model_routing`、`checkpoints`、`auxiliary`、`curator`、`skills`、`gateway`、`logging`、`cron`、`profiles`、`plugins`、`honcho`。

`auxiliary` 持有副 LLM 工作（curator、vision、embedding、title 生成、session_search 等）的逐任务覆盖——每个任务可钉自己的 provider/model/base_url/max_tokens/reasoning_effort。解析顺序见 `agent/auxiliary_client.py::_resolve_auto`。

`curator` 持有后台 skill 维护配置——`enabled`、`interval_hours`、`min_idle_hours`、`stale_after_days`、`archive_after_days`、`backup`（嵌套）。

### .env 变量（仅密钥——API key、token、密码）：
1. 加进 `hermes_cli/config.py` 的 `OPTIONAL_ENV_VARS`，带元数据：
```python
"NEW_API_KEY": {
    "description": "用途",
    "prompt": "显示名",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

非密钥设置（超时、阈值、feature flag、路径、显示偏好）属于 `config.yaml`，不属于 `.env`。若内部代码为向后兼容需要 env var 镜像，从 `config.yaml` 桥接到 env var（见 `gateway_timeout`、`terminal.cwd` → `TERMINAL_CWD`）。

### 配置加载器（三条路径——要知道你在哪条）：

| 加载器 | 用于 | 位置 |
|---|---|---|
| `load_cli_config()` | CLI 模式 | `cli.py`——合并 CLI 专属默认值 + 用户 YAML |
| `load_config()` | `hermes tools`、`hermes setup`、大多数 CLI 子命令 | `hermes_cli/config.py`——合并 `DEFAULT_CONFIG` + 用户 YAML |
| 直接 YAML 加载 | 网关运行时 | `gateway/run.py` + `gateway/config.py`——读原始用户 YAML |

若你加了新键，CLI 看得到但网关看不到（或反之），说明你在错的加载器上。检查 `DEFAULT_CONFIG` 覆盖。

### 工作目录：
- **CLI**——用进程当前目录（`os.getcwd()`）。
- **消息平台**——用 `config.yaml` 的 `terminal.cwd`。网关把它桥接到 `TERMINAL_CWD` env var 给子工具。**`MESSAGING_CWD` 已移除**——若它在 `.env` 里设了，配置加载器会打弃用警告。`.env` 里的 `TERMINAL_CWD` 同理；规范设置是 `config.yaml` 的 `terminal.cwd`。

---

## 十三、Skin / 主题系统

skin 引擎（`hermes_cli/skin_engine.py`）提供数据驱动的 CLI 视觉自定义。Skin 是**纯数据**——加新 skin 无需改代码。

### 架构

```
hermes_cli/skin_engine.py    # SkinConfig dataclass、内置 skin、YAML 加载器
~/.hermes/skins/*.yaml       # 用户安装的自定义 skin（drop-in）
```

- `init_skin_from_config()`——CLI 启动时调用，从配置读 `display.skin`
- `get_active_skin()`——返回当前 skin 的缓存 `SkinConfig`
- `set_active_skin(name)`——运行时切 skin（`/skin` 命令用）
- `load_skin(name)`——先从用户 skin 加载，再内置，再退回 default
- 缺失的 skin 值自动从 `default` skin 继承

### 内置 skin

- `default`——经典 Hermes 金色/kawaii（当前外观）
- `ares`——深红/古铜战神主题，带自定义 spinner 翅膀
- `mono`——干净的灰阶单色
- `slate`——冷蓝、面向开发者的主题

### 加内置 skin

加进 `hermes_cli/skin_engine.py` 的 `_BUILTIN_SKINS` dict（结构见英文原文）。

### 用户 skin（YAML）

用户建 `~/.hermes/skins/<name>.yaml`，用 `/skin <name>` 或 `display.skin: <name>` 激活。可自定义 banner 边框/标题/强调色、spinner 动词/翅膀、品牌 agent_name/response_label、tool_prefix 等。

---

## 十四、Plugin

Hermes 有两个 plugin 界面。两者都在仓库的 `plugins/` 下，让仓库自带 plugin 能和 `~/.hermes/plugins/` 用户安装的、以及 pip 安装的 entry point 一起被发现。

### 通用 plugin（`hermes_cli/plugins.py` + `plugins/<name>/`）

`PluginManager` 从 `~/.hermes/plugins/`、`./.hermes/plugins/` 和 pip entry point 发现 plugin。每个 plugin 暴露一个 `register(ctx)` 函数，可以：

- 注册 Python 回调生命周期 hook：`pre_tool_call`、`post_tool_call`、`pre_llm_call`、`post_llm_call`、`on_session_start`、`on_session_end`
- 通过 `ctx.register_tool(...)` 注册新工具
- 通过 `ctx.register_cli_command(...)` 注册 CLI 子命令——plugin 的 argparse 树在启动时接入 `hermes`，使 `hermes <pluginname> <subcmd>` 工作，无需改 `main.py`

Hook 从 `model_tools.py`（pre/post tool）和 `run_agent.py`（生命周期）调用。**发现时机陷阱：** `discover_plugins()` 只作为 import `model_tools.py` 的副作用运行。不 import `model_tools.py` 就读 plugin 状态的代码路径必须显式调用 `discover_plugins()`（幂等）。

### Memory-provider plugin（`plugins/memory/<name>/`）

可插拔 memory 后端的独立发现系统。当前内置 provider 包括 **honcho、mem0、supermemory、byterover、hindsight、holographic、openviking、retaindb**。

每个 provider 实现 `MemoryProvider` ABC（见 `agent/memory_provider.py`），由 `agent/memory_manager.py` 编排。生命周期 hook 含 `sync_turn(turn_messages)`、`prefetch(query)`、`shutdown()`，以及可选 `post_setup(hermes_home, config)`（用于 setup 向导集成）。

**经 `plugins/memory/<name>/cli.py` 的 CLI 命令：** 若 memory plugin 定义 `register_cli(subparser)`，`discover_plugin_cli_commands()` 在 argparse setup 时发现并接入 `hermes <plugin>`。框架只为**当前激活**的 memory provider（读 config.yaml 的 `memory.provider`）暴露 CLI 命令，所以禁用的 provider 不会塞满 `hermes --help`。

**规则（Teknium，2026 年 5 月）：** plugin **绝不能**改核心文件（`run_agent.py`、`cli.py`、`gateway/run.py`、`hermes_cli/main.py` 等）。若 plugin 需要框架没暴露的能力，去拓宽通用 plugin 面（新 hook、新 ctx 方法）——绝不把 plugin 专属逻辑硬编码进核心。PR #5295 因此从 `main.py` 删掉了 95 行硬编码的 honcho argparse。

**不再新增 in-tree memory provider（策略，2026 年 5 月）：** `plugins/memory/` 下内置 memory provider 的集合已封闭。新 memory 后端必须作为**独立 plugin 仓库**出货，用户安装进 `~/.hermes/plugins/`（或经 pip entry point）——它们实现同一个 `MemoryProvider` ABC，经同一发现路径注册，经 `hermes memory setup` / `post_setup()` 集成，而不落进本树。向 `plugins/memory/` 加新目录的 PR 会被关闭并指向「把 provider 发布成自己的仓库」。现有 in-tree provider 保留；对它们的 bug 修复欢迎。

### Model-provider plugin（`plugins/model-providers/<name>/`）

每个推理后端（openrouter、anthropic、gmi、deepseek、nvidia…）都作为这里的 plugin 出货。每个 plugin 的 `__init__.py` 在模块加载时调用 `providers.register_provider(ProviderProfile(...))`。`providers/__init__.py._discover_providers()` 是一个**懒加载、独立的**发现系统——在首次 `get_provider_profile()` 或 `list_providers()` 调用时扫描，**不**由通用 PluginManager 扫。

扫描顺序：
1. 自带：`<repo>/plugins/model-providers/<name>/`
2. 用户：`$HERMES_HOME/plugins/model-providers/<name>/`
3. 旧版：`<repo>/providers/<name>.py`（向后兼容）

同名用户 plugin 覆盖自带的——`register_provider()` 是 last-writer-wins。这让第三方无需打补丁就能替换任何内置 profile。

通用 PluginManager 会记录 `kind: model-provider` manifest，但**不** import 它们（会重复实例化 `ProviderProfile`）。没显式 `kind:` 的 plugin 经源文本启发式（`__init__.py` 里有 `register_provider` + `ProviderProfile`）自动归类。

完整作者指南：`website/docs/developer-guide/model-provider-plugin.md`。

### Dashboard / context-engine / image-gen plugin 目录

`plugins/context_engine/`、`plugins/image_gen/` 等遵循同样模式（ABC + orchestrator + 每 plugin 一目录）。context engine 接入 `agent/context_engine.py`；image-gen provider 接入 `agent/image_gen_provider.py`。参考/文档配套 plugin（`example-dashboard`、`strike-freedom-cockpit`、`plugin-llm-example`、`plugin-llm-async-example`）住在 [`hermes-example-plugins`](https://github.com/NousResearch/hermes-example-plugins) 配套仓库，不在本树。

---

## 十五、Skill

两个平行界面：

- **`skills/`**——内置、默认可加载的 skill。按类别目录组织（如 `skills/github/`、`skills/mlops/`）。
- **`optional-skills/`**——较重或小众、随仓库出货但默认不激活。经 `hermes skills install official/<category>/<skill>` 显式安装。适配器在 `tools/skills_hub.py`（`OptionalSkillSource`）。类别含 `autonomous-ai-agents`、`blockchain`、`communication`、`creative`、`devops`、`email`、`health`、`mcp`、`migration`、`mlops`、`productivity`、`research`、`security`、`web-development`。

review skill PR 时，看它针对哪个目录——重依赖或小众 skill 属于 `optional-skills/`。

### SKILL.md frontmatter

标准字段：`name`、`description`、`version`、`author`、`license`、`platforms`（OS 门控列表：`[macos]`、`[linux, macos]`…）、`metadata.hermes.tags`、`metadata.hermes.category`、`metadata.hermes.related_skills`、`metadata.hermes.config`（skill 需要的 config.yaml 设置——存在 `skills.config.<key>`，setup 时提示，加载时注入）。

顶层 `tags:` 和 `category:` 也接受，由加载器从 `metadata.hermes.*` 镜像。

### Skill 作者标准（硬线）

每个新建或现代化的 skill——内置、optional 或贡献的——合并前都必须满足这些标准。Reviewer 拒绝违反的 PR。

1. **`description` ≤ 60 字符，一句话，以句号结尾。** 长描述会让 skill 列表臃肿、在多 skill 加载时稀释模型注意力。陈述能力，而非实现。不用营销词（"powerful"、"comprehensive"、"seamless"、"advanced"）。不重复 skill 名。
2. **SKILL.md 散文里引用的工具必须是原生 Hermes 工具或 skill 明确预期的 MCP server。** 用 backtick 按名指向正确工具（`` `terminal` ``、`` `web_extract` ``、`` `read_file` ``、`` `patch` ``、`` `search_files` ``、`` `vision_analyze` ``、`` `browser_navigate` ``、`` `delegate_task` `` 等）。不要点名 agent 已包装的 shell 工具——`grep` → `search_files`，`cat`/`head`/`tail` → `read_file`，`sed`/`awk` → `patch`，`find`/`ls` → `search_files target='files'`。依赖 MCP server 就点名它并在 `## Prerequisites` 文档化预期 setup。其他（第三方 CLI、shell 管道等）在脚本文件里随意，但不该是散文里的头号交互面。
3. **`platforms:` 门控对照实际脚本 import 审计。** 用 POSIX-only 原语（`fcntl`、`termios`、`os.setsid`、用 `os.kill(pid, 0)` 探活、`/proc`、硬编码 `/tmp`、`signal.SIGKILL`、bash heredoc、`osascript`、`apt`、`systemctl`）的 skill 必须声明支持的平台。默认姿态：先尝试跨平台修——`tempfile.gettempdir`、`pathlib.Path`、`psutil.pid_exists`、用 Python 级过滤代替 `grep`。只在依赖确实平台绑定时才门控到更窄集合。
4. **`author` 先记人类贡献者。** 外部贡献中，贡献者真名 + GitHub handle 在前；"Hermes Agent" 是次要协作者。若贡献者 commit 显示 "Hermes Agent" 作者（因为他们用 Hermes 起草），换成他们实名——给人类署名，不给工具。
5. **SKILL.md 正文用现代 section 顺序。** `# <Skill> Skill` 标题，2-3 句 intro（讲它做什么、不做什么），`## When to Use`、`## Prerequisites`、`## How to Run`、`## Quick Reference`、`## Procedure`、`## Pitfalls`、`## Verification`。复杂 skill 目标约 200 行，简单约 100 行。砍掉冗余 intro 废话、营销散文、对已在 `## Prerequisites` 的 env var 的重复解释。
6. **脚本进 `scripts/`，参考进 `references/`，模板进 `templates/`。** 别指望模型每次内联写 parser、XML walker 或重要逻辑——出货 helper 脚本。从 SKILL.md 按相对 skill 目录的路径引用它。
7. **测试住在 `tests/skills/test_<skill>_skill.py`**，只用 stdlib + pytest + `unittest.mock`。无实时网络调用。经 `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q` 跑。
8. **`.env.example` 新增隔离在清晰界定的块里。** 别碰周围文件——贡献者提供的 `.env.example` 通常过时，skill 自己块外的编辑在抢救时必须丢弃。

外部 skill PR 的完整抢救/现代化 checklist 在 `hermes-agent-dev` skill 的 `references/new-skill-pr-salvage.md`——打磨贡献者 skill PR 前先加载。

---

## 十六、工具集（Toolsets）

所有工具集在 `toolsets.py` 定义为单个 `TOOLSETS` dict。每个平台适配器选一个基础工具集（如 Telegram 用 `"messaging"`）；`_HERMES_CORE_TOOLS` 是大多数平台继承的默认包。

当前工具集键：`browser`、`clarify`、`code_execution`、`cronjob`、`debugging`、`delegation`、`discord`、`discord_admin`、`feishu_doc`、`feishu_drive`、`file`、`homeassistant`、`image_gen`、`kanban`、`memory`、`messaging`、`moa`、`rl`、`safe`、`search`、`session_search`、`skills`、`spotify`、`terminal`、`todo`、`tts`、`video`、`vision`、`web`、`yuanbao`。

经 `hermes tools`（curses UI）或 config.yaml 的 `tools.<platform>.enabled` / `tools.<platform>.disabled` 列表逐平台启用/禁用。

---

## 十七、委派（`delegate_task`）

`tools/delegate_tool.py` spawn 一个带独立上下文 + 终端 session 的 subagent。默认父 agent 等子 agent 的摘要后再继续自己的循环。带 `background=true` 时，Hermes 立即返回一个委派 id，结果稍后经异步委派完成队列重新进入对话。

两种形态：

- **单个：** 传 `goal`（+ 可选 `context`、`toolsets`）。
- **批量（并行）：** 传 `tasks: [...]`——每个有自己的 subagent 并发跑。并发被 `delegation.max_concurrent_children`（默认 3）限制。

角色：

- `role="leaf"`（默认）——聚焦 worker。不能调 `delegate_task`、`clarify`、`memory`、`send_message`、`execute_code`。
- `role="orchestrator"`——保留 `delegate_task` 以 spawn 自己的 worker。受 `delegation.orchestrator_enabled`（默认 true）门控，受 `delegation.max_spawn_depth`（默认 2）约束。

`config.yaml` 的 `delegation:` 下关键旋钮：`max_concurrent_children`、`max_spawn_depth`、`child_timeout_seconds`、`orchestrator_enabled`、`subagent_auto_approve`、`inherit_mcp_toolsets`、`max_iterations`。

持久性规则：background `delegate_task` 与当前轮分离，但仍是进程内的。需要扛进程重启的工作，用 `cronjob` 或 `terminal(background=True, notify_on_complete=True)`。

---

## 十八、Curator（skill 生命周期）

后台 skill 维护系统，追踪 agent 创建的 skill 的使用并自动归档过期的。用户永不丢 skill；归档进 `~/.hermes/skills/.archive/`，可恢复。

- **核心：** `agent/curator.py`（review 循环、自动转换、LLM review prompt）+ `agent/curator_backup.py`（运行前 tar.gz 快照）。
- **CLI：** `hermes_cli/curator.py` 接入 `hermes curator <verb>`，verb 有：`status`、`run`、`pause`、`resume`、`pin`、`unpin`、`archive`、`restore`、`prune`、`backup`、`rollback`。
- **遥测：** `tools/skill_usage.py` 拥有 sidecar `~/.hermes/skills/.usage.json`——逐 skill 的 `use_count`、`view_count`、`patch_count`、`last_activity_at`、`state`（active / stale / archived）、`pinned`。

不变量：
- Curator 只碰 `created_by: "agent"` 来源的 skill——内置 + hub 安装的 skill 不在范围。
- 永不删除；最大破坏动作是归档。
- Pinned skill 免于所有自动转换和 LLM review pass。
- `skill_manage(action="delete")` 拒绝 pinned skill；patch/edit/write_file/remove_file 放行，使 agent 能继续改进 pinned skill。

配置 section（config.yaml 的 `curator:`）：`enabled`、`interval_hours`、`min_idle_hours`、`stale_after_days`、`archive_after_days`、`backup.*`。

完整用户文档：`website/docs/user-guide/features/curator.md`。

---

## 十九、Cron（定时任务）

`cron/jobs.py`（job 存储）+ `cron/scheduler.py`（tick 循环）。Agent 经 `cronjob` 工具排定 job；用户经 `hermes cron <verb>`（`list`、`add`、`edit`、`pause`、`resume`、`run`、`remove`）或 `/cron` 斜杠命令。

支持的调度格式：
- 时长：`"30m"`、`"2h"`、`"1d"`
- "every" 短语：`"every 2h"`、`"every monday 9am"`
- 5 字段 cron 表达式：`"0 9 * * *"`
- ISO 时间戳（一次性）：`"2026-06-01T09:00:00Z"`

逐 job 字段含 `skills`（加载特定 skill）、`model` / `provider` 覆盖、`script`（运行前数据收集脚本，其 stdout 注入 prompt；`no_agent=True` 让脚本成为整个 job）、`context_from`（把 job A 最后输出链入 job B 的 prompt）、`workdir`（在特定目录运行并加载其 `AGENTS.md`/`CLAUDE.md`）、多平台投递。

加固不变量：
- 对 cron session **3 分钟硬中断**——失控 agent 循环不能霸占调度器。
- Catchup 窗口：job 周期的一半，钳制在 120s–2h。
- Grace 窗口：错过触发时间的一次性 job 给 120s。
- `~/.hermes/cron/.tick.lock` 文件锁防跨进程重复 tick。
- Cron session 默认传 `skip_memory=True`；memory provider 有意不在 cron 期间运行。

Cron 投递**不**镜像进目标网关 session——它们落在自己的 cron session（带 header/footer 框），使主对话的消息角色交替保持完整。

---

## 二十、Kanban（多 agent 工作队列）

持久的 SQLite 看板，让多个 profile / worker 协作共享任务。用户经 `hermes kanban <verb>` 驱动；dispatcher spawn 的 worker 经专用 `kanban_*` 工具集驱动，使它们不在 kanban 任务里时 schema 足迹为零。

- **CLI：** `hermes_cli/kanban.py` 接入 `hermes kanban`，verb 有 `init`、`create`、`list`（别名 `ls`）、`show`、`assign`、`link`、`unlink`、`comment`、`complete`、`block`、`unblock`、`archive`、`tail`，及较少用的 `watch`、`stats`、`runs`、`log`、`assignees`、`heartbeat`、`notify-*`、`dispatch`、`daemon`、`gc`。
- **Worker/orchestrator 工具集：** `tools/kanban_tools.py` 暴露 `kanban_show`、`kanban_complete`、`kanban_block`、`kanban_heartbeat`、`kanban_comment`、`kanban_create`、`kanban_link`；在 dispatcher spawn 任务外显式启用 `kanban` 工具集的 profile 还获 `kanban_list` 和 `kanban_unblock` 用于看板路由。
- **Dispatcher：** 长驻循环（默认每 60s）回收过期 claim、提升 ready 任务、原子 claim、spawn 被指派的 profile。默认经 `kanban.dispatch_in_gateway: true` **在网关内**运行。
- **Plugin 资产：** `plugins/kanban/dashboard/`（web UI）+ `plugins/kanban/systemd/`（`hermes-kanban-dispatcher.service`，用于独立 dispatcher 部署）。

隔离模型：
- **Board** 是硬边界——worker spawn 时 env 里钉 `HERMES_KANBAN_BOARD`，使它们看不到其他 board。
- **Tenant** 是 board *内*的软命名空间——一个专家 fleet 可用 workspace 路径 + memory key 隔离服务多个业务。
- 同一任务连续 `kanban.failure_limit` 次非成功尝试后（默认 2），dispatcher 自动 block 它以防 spin 循环。

完整用户文档：`website/docs/user-guide/features/kanban.md`。

---

## 二十一、重要策略

### Prompt 缓存绝不能破

Hermes 确保整个对话期间缓存有效。**不要实现会这样做的改动：**
- 中途改动过去的上下文
- 中途切换工具集
- 中途重载 memory 或重建 system prompt

破缓存会大幅推高成本。唯一改动上下文的时机是上下文压缩。

改动 system-prompt 状态（skill、tools、memory 等）的斜杠命令必须**缓存感知**：默认延迟失效（改动下个 session 生效），带一个 opt-in 的 `--now` flag 做立即失效。规范模式见 `/skills install --now`。

### 后台进程通知（网关）

用 `terminal(background=true, notify_on_complete=true)` 时，网关跑一个 watcher 检测进程完成并触发新的 agent 轮次。用 config.yaml 的 `display.background_process_notifications`（或 `HERMES_BACKGROUND_NOTIFICATIONS` env var）控制后台进程消息的详细度：

- `all`——运行中输出更新 + 最终消息（默认）
- `result`——只最终完成消息
- `error`——只在退出码 != 0 时的最终消息
- `off`——完全无 watcher 消息

---

## 二十二、Profile：多实例支持

Hermes 支持 **profile**——多个完全隔离的实例，每个有自己的 `HERMES_HOME` 目录（config、API key、memory、session、skill、网关等）。

核心机制：`hermes_cli/main.py` 的 `_apply_profile_override()` 在任何模块 import 前设 `HERMES_HOME`。所有 `get_hermes_home()` 引用自动作用到激活的 profile。

### profile 安全代码的规则

1. **所有 HERMES_HOME 路径用 `get_hermes_home()`。** 从 `hermes_constants` import。读写状态的代码里**绝不**硬编码 `~/.hermes` 或 `Path.home() / ".hermes"`。
   ```python
   # 好
   from hermes_constants import get_hermes_home
   config_path = get_hermes_home() / "config.yaml"
   # 坏——破坏 profile
   config_path = Path.home() / ".hermes" / "config.yaml"
   ```
2. **面向用户的消息用 `display_hermes_home()`。** 从 `hermes_constants` import。它对默认返回 `~/.hermes`，对 profile 返回 `~/.hermes/profiles/<name>`。
3. **模块级常量没问题**——它们在 import 时缓存 `get_hermes_home()`，那在 `_apply_profile_override()` 设好 env var 之后。只要用 `get_hermes_home()` 而非 `Path.home() / ".hermes"`。
4. **mock `Path.home()` 的测试还必须设 `HERMES_HOME`**——因为代码现在用 `get_hermes_home()`（读 env var），不是 `Path.home() / ".hermes"`：
   ```python
   with patch.object(Path, "home", return_value=tmp_path), \
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / ".hermes")}):
       ...
   ```
5. **网关平台适配器应用 token 锁**——若适配器用唯一凭证（bot token、API key）连接，在 `connect()`/`start()` 调 `gateway.status` 的 `acquire_scoped_lock()`，在 `disconnect()`/`stop()` 调 `release_scoped_lock()`。这防两个 profile 用同一凭证。规范模式见 `plugins/platforms/irc/adapter.py`。
6. **profile 操作锚定 HOME，不是 HERMES_HOME**——`_get_profiles_root()` 返回 `Path.home() / ".hermes" / "profiles"`，**不**是 `get_hermes_home() / "profiles"`。这是有意的——让 `hermes -p coder profile list` 不管哪个 profile 激活都能看到所有 profile。

---

## 二十三、已知陷阱

### 不要硬编码 `~/.hermes` 路径
代码路径用 `hermes_constants` 的 `get_hermes_home()`。面向用户的 print/log 用 `display_hermes_home()`。硬编码 `~/.hermes` 破坏 profile——每个 profile 有自己的 `HERMES_HOME` 目录。这是 PR #3575 修的 5 个 bug 的根源。

### 不要引入新的 `simple_term_menu` 用法
`hermes_cli/main.py` 现有调用点仅作旧版兜底；首选 UI 是 curses（stdlib），因为 `simple_term_menu` 在 tmux/iTerm2 用方向键时有幽灵重复渲染 bug。新交互菜单必须用 `hermes_cli/curses_ui.py`——规范模式见 `hermes_cli/tools_config.py`。

### spinner/显示代码里不要用 `\033[K`（ANSI 擦到行尾）
在 `prompt_toolkit` 的 `patch_stdout` 下会泄漏成字面 `?[K` 文本。用空格填充：`f"\r{line}{' ' * pad}"`。

### `_last_resolved_tool_names` 是 `model_tools.py` 的进程全局
`delegate_tool.py` 的 `_run_single_child()` 在 subagent 执行前后保存并恢复这个全局。若你加新代码读这个全局，注意它在子 agent 运行期间可能临时是过期的。

### 不要在 schema 描述里硬编码跨工具引用
工具 schema 描述不能按名提到其他工具集的工具（如 `browser_navigate` 说"prefer web_search"）。那些工具可能不可用（缺 API key、禁用工具集），导致模型幻觉调用不存在的工具。若需要跨引用，在 `model_tools.py` 的 `get_tool_definitions()` 里动态加——模式见 `browser_navigate` / `execute_code` 后处理块。

### 网关有两道消息 guard——都必须放行审批/控制命令
agent 运行时，消息经过两道顺序 guard：(1) **base 适配器**（`gateway/platforms/base.py`）在 `session_key in self._active_sessions` 时把消息排进 `_pending_messages`；(2) **网关 runner**（`gateway/run.py`）在 `/stop`、`/new`、`/queue`、`/status`、`/approve`、`/deny` 到达 `running_agent.interrupt()` 前拦截它们。任何必须在 agent 阻塞时到达 runner 的新命令（如审批 prompt）**必须**绕过**两道** guard 并 inline 分派，而非经 `_process_message_background()`（会与 session 生命周期竞争）。

### 从过期分支 squash merge 会静默回退近期修复
squash-merge 一个 PR 前，确保分支与 `main` 最新（在 worktree 里 `git fetch origin main && git reset --hard origin/main`，再重新应用 PR 的 commit）。过期分支里某个无关文件的版本会在 squash 时静默覆盖 main 上的近期修复。merge 后用 `git diff HEAD~1..HEAD` 验证——意外删除是红旗。

### 不要没有 E2E 验证就接入死代码
从未出货的未用代码死掉是有原因的。把未用模块接入活代码路径前，用真实 import（非 mock）对临时 `HERMES_HOME` 跑通真实解析链做 E2E 测试。

### 测试不能写 `~/.hermes/`
`tests/conftest.py` 的 `_isolate_hermes_home` autouse fixture 把 `HERMES_HOME` 重定向到临时目录。测试里绝不硬编码 `~/.hermes/` 路径。

**Profile 测试**：测 profile 功能时，还要 mock `Path.home()`，使 `_get_profiles_root()` 和 `_get_default_hermes_home()` 解析到临时目录内。用 `tests/hermes_cli/test_profiles.py` 的模式：
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---

## 二十四、测试

**永远用 `scripts/run_tests.sh`**——不要直接调 `pytest`。该脚本强制与 CI 一致的 hermetic 环境（unset 凭证 var、TZ=UTC、LANG=C.UTF-8、`-n auto` xdist worker、in-tree 子进程隔离 plugin）。在 16+ 核、设了 API key 的开发机上直接 `pytest` 会与 CI 分叉，已造成多次"本地过、CI 挂"事故（反之亦然）。

```bash
scripts/run_tests.sh                                  # 全套，CI 一致
scripts/run_tests.sh tests/gateway/                   # 一个目录
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # 一个测试
scripts/run_tests.sh -v --tb=long                     # 透传 pytest flag
scripts/run_tests.sh --no-isolate tests/foo/          # 关子进程隔离（更快，调试用）
```

### 每测试一子进程隔离

每个测试经 `tests/_isolate_plugin.py` in-tree plugin 在新 spawn 的 Python 子进程里跑。这意味着一个测试的模块级 dict/set 和 ContextVar 不能漏进下一个——历史的 `_reset_module_state` autouse fixture 已移除。

实现要点：
- plugin 用 `multiprocessing.get_context("spawn")`，Linux/macOS/Windows 都工作（不用 POSIX `fork`）。
- 每测试开销约 0.5–1.0s（Python 启动 + pytest 收集）。xdist 并行跨核摊销；20 核机器上全套墙钟时间和以前差不多，但无 flake。
- `isolate_timeout`（在 `pyproject.toml` 配）每测试上限 30s。挂起被杀并报为失败。
- 传 `--no-isolate` 关隔离——交互调试单测时，或你专门想验证状态泄漏时有用。
- plugin 在子进程里自禁用（哨兵 env var `HERMES_ISOLATE_CHILD=1`），无 fork 炸弹风险。

### 为什么要 wrapper（以及为什么"直接调 pytest"不行）

脚本关掉五个真实的本地-vs-CI 分叉源：

| | 无 wrapper | 有 wrapper |
|---|---|---|
| Provider API key | 你 env 里有啥（自动探测池） | 所有 `*_API_KEY`/`*_TOKEN` 等 unset |
| HOME / `~/.hermes/` | 你真实 config+auth.json | 每测试临时目录 |
| 时区 | 本地 TZ（PDT 等） | UTC |
| Locale | 你设的 | C.UTF-8 |
| xdist worker | `-n auto` = 全核 | `-n auto`（安全——子进程隔离防跨 worker flake） |

`tests/conftest.py` 也把第 1-4 点作为 autouse fixture 强制，所以任何 pytest 调用（含 IDE 集成）都得 hermetic 行为——但 wrapper 是双保险。

### 不用 wrapper 跑（只在你必须时）

不能用 wrapper（如 IDE 直接 shell pytest）时，至少激活 venv。隔离 plugin 从 `pyproject.toml` 的 `addopts` 自动加载，所以两种方式都得同样的每测试进程隔离。

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

调试时需要绕过隔离求快速反馈：
```bash
python -m pytest tests/agent/test_foo.py -q --no-isolate
```

推前永远跑全套。

### 不要写变更探测器测试

一个测试是**变更探测器**，如果它在**预期会变**的数据被更新时失败——模型目录、配置版本号、枚举计数、provider 模型的硬编码列表。这些测试不加行为覆盖；它们只保证例行源码更新破坏 CI、耗工程时间去"修"。

**不要写：**
```python
# 目录快照——每次模型发布就破
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
# 配置版本字面量——每次 schema bump 就破
assert DEFAULT_CONFIG["_config_version"] == 21
# 枚举计数——每次加 skill/provider 就破
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**要写：**
```python
# 行为：目录管道根本上工作吗？
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1
# 行为：迁移把用户版本 bump 到当前最新了吗？
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
# 不变量：每个目录模型都有 context-length 条目
for m in _PROVIDER_MODELS["huggingface"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

规则：测试读起来像当前数据快照就删；读起来像两份数据如何关联的契约就留。

---

# 当前任务：企业微信多租户改造

> 这一节是 `AGENTS.md` 没有的、面向你当前主线工作的高价值内容。

## 目标

把 Hermes 从"个人助手"改造成**单个 gateway 进程服务多个企业微信（WeCom）用户**的内部助手入口，并确保每个企业微信用户的数据默认互相隔离。所有隔离层用同一个 **owner_key**。

```
owner_key = f"wecom:{corp_id}:{app_id}:{user_id}"   # 如 wecom:ww1234:1000001:zhangsan
```

## 相关文档（study/ 目录）

| 文件 | 用途 |
|---|---|
| `study/multi-tenant-wecom-rebuild-plan.md` | **方案主文档**（v7 九必改定稿，每条断言带源码行号） |
| `study/hermes-agent-architecture-analysis.md` | 改造前的架构分析 |
| `study/process/*.md` | **进度记录**——每次推进都按"做了什么 / 已执行验证 / 仍未执行"记一笔 |

## 阶段 1 必须闭环的隔离面（九必改）

1. 统一 owner_key（所有隔离用同一个 key）
2. owner_key 进 session 路由（`build_session_key` 含隔离维度，防 DB 前 agent 复用导致 history bleed）
3. 历史读取四种形态 owner 校验（list / search / read / scroll / locate）
4. 所有 session 创建写 owner（含 compression 子 session）
5. Memory 三路径 + fail-closed（live / offline / pending）
6. 运行时 allowlist 收敛（gateway 创建 agent 前收敛工具面）
7. file 真实路径校验（`resolve()` + symlink，防越界访问其他租户 workspace）
8. 上传附件按 owner 隔离（media 落各自 workspace）
9. `/resume` + `switch_session` 会话恢复校验 owner（resume / switch / title 全校验）

## 关键不变量（改动红线）

- **fail-closed**：多租户模式下取不到 owner_key 必须**拒绝请求**，绝不放行成全局会话。
- **不泄漏 session 存在性**：owner 校验失败返回 "not found" 而非 "forbidden"（防枚举）。`assert_session_owner()` 是推荐的统一 helper。
- **session_search 两条路径**都要校验：registry handler（`tools/session_search_tool.py`）+ `invoke_tool` 特殊分支（`agent/agent_runtime_helpers.py`，不经 registry）。
- **默认行为不变**：`security.multi_tenant.enabled` 默认 `false`，单用户体验完全不受影响。
- 同时遵守全局两条铁律：**prompt 缓存不可破**、**消息角色严格交替**。

## 当前进度（截至 2026-06-25）

阶段 1 收尾项基本完成，已通过多组**定向 pytest**（详见最新 `study/process/` 记录）。**ruff / mypy / 全量 pytest 按你的要求暂时跳过**——这不代表它们已通过。

下一步建议：对照方案"必改"项做阶段 1 整体验收清单复核；合并前至少再跑一次更大的相关测试集，额度允许再补 ruff / mypy。阶段 2/3 目前仍是高层占位，不建议在补详细方案前直接开工。
