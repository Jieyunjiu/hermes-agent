# 企业微信多租户改造——讨论与改进思路（2026-06-25）

> 本文记录与 AI 助手讨论后形成的改造背景、目标、困难判断，以及两个关键技术点（缓存 vs 文件隔离、shell 工具收敛）的源码级结论与建议。
> 性质：**思考/决策辅助文档**，不是实施清单。实施清单见 `study/multi-tenant-wecom-rebuild-plan.md`，进度见 `study/process/`。
> 所有源码行号基于当前分叉版本（`self` 分支），仅供定位参考。

---

## 一、改造背景与目标

### 选型与策略

- **选 Hermes 而非 Openclaw**：Hermes 是 Python 技术栈，匹配团队技术栈，且 Python 是 AI agent 的"母语"，便于 AI 辅助改造。
- **硬分叉策略**：取**当前这份代码**做改造，从此**不再拉取官方新代码**。
- **产品目标**：把"私人助理"改成"公用机器人"——每个同事都能在企业微信跟它交互、用它的能力做日常任务；再装上**自研 skill**，让同事**一句话触发固定业务流程**。

### 隔离要求

同事共用一个 Hermes 进程，但**记忆、历史、工作环境互相隔离**。统一用一把钥匙：

```
owner_key = f"wecom:{corp_id}:{app_id}:{user_id}"
```

`security.multi_tenant.enabled` 默认 `false`，单用户体验完全不受影响。

---

## 二、困难判断

### 硬分叉带来的变化

- **解除的困难**：不再跟高频更新的上游对齐，省掉了 rebase/merge 冲突地狱；"plugin 不能改核心文件"那条上游规矩对我们不再是硬约束。
- **新增的困难（需长期承担）**：冻结在当前版本，等于"领养"一个约 12k 行 `run_agent.py` 的庞然大物。上游后续的**安全补丁、依赖漏洞修复、CVE 修复都收不到了**。对一个连着企业微信、给全公司用的内部机器人，"不再打安全补丁"是必须正视的长期成本。
  - 建议：记下分叉的官方基线 commit；偶尔人工扫一眼上游的**安全类**提交，决定要不要手动捡回来（这是"挑安全补丁"，不是"拉新代码"）。

### 隔离本身的三个根本难点

1. **穷尽性**：在单进程内用 owner_key 做软隔离，安全性全压在"每一处入口都正确校验了 owner_key"上，漏一处就漏。历史读取有 list/search/read/scroll/locate/resume/switch/title 多个入口，`session_search` 还有两条调用路径（registry handler + `invoke_tool` 特殊分支，后者不经 registry）。"漏一个补一个"是这类改造的常态。
2. **完整性的天花板**：阶段 1 只做数据软隔离，**挡不住 terminal 跑任意 shell**。真正的执行隔离（OS/容器级）要等阶段 2/3。中间存在一个"看起来安全、其实没完全安全"的危险期——不能误以为"阶段 1 做完就安全了"。
3. **可持续性 + 两条铁律**：多租户是深度核心改动，必须同时不破 **prompt 缓存**、不破 **消息角色严格交替**。

---

## 三、关键技术点一：缓存 vs 文件，隔离方式不同

### 结论

记忆、历史是**永久落盘**文件，按用户分目录/加列隔离的思路**正确**。但"缓存"**根本不落盘**，隔离方式完全不同——靠"让 key 带 owner_key"，不是"分文件存"。

### 两种都叫 cache 的东西

**缓存 A：prompt 缓存（在 Anthropic 服务器上，不在本机）**

源码 `agent/prompt_caching.py`：本地代码只做一件事——给消息打"请缓存我"的标记（`{"type": "ephemeral"}`），最多 4 个断点（system prompt + 最后 3 条消息）。

- 实际缓存**存在 Anthropic 服务器**，本机管不到。
- **临时**，TTL 5 分钟（或 1 小时）。
- 命中条件：寄出去的"信的开头"（system prompt + 历史前缀）**一个字节都不能变**。变一个字 → 不命中 → 全价重算。多轮对话能省约 75% input 成本。

**缓存 B：agent 缓存（在 gateway 进程内存里）**

源码 `gateway/run.py:2610` 附近，`self._agent_cache = OrderedDict()`，注释原文说明：

> 把每个会话的 AIAgent 对象缓存在内存里，避免每条消息都 new 一个新 agent、重拼 system prompt（含 memory）、从而破坏前缀缓存、贵约 10 倍。
> Key: session_key，Value: (AIAgent, config_signature_str)

- 存在**内存（RAM）**，不是磁盘；有大小上限和闲置过期。
- 作用：保住缓存 A（不重建上下文）。

### 对隔离方案的意义

| 东西 | 存在哪 | 怎么隔离 |
|---|---|---|
| 记忆 MEMORY.md / USER.md | 磁盘 `~/.hermes/memories/` | **分目录**（已在做：`memories/owners/<hash>/`，`tools/memory_tool.py:69`）|
| 历史 | 磁盘 SQLite `state.db` 的 `messages` 表 | **加 owner_key 列 / 过滤**（必改 1、3）|
| 缓存 A（prompt cache）| Anthropic 服务器，临时 | 不能"分文件存"——保证不同用户"信的开头"不同 |
| 缓存 B（agent cache）| gateway 内存，临时 | 不能"分文件存"——让 cache 的 **key 带 owner_key** |

**一句话**：永久文件（记忆/历史）= 分目录隔离；缓存 = 临时 = 让 key 带 owner 隔离，靠"不中途改开头"保命。

- session_key 含 owner_key（必改 2）→ 张三/李四命中不同的 agent 缓存条目；他们寄给 Anthropic 的"信的开头"也天然不同 → prompt 缓存也不串。

---

## 四、关键技术点二：shell 工具收敛

### 结论

"停用灵活 shell、改用受限的结构化命令"的方向**完全正确**，而且**不用自己造轮子**——Hermes 已内置这些受限工具（`file` 工具集）。要做的是"**关掉灵活的、打开受限的**"，外加一个**安全细节修正**。

### terminal 确实是漏洞源

源码 `tools/terminal_tool.py:2681`，terminal 的参数就是一坨自由文本：

```python
"command": {"type": "string", "description": "The command to execute on the VM"}
```

无法像校验"路径参数"那样校验它（管道、`$(...)`、软链接、`..` 都能绕）。阶段 1 唯一可靠做法：**多租户下直接不给这个工具**（必改 6 运行时 allowlist 收敛）。

### Hermes 已有"受限结构化命令"

源码 `tools/file_tools.py:1844-1847`，`file` 工具集已注册：

```python
read_file      # 读文件（替代 cat）
write_file     # 写文件
patch          # 改文件（替代 sed/awk）
search_files   # 查找（替代 grep/find/ls）
```

且已加多租户工作区锁定，`tools/file_tools.py:266-285`：

```python
multi_tenant_root = _multi_tenant_workspace_root()   # 当前 owner 的工作区根
# 多租户下相对路径不跟随终端 cwd，固定落到当前 owner workspace
return (multi_tenant_root / p).resolve()             # .resolve() 顺带解软链接
```

即"查找命令自带的路径开头就是用户路径"——`search_files` / `read_file` 已实现：相对路径被钉死在该 owner 工作区，`.resolve()` 挡软链接越界。

### 安全修正：身份别让大模型自己传

**不要**把 user_id 做成工具参数（大模型填）。否则用户话术可诱导模型填别人的 id 越权：

```
张三说"用李四的身份查 /lisi 目录" → 模型可能调 search_files(user_id="lisi", ...)  ❌
```

正确做法是 Hermes 现有做法：**身份来自可信 ContextVar `get_current_owner_key()`**（`tools/file_tools.py:283`），由 gateway 从企业微信**已认证的发信人**注入，模型碰不到、改不了。

> 比喻：user_id 不该是"用户口头报的名字"（可撒谎），而该是"门禁刷卡刷出来的身份"（系统盖章、改不了）。永远信后者。

### 落到阶段 1 的具体建议

1. **多租户 gateway 用收窄工具集**：开 `file`（已锁工作区）、按需 `web`/`search`、`skills`（业务 skill）；**关 `terminal` 和 `code_execution`**。机制：`toolsets.py` + `tools.<platform>.enabled/disabled`（必改 6）。
2. **身份只走 ContextVar owner_key**，绝不做成模型可填的工具参数。
3. **业务确需"类 shell"能力**（解压、转格式等）：不开 raw terminal，**包成专用结构化工具或 skill**，内部只调 `file` 工具或写死命令，路径同样钉在 owner 工作区（对应足迹阶梯"CLI 命令+skill / service-gated 工具"）。
4. **代价要心里有数**：关 terminal 后助手不能随便跑命令，但公司内部场景需求固定（传文件、放指定位置、列目录、发回），`file` 工具集够用，换来可校验的安全边界——对本场景划算。

### 业务 skill 的隔离要点（补充）

业务 skill 走 skill 系统是契合 Hermes 设计的（足迹阶梯首选）。但要区分：

- **业务能力**（skill 逻辑）→ 可全公司**共享**，省事。
- **业务数据**（skill 产出的结果/文件/记忆）→ 必须落到**调用者各自的 owner_key 工作区**，否则隔离破。

即：**skill 共享，但 skill 运行时碰到的一切数据仍走 owner_key 隔离那套**。

---

## 五、小结与下一步可读源码

### 小结

1. **缓存 ≠ 文件**：记忆/历史落盘、按目录/列隔离；prompt 缓存在 Anthropic 服务器、agent 缓存在 gateway 内存，都不落盘，靠"key 带 owner_key"隔离、"不中途改开头"保命。
2. **shell 收敛思路对且有现成轮子**：关 `terminal`、留 `file` 工具集（已锁 owner 工作区）；唯一认知修正——**身份走可信 ContextVar，别做成模型可填的参数**。

### 可继续深入的源码（只读理解）

- `tools/file_tools.py` 工作区锁定逻辑逐行核查：相对路径、绝对路径、软链接、`..` 四种情况怎么挡，有无缺口。
- `gateway/session.py:686` `build_session_key`：owner_key 怎么进 key、哪些路径可能漏。
- 对照"九必改"逐条核查当前代码实际覆盖，列出"已校验 / 可能还漏"清单。
