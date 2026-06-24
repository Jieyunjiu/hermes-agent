# Hermes Agent 项目架构分析

> 分析时间：2026-06-24
> 分析对象：hermes-agent 源码仓库
> 分析方式：系统性源码走读（入口、核心循环、工具系统、CLI、Gateway、插件/技能、测试）

---

## 一、项目定位与核心设计哲学

Hermes 是一个**个人 AI Agent 平台**，把同一个 agent core 运行在多种界面上：CLI、TUI、Electron 桌面应用、以及 ~20 个消息平台（Telegram/Discord/Slack/WhatsApp 等）。它能跨会话学习（记忆 + 技能）、委托子 agent、跑定时任务，还能驱动真实终端和浏览器。

**两条贯穿全局的设计准则**（也是 PR review 的标尺）：

| 准则 | 含义 | 影响 |
|------|------|------|
| **Per-conversation prompt caching is sacred**（逐会话 prompt 缓存神圣不可侵犯） | 一个长对话每轮都复用缓存前缀；任何中途修改历史/换工具集/重建 system prompt 都会让缓存失效，成倍增加成本 | 唯一允许中途改 context 的是「上下文压缩」 |
| **Core 是窄腰，能力放在边缘**（narrow waist, capability at the edges） | 每个 model tool 都会随每次 API 调用发送，所以新增**核心**工具的门槛极高 | 大多数新能力应该通过 CLI 命令+skill / 插件 / MCP server 到达，而非长在 core 上 |

理解这两点，就读懂了项目里 90% 的「为什么这样设计」。

---

## 二、整体架构总览

```
                    ┌─────────────────────────────────────────┐
   入口              │  hermes (hermes_cli/main.py)            │  hermes-acp / hermes-agent
                    │  _apply_profile_override() 先设 HERMES_HOME │
                    └──────────────┬──────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
   ┌─────────┐             ┌──────────────┐           ┌──────────────┐
   │  CLI    │             │  Gateway     │           │  TUI / 桌面  │
   │ cli.py  │             │ gateway/run  │           │ ui-tui (Ink) │
   │ (15k行) │             │ + platforms/ │           │ + tui_gateway│
   └────┬────┘             └──────┬───────┘           └──────┬───────┘
        │                         │                          │
        └────────────┬────────────┴──────────────────────────┘
                     ▼
        ┌────────────────────────────────────────┐
        │   AIAgent (run_agent.py — 5.5k行 facade)│
        │   实际逻辑转发到 agent/ 子包:           │
        │   • agent_init.py     — 初始化          │
        │   • conversation_loop — 主循环          │
        │   • system_prompt.py  — 系统提示词      │
        │   • context_compressor — 压缩           │
        │   • memory_manager / memory_provider    │
        └──────────────┬─────────────────────────┘
                       │ 每轮调用 model + tools
                       ▼
        ┌────────────────────────────────────────┐
        │  model_tools.py — 工具编排/dispatch     │
        │  toolsets.py    — 工具集定义            │
        │  tools/registry.py — 中央注册表(singleton)│
        │  tools/*.py     — 各工具自注册          │
        └──────────────┬─────────────────────────┘
                       │
        ┌──────────────┴──────────────────────────┐
        ▼                                          ▼
   ┌──────────┐     ┌──────────────┐     ┌──────────────────┐
   │ skills/  │     │ plugins/     │     │ providers/ +     │
   │ optional │     │ memory/      │     │ model-providers/ │
   │ -skills/ │     │ model-providers│   │ (推理后端插件)   │
   └──────────┘     └──────────────┘     └──────────────────┘
```

---

## 三、入口与启动链路

三个等价入口（`pyproject.toml` 的 `[project.scripts]`）：

| 命令 | 入口函数 | 用途 |
|------|----------|------|
| `hermes` | `hermes_cli.main:main` | 主 CLI / TUI / gateway / 所有子命令 |
| `hermes-agent` | `run_agent:main` | 直接跑 agent（库式调用，编程接口） |
| `hermes-acp` | `acp_adapter.entry:main` | 作为 ACP server 接入 VS Code/Zed/JetBrains |

**启动顺序的关键点**（`hermes_cli/main.py`）：
1. **最先** import `hermes_bootstrap`（Windows UTF-8 stdio 修复，POSIX 无操作）
2. `_apply_profile_override()` 在任何模块 import **之前**设置 `HERMES_HOME` → 这就是「profile 隔离」机制：每个 profile 有独立的 config/keys/memory/sessions/skills/gateway
3. 所有 `get_hermes_home()` 调用都会自动 scope 到当前 profile

**Profile 规则**（改造时必须遵守）：
- 代码路径一律用 `get_hermes_home()`（从 `hermes_constants`），**绝不**硬编码 `~/.hermes`
- 用户可见信息用 `display_hermes_home()`
- 模块级常量缓存 `get_hermes_home()` 是安全的（在 override 之后）

---

## 四、核心：AIAgent 与对话循环

这是最重要的部分。`run_agent.py` 虽然有 5568 行，但现在是一个 **facade/forwarder**——真实逻辑已被抽取到 `agent/` 子包（这是 AGENTS.md 鼓励的「把 god-file 拆成干净模块」的成果）。

### 4.1 分层结构

| 文件 | 行数 | 职责 |
|------|------|------|
| `run_agent.py` | 5568 | `AIAgent` 类外壳 + forwarder 方法 |
| `agent/agent_init.py` | 1781 | `init_agent()` — 真实初始化（~60 个参数） |
| `agent/conversation_loop.py` | 4595 | `run_conversation()` — 主循环（从 run_agent 抽出的最大一块） |
| `agent/system_prompt.py` | 536 | 系统提示词组装 |
| `agent/turn_context.py` | — | 每轮 setup 的 prologue |

### 4.2 `AIAgent.__init__` 的关键参数

构造函数有 ~60 个参数，核心几类：
- **凭证/路由**：`base_url`, `api_key`, `provider`, `api_mode`（"chat_completions" / "codex_responses" / ...）
- **模型/预算**：`model`, `max_iterations`（默认 90，与子 agent 共享）， `iteration_budget`, `fallback_model`
- **工具集**：`enabled_toolsets`, `disabled_toolsets`
- **平台上下文**：`platform`（"cli"/"telegram"/...）, `session_id`, `chat_id`, `thread_id`, `gateway_session_key`
- **回调（一堆）**：`stream_delta_callback`, `tool_start_callback`, `tool_complete_callback`, `step_callback`, `event_callback`... 这些是 UI 层订阅 agent 事件的钩子
- **记忆/checkpoint**：`skip_memory`, `session_db`, `checkpoints_enabled`

### 4.3 主循环（`conversation_loop.run_conversation`）

```
build_turn_context()        ← 每轮 setup：stdio 守护、重置计数、消息净化、
                              system prompt restore-or-build、preflight 压缩、
                              pre_llm_call 插件钩子、外部 memory prefetch

while (api_call_count < max_iterations and budget.remaining > 0) or grace_call:
    if interrupt_requested: break
    api_call_count += 1
    consume budget (或 grace_call)

    # 排空 /steer（在 API 调用期间注入的引导消息）
    drain_pending_steer()

    response = client.chat.completions.create(model, messages, tools=schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(...)        ← 见第五节
            messages.append(tool_result_message(result))
    else:
        final_response = response.content
        break

    # 触发各种回调 / 压缩检查 / 失败重试 / fallback model
```

**循环的几个硬约束**（违反 = review 被拒）：
- **消息角色严格交替**：绝不允许两个相同 role 的消息相邻；绝不在 loop 中途注入 synthetic user message
- **system prompt 在会话生命周期内 byte-stable**（只有上下文压缩能触发重建）
- 有 **one-turn grace call**（预算耗尽后给模型最后一次机会）

---

## 五、工具系统（三层架构）

这是整个项目最精巧的部分，也是你最可能做改造的地方。

### 5.1 三层结构

```
tools/registry.py   ← 中央注册表 singleton（无依赖，被所有 tool 文件 import）
       ↑
tools/*.py          ← 每个 tool 文件在 module level 调 registry.register()
       ↑
model_tools.py      ← import registry + 触发 discover_builtin_tools()；提供
                      handle_function_call() / get_tool_definitions()
       ↑
run_agent.py, cli.py, gateway/run.py
```

### 5.2 `ToolRegistry` singleton 的核心 API

| 方法 | 作用 |
|------|------|
| `register(name, toolset, schema, handler, check_fn, ...)` | 注册工具；`override=True` 才能覆盖内置工具 |
| `discover_builtin_tools()` | AST 扫描 `tools/*.py`，自动 import 含顶层 `registry.register()` 的模块 |
| `get_definitions(tool_names)` | 返回 OpenAI 格式 schema；会跑 `check_fn` 过滤（30s TTL 缓存） |
| `dispatch(name, args)` | 执行 handler；自动桥接 async；异常统一返回 `{"error": ...}` |
| `register_toolset_alias()` / `deregister()` | 给 MCP 动态刷新用 |

**关键设计**：
- **自动发现**：任何 `tools/*.py` 带 top-level `registry.register()` 就被自动 import——无需维护 import 列表。但**接入到 toolset** 仍是手动步骤。
- **check_fn TTL 缓存**：像 `check_terminal_requirements` 这种会探测外部状态（Docker/Modal）的检查，30s 内缓存，既避免每轮重复探测，又能让 `hermes tools enable` 在一两轮内生效。
- **shadowing 保护**：同名工具注册默认被拒（除非都来自 MCP，或显式 `override=True`），防止插件/MCP 意外覆盖内置工具。
- **generation 计数器**：每次 mutation 自增，`get_tool_definitions` 据此做缓存 memo。

### 5.3 Toolsets（`toolsets.py`）

`TOOLSETS` 是一个 dict，把工具组织成命名组，可互相 `includes`：

```python
_HERMES_CORE_TOOLS = ["web_search", "web_extract", "terminal", "process",
                      "read_file", "write_file", "patch", "search_files",
                      "vision_analyze", "todo", "memory", "delegate_task", ...]

TOOLSETS = {
    "web": {"tools": ["web_search","web_extract"], "includes": []},
    "browser": {"tools": [...12个], "includes": []},
    "messaging": {... includes core ...},   # 平台基座
    ...
}
```

每个平台 adapter 选一个 base toolset（Telegram 用 `messaging`）。用户通过 `hermes tools`（curses UI）或 `config.yaml` 的 `tools.<platform>.enabled/disabled` 调整。

### 5.4 Footprint Ladder（新增能力的决策阶梯）

**这是改造时的指南针**。从上到下，永久 surface 递增，选能正确解决问题的**最高（最小 footprint）档**：

1. **扩展现有代码** — 能力是已有东西的变体，零新 surface
2. **CLI 命令 + skill** — `hermes <subcommand>` 引导 skill 执行，**零 model-tool footprint**（订阅/定时任务/服务安装的默认选择）
3. **Service-gated tool (`check_fn`)** — 需要结构化参数返回 + 只在配置了前置条件时才出现（如 HA 工具 gated on token）
4. **Plugin** — `~/.hermes/plugins/<name>/`，运行时发现
5. **MCP server（在 catalog 里）** — 真需要是 tool 但非核心，优先做成 MCP server
6. **新核心 tool** — 最后手段，仅当 terminal+file 无法达到、且几乎所有用户都需要时

> **改造建议**：你的自定义/本地工具**绝对不要**改 Hermes core，走 plugin 路线：`~/.hermes/plugins/<name>/plugin.yaml` + `__init__.py`，用 `ctx.register_tool(...)`。

### 5.5 添加核心工具的 2 步（仅当你确实要贡献核心工具）

1. 建 `tools/your_tool.py`，调 `registry.register(name, toolset, schema, handler, check_fn, requires_env)`
2. 在 `toolsets.py` 的 `_HERMES_CORE_TOOLS` 或某个 toolset 加上工具名（**必须**，否则不会被 expose 给 agent）

---

## 六、CLI 架构（`cli.py`，15484 行）

`HermesCLI` 类是交互式 CLI 编排器：
- **Rich** 画 banner/panel，**prompt_toolkit** 做带自动补全的输入
- `KawaiiSpinner`（`agent/display.py`）在 API 调用时显示动画表情
- `process_command()` 是 slash 命令分发器

### Slash 命令的单一真相源（`hermes_cli/commands.py`）

这是架构上的亮点——所有 slash 命令定义在**一个** `COMMAND_REGISTRY`（list of `CommandDef`），所有下游消费者自动派生：

```
COMMAND_REGISTRY (commands.py)
   ├→ CLI:        process_command() 经 resolve_command() 分发
   ├→ Gateway:    GATEWAY_KNOWN_COMMANDS + dispatch
   ├→ Gateway /help: gateway_help_lines()
   ├→ Telegram:   telegram_bot_commands() 生成 BotCommand 菜单
   ├→ Slack:      slack_subcommand_map() 生成 /hermes 路由
   ├→ 自动补全：    COMMANDS dict → SlashCommandCompleter
   └→ CLI /help:  COMMANDS_BY_CATEGORY → show_help()
```

**加一个 slash 命令** = 1 处 `CommandDef` + 1 处 handler（+ gateway handler 若需要）。**加别名**只需在 `aliases` tuple 加一项，其余全自动更新。

`CommandDef` 字段：`name`, `description`, `category`, `aliases`, `args_hint`, `subcommands`, `cli_only`, `gateway_only`, `gateway_config_gate`（config 开关控制命令在 gateway 是否可用）。

---

## 七、Gateway / 消息平台架构

### 7.1 进程模型

```
hermes gateway (gateway/run.py, 17910 行)
   ├─ 每个平台一个 adapter，继承 BasePlatformAdapter (gateway/platforms/base.py, 5255行)
   │   实现：connect/disconnect/send/edit_message/create_handoff_thread/...
   ├─ session.py (1532行) — 每个聊天一个 agent 会话
   ├─ 平台自注册：platform_registry.py (plugin 平台先查，内建 fallback if/elif)
   └─ 接收消息 → 路由到对应 chat 的 AIAgent → 把 response 发回平台
```

### 7.2 平台 adapter 的两种注册方式

1. **内建 adapter**（telegram/discord/slack/...）：走 `_create_adapter()` 里的 if/elif
2. **Plugin adapter**：调 `platform_registry.register(PlatformEntry(...))`，优先查找

`PlatformEntry` 含 `adapter_factory`（工厂而非裸类，便于自定义 init）、`check_fn`、`validate_config`、`is_connected`、`required_env`、`setup_fn` 等。`gateway/platforms/ADDING_A_PLATFORM.md` 是加平台的指南。

### 7.3 Gateway 的两个消息守卫（改造陷阱）

当 agent 运行时，消息经过**两个**守卫：
1. **base adapter**：`_pending_messages` 队列（当 session active）
2. **gateway runner**：拦截 `/stop` `/new` `/queue` `/approve` 等

> 任何必须在 agent 阻塞时到达 runner 的新命令（如审批），必须**同时**绕过两个守卫并 inline dispatch，否则会 race。

### 7.4 Cron 投递不镜像到主会话

Cron 投递落在自己的 cron session（带 header/footer frame），**不**镜像到目标 gateway session——这是为了保持主对话的 message-role alternation 完整。

---

## 八、插件与记忆系统

### 8.1 三个独立的插件发现系统（重要区分！）

| 系统 | 位置 | 发现时机 | 作用 |
|------|------|----------|------|
| **General PluginManager** | `plugins/<name>/` + `~/.hermes/plugins/` + pip entry point | `discover_plugins()` 作为 import `model_tools.py` 的副作用 | 工具/CLI 命令/lifecycle 钩子 |
| **Model providers** | `plugins/model-providers/<name>/` | **lazy**，首次 `get_provider_profile()` 时扫描 | 推理后端 profile（30+ 个：anthropic/openrouter/gemini/deepseek/xai...） |
| **Memory providers** | `plugins/memory/<name>/` | MemoryManager 编排 | 记忆后端（honcho/mem0/supermemory/byterover/hindsight/holographic/openviking/retaindb） |

**陷阱**：`discover_plugins()` 只在 import `model_tools.py` 时运行。读 plugin 状态但不先 import model_tools 的路径必须显式调 `discover_plugins()`（幂等）。

### 8.2 `PluginContext` facade

插件 `register(ctx)` 拿到 `PluginContext`，可以：
- `ctx.register_tool(...)` — 注册工具（委托给 `registry.register`，带 override 支持）
- `ctx.register_cli_command(...)` — 加 `hermes <plugin> <subcmd>` 子命令
- `ctx.register_platform(...)` — 注册平台 adapter
- `ctx.inject_message(...)` — 注入消息到活跃对话
- `ctx.llm` — 用宿主的 model/auth 跑 LLM 调用（fail-closed，config gate）
- 注册 lifecycle 钩子：`pre_tool_call`/`post_tool_call`/`pre_llm_call`/`post_llm_call`/`on_session_start`/`on_session_end`

**铁律（2026年5月）**：插件**不得**修改 core 文件（`run_agent.py`/`cli.py`/`gateway/run.py`/`main.py`）。需要更多能力就**扩展通用 plugin surface**（新 hook/新 ctx 方法），绝不硬编码。

### 8.3 MemoryProvider ABC（`agent/memory_provider.py`）

抽象基类定义完整生命周期，所有记忆后端实现它：
```
initialize()          — 连接、建资源
system_prompt_block() — 静态提示文本
prefetch(query)       — 每轮前后台召回
sync_turn()           — 每轮后异步写
get_tool_schemas()    — 暴露给模型的工具
handle_tool_call()    — 分发工具调用
shutdown()
+ 可选: on_turn_start / on_session_end / on_pre_compress / on_delegation / backup_paths
```
MemoryManager 强制**单一外部 provider**限制（防 schema 膨胀和冲突）。

> **政策**：不再接受新的 in-tree memory provider。新后端必须作为**独立 plugin repo** 发布。

---

## 九、Skills 系统

两个并行 surface：
- **`skills/`** — 内建，默认可用（按 category：github/mlops/research/creative/productivity/software-development...）
- **`optional-skills/`** — 重量/小众，**默认不激活**，`hermes skills install official/<cat>/<skill>` 安装

### SKILL.md frontmatter 与硬性标准

每个 skill 必须满足（`skills/<cat>/<name>/SKILL.md`）：
1. `description` ≤60 字符，单句，以句号结尾（不堆砌营销词）
2. SKILL.md 正文提到的工具必须是**原生 Hermes 工具或 MCP server**，用反引号命名（`` `terminal` ``,`` `read_file` ``）；**不要**点名 shell 工具（grep→`search_files`,cat→`read_file`）
3. `platforms:` 门控要和实际脚本 import 一致
4. `author` 先写人类贡献者，再写 "Hermes Agent"
5. 现代 section 顺序；脚本在 `scripts/`，引用在 `references/`，模板在 `templates/`
6. 测试在 `tests/skills/test_<skill>_skill.py`

### Skill slash 命令的缓存安全

Skill slash 命令扫描 `~/.hermes/skills/`，作为 **user message**（不是 system prompt）注入，以**保护 prompt caching**。修改 system-prompt 状态的 slash 命令必须 cache-aware：默认延迟生效（下个 session），`--now` 立即生效。

---

## 十、配置体系（三条 loader 路径，极易踩坑）

| Loader | 使用者 | 位置 |
|--------|--------|------|
| `load_cli_config()` | CLI 模式 | `cli.py` |
| `load_config()` | `hermes tools`/`setup`/大多子命令 | `hermes_cli/config.py`（`DEFAULT_CONFIG` + 用户 YAML） |
| 直接 YAML load | Gateway 运行时 | `gateway/run.py` + `gateway/config.py` |

> 加了新 key，CLI 看得到但 gateway 看不到 → 你在错的 loader 上。检查 `DEFAULT_CONFIG` 覆盖。

**关键区分**：
- `config.yaml` — **所有**非密配置（超时/阈值/feature flag/路径/显示偏好）
- `.env` — **仅限密钥**（API key/token/password）。加非密 env var 的 PR 会被拒

顶层 config section：`model`/`agent`/`terminal`/`compression`/`display`/`memory`/`security`/`delegation`/`smart_model_routing`/`checkpoints`/`auxiliary`/`curator`/`skills`/`gateway`/`cron`/`profiles`/`plugins`...

`auxiliary` 段很特别：per-task 覆盖 side-LLM 工作（curator/vision/embedding/title/session_search），每个任务可独立 pin provider/model/base_url/max_tokens/reasoning_effort。

---

## 十一、测试体系（改造必读）

**永远用 `scripts/run_tests.sh`**，不要直接 `pytest`。脚本强制 CI 环境一致性：
- unset 所有 `*_API_KEY`/`*_TOKEN`
- `~/.hermes/` → 每个 test 一个 temp dir
- `TZ=UTC`，`LANG=C.UTF-8`
- `-n auto` xdist 并行

### Subprocess-per-test 隔离

每个 test 跑在全新的 spawn 子进程（`tests/_isolate_plugin.py`），所以模块级 dict/set/ContextVar 不会跨 test 泄漏。开销 ~0.5-1s/test，xdist 并行摊销。`--no-isolate` 可关。

### 测试哲学：行为契约，不是快照

**不要写 change-detector**（会因数据正常更新而失败）：
```python
# ❌ 坏：catalog 快照，每次发模型都坏
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert DEFAULT_CONFIG["_config_version"] == 21
assert len(_PROVIDER_MODELS["huggingface"]) == 8

# ✅ 好：关系/不变量
assert "gemini" in _PROVIDER_MODELS                              # 管道通吗
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"] # migration 对吗
for m in _PROVIDER_MODELS["huggingface"]:                        # 每个 catalog 条目都有 context length
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

测试约 17k 个，跨 ~900 文件（127 个 tests 子目录：e2e/stress/integration/docker/fakes...）。

---

## 十二、依赖与供应链安全

项目对依赖极谨慎（经历过 litellm 投毒 + Mini Shai-Hulud 蠕虫）：
- **核心依赖精确 pin**（`==X.Y.Z`，不用 range），核心只放**每个 session 都用**的包
- provider-specific 的（anthropic/firecrawl/fal/edge-tts）放 extra，首次用时 `tools/lazy_deps.py` 懒加载
- Git URL 用 commit SHA，GitHub Actions 用 SHA+注释

---

## 十三、关键改造建议（针对学习/改造目标）

基于以上分析，如果你要在此项目上改造，以下是入口选择：

| 你想做的事 | 推荐路径 | 涉及文件 |
|-----------|---------|---------|
| 加一个自定义工具 | **Plugin** | `~/.hermes/plugins/<name>/plugin.yaml` + `__init__.py` + `ctx.register_tool` |
| 加一条 slash 命令 | 注册表 + handler | `hermes_cli/commands.py`（CommandDef）+ `cli.py`（handler）+ 可选 `gateway/run.py` |
| 接一个新消息平台 | plugin adapter | `plugins/platforms/<name>/` + `platform_registry.register` + 看 `ADDING_A_PLATFORM.md` |
| 换个推理后端 | model-provider plugin | `plugins/model-providers/<name>/` + `register_provider(ProviderProfile)` |
| 加记忆后端 | 独立 plugin repo | 实现 `MemoryProvider` ABC（注意：不再接受 in-tree） |
| 加技能 | skill | `skills/<cat>/<name>/SKILL.md`（+ scripts/references/templates） |
| 改 agent 行为 | 谨慎，看 prompt caching | `agent/conversation_loop.py` + `agent/system_prompt.py` |
| 定时任务 | cronjob 工具 / `hermes cron` | `cron/jobs.py` + `cron/scheduler.py` |
| 多 agent 协作 | kanban | `hermes kanban` + `tools/kanban_tools.py` |

**三条红线**（改造时务必遵守）：
1. **不要破坏 prompt caching** — 不中途改 context/换 toolset/重建 system prompt
2. **Core 保持窄** — 大多数能力走 plugin/skill/MCP，不碰 `run_agent.py`/`cli.py`/`model_tools.py`/`toolsets.py`
3. **Profile-safe** — 用 `get_hermes_home()`，不硬编码 `~/.hermes`
