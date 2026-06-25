# Hermes Agent 源码学习文档（通用版）

> 目的：帮助初学者系统读懂 Hermes Agent 这个智能体项目的源码。
> 风格：从"它是什么 → 怎么运转 → 关键概念 → 怎么动手验证"逐层展开，对初学者友好。
> 行号基于当前分叉版本（`self` 分支），仅供定位，可能随代码变动；**真相永远在文件系统里**。
> 配套文档：架构权威说明见 `AGENTS.md`（英文）/ `HUMAN_zh.md`（中文完整版）。

---

## 一、这个项目是什么（一句话心智模型）

Hermes 是一个**个人 AI agent**：同一套"大脑"（agent 内核）同时跑在 CLI、消息网关（企业微信/Telegram/Slack 等约 20 个平台）、TUI、桌面 App 上。

> 心智模型：可以把它理解成"一个会用工具的助理"。
> - **大模型**负责思考、决定"下一步该干嘛"。
> - **Hermes 内核**负责把大模型的决定变成真实动作（读文件、跑命令、查网页、发消息……），再把结果喂回给大模型。
> - 这个"思考 → 动作 → 看结果 → 再思考"的循环不停转，直到任务完成。

它的扩展靠 **plugin 和 skill**，而不是把内核改大。

---

## 二、怎么读这个项目（入口文件地图）

不要试图一次读完。先认住这几个**承重入口**，其它都是围着它们转的：

| 文件 | 角色 | 你什么时候看它 |
|---|---|---|
| `run_agent.py` | `AIAgent` 类——**核心对话循环**（约 12k 行）| 想懂"agent 怎么思考/调工具" |
| `agent/conversation_loop.py` | 对话循环的实际实现细节 | 想懂循环里每一步、缓存怎么打 |
| `model_tools.py` | 工具编排：发现工具、分派调用 | 想懂"工具是怎么被调起来的" |
| `toolsets.py` | 工具集定义（哪些工具打包给哪个平台）| 想控制"某平台能用哪些工具" |
| `tools/registry.py` | 工具注册中心（被所有工具 import）| 想懂工具如何被自动发现 |
| `tools/*.py` | 每个具体工具的实现 | 想看某个工具到底干了啥 |
| `cli.py` | `HermesCLI`——交互式命令行（约 11k 行）| 想懂命令行体验、斜杠命令 |
| `gateway/run.py` | 消息网关运行器 | 想懂"多平台消息怎么进来、怎么路由" |
| `gateway/session.py` | 会话 key 与会话来源 | 想懂"一条消息属于哪个会话" |
| `hermes_state.py` | `SessionDB`——SQLite 历史存储 | 想懂历史/搜索怎么存 |
| `tools/memory_tool.py` | 记忆系统（MEMORY.md/USER.md）| 想懂跨会话记忆怎么存 |
| `hermes_constants.py` | `get_hermes_home()` 等 profile 感知路径 | 几乎所有涉及路径的代码都依赖它 |

**依赖方向**（谁依赖谁）：

```
tools/registry.py          （最底层，无依赖）
       ↑
tools/*.py                 （import 时调用 registry.register() 自注册）
       ↑
model_tools.py             （触发工具发现）
       ↑
run_agent.py / cli.py / gateway/run.py / batch_runner.py
```

---

## 三、核心概念 1：agent 是怎么"思考"的——对话循环

这是整个项目的心脏，在 `run_agent.py` 的 `run_conversation()` 里（实现细节在 `agent/conversation_loop.py`）。简化后长这样：

```python
while 还没到迭代上限 and 预算还有:
    if 收到中断请求: break
    # 1. 把"当前所有消息 + 可用工具"发给大模型
    response = client.chat.completions.create(model=..., messages=messages, tools=tool_schemas)
    if response.tool_calls:           # 2. 大模型决定要调工具
        for tool_call in response.tool_calls:
            result = handle_function_call(工具名, 参数)   # 3. 内核真的去执行
            messages.append(把结果包成一条消息)            # 4. 把结果塞回对话
        # 回到循环顶部，让大模型看着结果继续想
    else:
        return response.content        # 5. 大模型不调工具了 = 给出最终回答
```

要点（给初学者）：

- **完全同步**：一步一步来，不是事件驱动的玄学。
- **消息是 OpenAI 格式**：`{"role": "system/user/assistant/tool", ...}`。`system` 是设定，`user` 是用户说的，`assistant` 是模型说的，`tool` 是工具返回的结果。
- **"工具调用"= function calling**：大模型不会自己执行代码，它只会"说"我想调 `read_file`、参数是 `path=xxx`；真正去执行的是 Hermes 内核。执行完把结果作为 `tool` 消息喂回去。
- **有上限和预算**：`max_iterations`（默认 90）防止无限循环；还有 token 预算追踪。

> 比喻：大模型像一个**坐在屋里、只能动嘴的专家**。它说"帮我把第 3 个抽屉打开看看"，跑腿打开抽屉、把里面东西拿给它看的是 Hermes。专家看了再说下一步。一来一回直到事情办完。

---

## 四、核心概念 2：工具系统（function calling + registry + toolsets）

这是 Hermes 最值得学的设计之一，分三层：

### 1. 工具自注册（`tools/registry.py` + `tools/*.py`）

每个工具文件在被 import 时，调用 `registry.register(...)` 把自己登记进去。例如 `tools/file_tools.py:1844`：

```python
registry.register(name="read_file", toolset="file", schema=READ_FILE_SCHEMA,
                  handler=_handle_read_file, check_fn=_check_file_reqs, ...)
```

- `name`：工具名（大模型会用这个名字来调它）。
- `schema`：告诉大模型"这个工具是干嘛的、需要哪些参数"（这就是发给大模型的"工具说明书"）。
- `handler`：真正执行的函数，**必须返回 JSON 字符串**。
- `check_fn`：可用性检查（比如缺 API key 就不暴露这个工具）。

**自动发现**：任何含顶层 `registry.register()` 的 `tools/*.py` 都会被自动 import，不用维护手动 import 列表。

### 2. 工具集打包（`toolsets.py`）

工具注册了还不够，必须出现在某个**工具集（toolset）**里才会真正暴露给 agent。`_HERMES_CORE_TOOLS` 是大多数平台继承的默认包。当前工具集有 `terminal`、`file`、`web`、`search`、`memory`、`skills`、`delegation` 等几十个。

> 关键理解：**"注册" ≠ "暴露"**。注册让工具存在；放进工具集才让某个平台的 agent 能用它。这两步是分开的，多租户收敛工具面就是利用这一点（只给某平台开 `file`、关 `terminal`）。

### 3. 调用编排（`model_tools.py`）

`handle_function_call()` 负责：拿到大模型给的工具名+参数 → 找到对应 handler → 执行 → 包装结果/错误 → 返回。

### 工具的两个典型例子（对比学习）

- **terminal（`tools/terminal_tool.py:2681`）**：参数是一坨自由文本 `command`。最灵活，也最难做安全校验。
- **file 工具集（`tools/file_tools.py`）**：`read_file` / `write_file` / `patch` / `search_files`，参数是**结构化的 `path`**，可以在执行前校验路径。这是"受限但安全"的代表。

---

## 五、核心概念 3：prompt 缓存（为什么"神圣不可破"）

这是理解 Hermes 很多设计取舍的关键，也最容易误解。**缓存有两种，都不落盘。**

### 缓存 A：prompt 缓存（在大模型服务商那边，比如 Anthropic）

源码 `agent/prompt_caching.py`：本地只做一件事——给消息打"请缓存"标记（`{"type": "ephemeral"}`），最多 4 个断点（system prompt + 最后 3 条消息）。

- 真正的缓存**存在服务商服务器**，本机管不到，TTL 5 分钟或 1 小时。
- 命中条件：每次请求的"开头那段"（system + 历史前缀）**一个字节都不能变**。
- 收益：多轮对话省约 75% 的输入 token 成本。

### 缓存 B：agent 缓存（在 gateway 进程内存里）

源码 `gateway/run.py:2610` 附近，`self._agent_cache`：把每个会话的 `AIAgent` 对象缓存在内存，避免每条消息都重建 agent、重拼 system prompt。注释明确：不缓存的话每轮重建会**破坏前缀缓存、贵约 10 倍**。

### 为什么"中途不能改上下文"

因为只要你在对话中途改了过去的消息、换了工具集、或重建了 system prompt，"开头那段"就变了 → 缓存 A 全程失效 → 成本暴涨，而且**功能看起来一切正常**，很难被测试发现。

> 唯一允许改上下文的时机：**上下文压缩**（对话太长时压缩历史）。
> 给初学者：可以把缓存 A 理解成"上次算过的开头直接复用"。你想省钱，就别动开头。

---

## 六、核心概念 4：会话与历史（SessionDB / state.db）

源码 `hermes_state.py`：历史存在一个 **SQLite 数据库** `~/.hermes/state.db` 里。

- `messages` 表存所有对话消息。
- 用 **FTS5**（SQLite 的全文搜索）支持跨会话快速搜索。
- 一条消息属于哪个"会话"，由 `gateway/session.py` 的 `build_session_key()`（约 686 行）决定——它根据平台、用户、聊天 ID 等算出一个 session key。

> 关键理解：**"会话"是逻辑概念，落到磁盘是 SQLite 行**。换平台、换用户，会话 key 不同，历史就分开了。这也是多租户隔离历史的抓手（让 session key/记录带上 owner 维度）。

---

## 七、核心概念 5：记忆（memory）

源码 `tools/memory_tool.py`：记忆是**磁盘上的 Markdown 文件**，存在 `~/.hermes/memories/`：

- `MEMORY.md`：agent 的个人笔记/观察（环境事实、项目信息等）。
- `USER.md`：关于用户的事实。

和历史（结构化、存 SQLite）不同，记忆是**给人和大模型都能读的纯文本**，会被注入到 system prompt 里，让 agent "跨会话记得事情"。

> 给初学者：历史 = "这次对话说了啥"（流水账，存数据库）；记忆 = "我长期记住的关于你和环境的要点"（笔记，存文件，每次开场带上）。

---

## 八、核心概念 6：gateway 多平台 + 路径的 profile 感知

### 多平台网关（`gateway/`）

`gateway/run.py` 是网关运行器，`gateway/platforms/` 下每个平台一个适配器（telegram、wecom、slack……）。消息从某平台进来 → 适配器统一成内部事件 → 路由到对应会话的 agent → agent 处理 → 回复发回该平台。

### profile 感知路径（`hermes_constants.py`）——必须懂的一条铁律

Hermes 支持 **profile**（多个完全隔离的实例，各有独立 `HERMES_HOME` 目录）。所以代码里：

- **读写状态用 `get_hermes_home()`**，绝不硬编码 `~/.hermes` 或 `Path.home() / ".hermes"`。
- **面向用户的消息用 `display_hermes_home()`**。

硬编码 `~/.hermes` 会破坏 profile——这是历史上多个 bug 的根源。读任何涉及路径的代码，都要留意它用的是不是 `get_hermes_home()`。

---

## 九、核心概念 7：扩展机制（plugin / skill）

Hermes 的设计哲学是"内核窄、能力长在边缘"，所以扩展机制很重要。

### Skill（技能）

- `skills/`：内置技能，默认可加载，每个是一个带 `SKILL.md` 的目录。
- `optional-skills/`：较重/小众，默认不激活，显式安装。
- 本质：skill 是**给 agent 看的操作手册 + 配套脚本**。用户一句话触发，agent 按手册一步步做。
- 重要：skill 通过 **user 消息**注入（不是塞进 system prompt），以保护 prompt 缓存。

### Plugin（插件）

- `plugins/` 下分多种：通用 plugin、memory provider、model provider、context engine、image gen 等。
- 通用 plugin 通过 `register(ctx)` 注册生命周期 hook、新工具、CLI 子命令。
- 铁律：**plugin 绝不能改核心文件**。需要新能力就拓宽通用 plugin 面，不在核心里特判某个 plugin。

> 给初学者：想给 Hermes 加能力，优先级（足迹阶梯，从省事到重）：
> 扩展现有代码 → CLI 命令+skill → service-gated 工具 → plugin → MCP server → 新核心工具（最后手段）。
> 越靠前越省事、越不污染内核。新核心工具最贵，因为它随**每次** API 调用一起发出去。

---

## 十、两条贯穿全局的设计哲学（读代码时的"为什么"）

很多看似奇怪的写法，背后都是这两条：

1. **每会话 prompt 缓存神圣不可破**——别中途改上下文/换工具集/重建 system prompt（唯一例外：上下文压缩）。
2. **内核是窄腰，能力长在边缘**——每个核心工具都很贵，新能力尽量走 skill/plugin。

附带还要守：**消息角色严格交替**（绝不连续两条同角色消息、绝不循环中途注入合成 user 消息）、**profile 安全**（路径用 `get_hermes_home()`）。

---

## 十一、怎么动手验证 / 调试

### 跑测试（必须用 wrapper）

```bash
source .venv/bin/activate
scripts/run_tests.sh                                   # 全套（CI 一致环境）
scripts/run_tests.sh tests/gateway/                    # 某个目录
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # 某个测试
scripts/run_tests.sh --no-isolate tests/foo/           # 关子进程隔离，调试更快
```

> 为什么不能直接 `pytest`：wrapper 会强制和 CI 一致的环境（清空 API key、UTC 时区、临时 HOME、子进程隔离），避免"本地过、CI 挂"。

### 看日志

```bash
hermes logs [--follow] [--level ...] [--session ...]
```

日志在 `~/.hermes/logs/`：`agent.log`（INFO+）、`errors.log`（WARNING+）、`gateway.log`（跑网关时）。

### 一个高效的"顺藤摸瓜"读码法

想搞懂某个功能时：
1. 在 `cli.py` / `gateway/run.py` 找到入口（命令或消息处理）。
2. 跟到它调用的方法，看它怎么调 `AIAgent` 或工具。
3. 工具相关就去 `tools/对应文件.py` 看 `handler`。
4. 配置相关就去 `hermes_cli/config.py` 的 `DEFAULT_CONFIG` 看默认值。

---

## 十二、推荐的源码阅读路线（按这个顺序最省力）

1. **先读 `HUMAN_zh.md` / `AGENTS.md` 的"项目结构"和"AIAgent 类"两节**——建立全局地图。
2. **`agent/conversation_loop.py` 的主循环**——理解 agent 怎么思考、怎么调工具、怎么打缓存。
3. **`tools/registry.py` + 一个简单工具（如 `tools/file_tools.py` 的 `read_file`）**——理解工具三层结构。
4. **`toolsets.py`**——理解工具如何按平台打包。
5. **`gateway/session.py` 的 `build_session_key` + `hermes_state.py`**——理解会话与历史。
6. **`tools/memory_tool.py`**——理解跨会话记忆。
7. **`gateway/run.py` 的 `_agent_cache`**——理解为什么要缓存 agent、缓存怎么保命。
8. **`hermes_cli/plugins.py` + 一个 `plugins/` 下的例子**——理解扩展机制。

读的过程中始终带着那两条哲学铁律问"为什么这么写"，很多设计就通了。

---

## 十三、一条消息的完整数据流转（端到端串讲）

> 这一节把前面所有零散概念串成**一条完整的数据流**：从用户发一条消息，到模型回复，中间每一步数据存在哪、怎么路由、怎么省钱、怎么保证可恢复。读懂这一节，等于把 gateway + loop + 缓存 + 存储全部打通。

### 13.1 两层"指纹"先分清（最容易混的点）

数据流里有两个不同层的 key，**千万别合并成一个**：

| 指纹 | 所在层 | 谁计算 | 是不是 DB 字段 | 作用 |
|---|---|---|---|---|
| **session_key / session_id** | **Hermes 自己** | `gateway/session.py` 的 `build_session_key()` | ✅ 是，DB 查询字段 | 路由：决定加载/复用哪个用户的消息列表与 agent |
| **前缀内容哈希** | **大模型厂商服务器** | 厂商对前缀内容自动做哈希 | ❌ 不是，在厂商内存里 | 在厂商的 KV 缓存池里查、复用 |

- **多租户改造要动的是前者**（session_key，给它加 owner 维度）。后者厂商自动算、应用碰不到、也不用碰——内容不同自然分开。
- 发给模型的消息列表里**没有"指纹字段"**，只有 `system` 消息 + 后续 `user/assistant/tool` 消息（纯文本）。

### 13.2 端到端流程图

```
① 用户发消息（CLI / 企业微信 / Slack ...）
       ↓
② gateway 适配器把它统一成内部事件，构造 SessionSource
   （多租户下：在这里生成 owner_key = 平台:租户:用户 等唯一标识）
       ↓
③ build_session_key(source) → 一个确定性的 session_key 字符串
   （多租户下：owner_hash 作为 namespace 前缀拼进 key → 不同用户不撞 key）
       ↓
④ 用 session_key 在内存 _agent_cache 里查这个用户的 AIAgent：
   ├─ 命中（热）   → 直接用内存里的 agent，它已持有完整消息列表（不查 DB）
   └─ 未命中（冷） → 新建 agent，从 DB(SessionStore/state.db) 加载该 session 的历史文本
       ↓
⑤ 把当前用户消息【追加】到这个 agent 的消息列表末尾（append-only，不改前面）
       ↓
⑥ 进入对话循环 run_conversation()：
   a. 发送【全量】消息列表给厂商（注意：是全量，不是只发新消息）
      - 系统提示逐字节复刻、消息字节归一化 → 让前缀 bit-perfect
      - 打 cache_control 缓存标记
   b. 厂商按【前缀内容哈希】在缓存池查 KV：
      - 旧前缀命中 → 复用 KV、跳过 prefill、按缓存价计费（省钱在这）
      - 新增部分   → 现算 KV
   c. 模型返回：
      ├─ 有 tool_calls → 先把这一轮【存进 DB】，再执行工具，
      │                  工具结果【追加进列表 + 存 DB】，回到 a 继续循环
      └─ 无 tool_calls → 这是最终答案，跳出循环
       ↓
⑦ 把最终回复存 DB，通过 gateway 适配器发回给【同一个】用户
```

### 13.3 每一步的关键认知（对应前面章节）

- **第②③步——路由是隔离的命门**：模型/厂商对你的多个用户提供**零隔离**（无状态 + 按内容寻址）。区分谁是谁、不让 A 的历史混进 B 的列表，**100% 是 Hermes 在第②③步的责任**。owner_key 必须在**路由层（session_key）**就生效——因为第④步的 agent 复用发生在查 DB 之前，只在 DB 查询里过滤不够。

- **第④步——内存 agent vs DB**：消息列表平时活在**内存的 cached agent** 里（为了保住 prompt 缓存，不每轮重建系统提示）；**DB(state.db) 是持久化兜底**，冷启动/缓存淘汰时才从 DB 加载。两者分工：内存=快、DB=可恢复。

- **第⑥a 步——发的是全量，省的是算量**：每次请求都把**完整列表**传给厂商，缓存**不会**让你"只发新消息"。省的是厂商对旧前缀的**重复计算 + 计费**，不是网络传输量。

- **第⑥c 步——先存 DB 再执行工具**：在工具产生任何副作用【之前】先把这一轮刷进 DB（`agent/conversation_loop.py` 约 4051-4058 行）。这样即使破坏性工具中途把进程干掉，resume 也能看到已执行的 tool-call 块——典型的"先写日志再动手"的可恢复设计。

- **数据落盘 vs 临时态**：历史（state.db 的 messages 表）、记忆（MEMORY.md）是**人类可读的文本、永久落盘**，按用户隔离靠分目录/加列；prompt 缓存（KV 向量）在**厂商 GPU、临时**，靠"前缀内容不同"自然分开；agent 缓存在**进程内存、临时**，靠 session_key 带 owner 来分。三者别混。

### 13.4 一句话总纲

> **单进程 Hermes**：按 owner 的 **session_key 路由**到各自的消息列表（内存 agent 持有、DB 兜底）；每轮把**全量列表**发给厂商，厂商按**前缀内容哈希**复用旧 KV、只现算新增部分（省的是计算与计费，不是传输）；模型要调工具就**先存 DB 再执行**，循环到模型不再调工具为止，回复发回**同一个用户**。隔离的命门在**路由层**——因为厂商对多用户零隔离，区分用户完全是 Hermes 自己的活。
