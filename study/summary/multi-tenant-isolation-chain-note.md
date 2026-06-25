# 专题笔记：Hermes 多租户隔离链（端到端）

## 笔记概览

这份笔记沿一条用户消息的生命周期，串起 Hermes 把"单人私人助理"改造成"单进程服务多企业微信用户、数据默认隔离"的**完整隔离链**：入站生成身份 → 路由防串号 → 共享存储按 owner 分区 → ContextVar 把身份带进工具层 → 工具层隔离干活（含 fail-closed）→ 出站按地址回复。

**两个贯穿全程的标识，职责不同，务必分清**：

| 标识 | 是什么 | 管什么 | 唯一性 |
|---|---|---|---|
| **owner_key** | `wecom:{corp_id}:{app_id}:{user_id}`，用时哈希成 `sha256[:16]` | **隔离**（谁的数据）| **全局唯一** |
| **chat_id** | 平台原生会话标识（DM 下≈UserID）| **投递**（发回给谁）| 仅在一条连接内有效 |

一个开关：`security.multi_tenant.enabled`，默认 `false`（单用户老行为完全不变）；为 `true` 时下面每一环才生效。

> 阅读提示：本笔记按"用户消息从进到出"的顺序展开，每步给真实代码位置。行号基于当前分叉版本，可能漂移。

---

## 步骤 1：入站——生成 owner_key，构造会话来源结构

用户首次（或每次）交互，适配器从企业微信消息里取**账号 ID（UserID，企业内唯一，不是显示名）**，生成 owner_key。

adapter 两种连接方式

企业微信支持两种把消息送进 Hermes 的"管道"，两者都产出带 owner_key 的 `SessionSource`，后面 loop 完全一样：

| 方式 | 机制 | 代码 |
|---|---|---|
| **主 adapter（WS 长连接）** | 后台建"智能机器人"，WebSocket 长连接接收 | `adapter.py`，`sender.get("userid")` :521 |
| **callback adapter（HTTP 回调）** | 企业微信用 HTTP POST 把加密 XML 推到回调 URL | `callback_adapter.py`，`FromUserName` :351 |

> `sender` 就是企业微信传来的信息包对象（类似 web 的 `request`），按固定字段取数据（`userid` 等）。

- 主 adapter（WS 智能机器人，`plugins/platforms/wecom/adapter.py`）：
  ```python
  # 这里的 sender 实际上就是类似 web开发中的request，类似一个企业微信提供的传递信息的信息包对象
  sender_id = str(sender.get("userid"))              # :521  UserID，不是中文名
  _owner_key = build_owner_key(self._corp_id, self._bot_id, sender_id)  # :545
  ```
- callback adapter（`plugins/platforms/wecom/callback_adapter.py`）：
  ```python
  user_id = root.findtext("FromUserName")            # :351  FromUserName = UserID
  owner_key = build_owner_key(corp_id, app_id, user_id)  # :372
  ```
- `build_owner_key`（`gateway/multi_tenant.py:79`）只是拼字符串 → `wecom:corp:app:user`；**哈希在用时才做**（`hash_owner_key` :114，`sha256[:16]`，纯十六进制防路径注入）。

**🔧 纠正你的步骤 1 表述**：创建的不是"chat_id + key"这么两个字段，而是一个 **`SessionSource` 结构**，里面同时带 `platform / chat_type / chat_id / user_id / thread_id / owner_key …`。owner_key 和 chat_id 都是它的字段，一起往后走。

> 关键：corp_id、app_id 在**渠道创建时**就固定（标识哪个机器人，所有用户共享）；user_id **每条消息现算**。所以"一个渠道 = 固定 corp/app + 千变万化的 user → 每条消息一个专属 owner_key"。

---

## 步骤 2：路由 + 存储——一个共享 DB，按 owner“分区”，不是按 key 建库

**🔧 重点纠正你的步骤 2「根据 key 创建 db」**：

> **不会给每个用户建一个数据库。** 整个进程**只有一个共享的 SQLite**（`state.db`，`hermes_state.py` 的 `SessionDB`）。owner 维度是**写进 session_key / 作为隔离条件**，在这个共享库里**逻辑分区**——不是物理上一人一个库。

两层"分区"机制：

1. **路由键带 owner（必改 2，`gateway/session.py:686` `build_session_key`）**：
   ```python
   if source.owner_key:
       owner_segment = hash_owner_key(source.owner_key)   # :732
       ns = f"{ns}:o{owner_segment}"                        # :734  owner_hash 进 namespace 前缀
   ```
   效果：两个企业里都叫 zhangsan 的用户，session_key 不再相同 → 不会复用同一个内存 agent → 杜绝 history bleed。
   注释点破要害（:728-729）：**owner 必须在路由键这一层就生效，因为 agent 缓存复用发生在查 DB 之前；只在 DB 查询里过滤来不及。**

   **`ns` 是什么**：它是 session_key 最前面的**命名空间段**（不是"窗口名"）。`_session_key_namespace`（`session.py:666`）默认 profile → `agent:main`（`main` 是固定字面量，原为多 profile 复用），命名 profile → `agent:<profile>`。多租户把 owner 哈希插进这个前缀：
   ```
   单人：    agent:main : wecom:dm:zhangsan001
   多租户：  agent:main:o<owner_hash> : wecom:dm:zhangsan001
                        ↑ owner 维度进前缀 → 两个企业的同名用户 key 不同
   ```

2. **session 行带 owner 列**：所有 session 创建（含 `/title` fallback、`/reset`、compression 子 session）都写入 owner_key，保证查询能按 owner 过滤。

> 给初学者：把它想成**一个大账本（共享 DB），每条记录多了一列"归属人(owner)"**；不是"一人发一本账本"。查谁的记录就按这列过滤。

---

## 步骤 3：读取消息队列——按 session 加载 + owner 校验防枚举

按 session_key（已含 owner_hash）找到内存里的 agent；冷启动时从共享 DB 按该 session 加载历史文本。

**DB 是两张表（共享库，按 owner 逻辑分区）**：

```
表1 sessions（会话元数据，一会话一行）       表2 messages（消息队列，一会话多行）
┌──────────────────────────────────┐        ┌──────────────────────────────────┐
│ id (session_id)      PRIMARY KEY  │        │ id              PRIMARY KEY        │
│ owner_key  ← 归属人(必改1)         │◄──┐    │ session_id  ──► 外键关联 sessions.id│
│ user_id / source / title          │   └────│ role (user/assistant/tool)        │
│ parent_session_id (压缩链)         │        │ content                           │
│ started_at / ended_at             │        │ tool_calls / timestamp / active   │
└──────────────────────────────────┘        └──────────────────────────────────┘
   owner_key 在【这张表】              messages 不带 owner_key，靠 session_id 关联
```

**取消息队列 = 三步**（owner 过滤在 sessions 层，不在 messages 层）：

```
① session_key ──映射──► 当前活跃 session_id
② 在 sessions 找到该行，校验 owner_key == 当前 owner   (assert_session_owner，防枚举)
③ SELECT * FROM messages WHERE session_id = ?          ← 这才是消息队列
```

**session_key → sessions → messages 关系草图**：

```
session_key  "agent:main:o<hash>:wecom:dm:zhangsan001"   ← 路由句柄(确定性算出, 在内存)
     │  SessionStore 把它映射到"当前活跃 session_id"
     ▼
┌─ sessions 表 ───────────────────────────────────────────────┐
│ id=20260625_..ab12  owner_key=wecom:corpA:bot:zhangsan001  active │ ← 当前会话
│ id=20260624_..cd34  owner_key=wecom:corpA:bot:zhangsan001  ended  │ ← /new 前的旧会话(仍在!)
└───────────────┬──────────────────────────────────────────────┘
                │ session_id 外键
                ▼
┌─ messages 表 ───────────────────────────────────────────────┐
│ session_id=20260625_..ab12  role=user       "帮我查..."        │
│ session_id=20260625_..ab12  role=assistant  "好的..."          │
│ session_id=20260624_..cd34  role=user       "（旧会话记录,可 /resume 找回）"│
└──────────────────────────────────────────────────────────────┘
```

凡是"按 session_id / 标题"取历史的入口（`/resume`、`session_search`、`_read_session`、`_scroll` …），都要校验目标 session 属不属于当前 owner，推荐统一用 helper（`gateway/multi_tenant.py:283`）：

```python
def assert_session_owner(db, session_id, owner_key):
    # session 不存在 / 存在但属于别人 → 返回【同样的】"not found"
    # 故意不区分两种情况，防止跨用户枚举 session_id / 标题（anti-enumeration）
```

**要点**：owner 校验失败返回 "not found" 而非 "forbidden"——**不泄漏 session 是否存在**。`session_search` 有**两条**调用路径（registry handler + `invoke_tool` 特殊分支，后者不经 registry），**两条都要校验**，漏一条就漏。

**`/new`（reset_session）的真实行为——旧会话封存，不删除**（`gateway/session.py:1275`）：

```python
db_end_session_id = old_entry.session_id          # 旧会话【标记 ended】，不是删除
session_id = f"{now}_{uuid...}"                     # 生成新 session_id
new_entry = SessionEntry(session_id=session_id, ...) # session_key 指向新会话
# 新会话继承 owner_key（:1312-1315）→ 仍属于同一个人
```

- **旧 session_id 和它的 messages 全部保留**，可经 `/resume`、`/sessions`、`session_search` 找回。
- 所以**不存在"废弃记录找不回"的问题**——Hermes 设计上**会话永不丢失**，`/new` 只是开一个新会话、把旧的封存。
- "换会话只换 session_id，owner_key 继承"——新会话不会变成无主的全局会话。

---

## 步骤 4：ContextVar 绑定 + 进入 loop

**🔧 重点纠正你对 ContextVar 的理解**：

> ContextVar **不是**一个像 `@dataclass` 的大数据结构。它是一个**"按当前请求/任务隔离的单值槽位"**——这里它只装**一个 owner_key 字符串**。可以理解成**"进门挂的工牌"**：每个请求各挂各的，互不串；屋里任何深处的代码都能抬头读这张工牌，不用层层传参。

绑定/传播/清理三步（`gateway/run.py` 的 `_set_session_env`）：

```python
# 绑定：把 owner_key 写进 ContextVar
_owner_key = context.source.owner_key or ""   # :12805
set_current_owner_key(_owner_key)             # :12806
# 传播：agent 在 worker 线程跑，copy_context 把工牌复印一份带过去
ctx = copy_context()                          # :12830
# 清理：处理完摘牌，防跨请求泄漏
clear_current_owner_key()                     # :12824
```

为什么用 ContextVar 而不是当参数传？因为 `AIAgent` 有约 60 个参数、工具几十个，owner_key 要到每个角落——**漏传一处就是泄漏点**。ContextVar 让身份变成"环境里随手可读"，边界处设一次、深处随时读、自动随 `copy_context()` 传播。这是"环境上下文 / 依赖注入"模式，避免参数穿透地狱。

**ContextVar 到底是什么 + 典型用途**（定义 `gateway/multi_tenant.py:72`：`_OWNER_KEY = ContextVar("HERMES_OWNER_KEY", default=_UNSET)`）：

- 它是标准库 `contextvars` 的类，但**不是可加字段的数据结构**——是一个**"按执行上下文隔离的单值槽"**，这里只装一个 owner_key 字符串。用法只有 `.set(v)` / `.get()`。
- 解决的痛点：**普通全局变量一处改、处处变**；ContextVar 让**同一个变量名在不同 async 任务/线程里各看各的值、改动互相隔离**。
- 传播靠 `copy_context()`：agent 在 worker 线程跑时，把当前上下文（含这个变量的值）复印一份带过去。
- **典型用途**（不止 Hermes）：当前请求的用户身份 / 请求 ID / trace 链路 ID / 数据库事务 / locale 语言——这些"每个请求一份、不能互相污染、又要随处可读"的环境值，都用 ContextVar，避免一层层当参数传（漏传即泄漏）。
- 比喻：墙上一块"当前服务对象"白板，但**每个进来的人看到的是他自己那块**——张三的请求白板写"张三"、李四的写"李四"，并发也不串。

补充：少数 slash 命令（`/memory pending`、`/skills pending`）在 `_set_session_env` **之前**执行，用 `scoped_owner_key()`（`multi_tenant.py:168`）临时补绑、用完恢复。

绑定好后进入对话循环 `run_conversation()`（`agent/conversation_loop.py`）：发全量消息列表 → 模型决定是否调工具 → **有工具就先存 DB 再执行**（:4051-4058，先写日志再动手，崩了能 resume）→ 循环到模型不再调工具。

---

## 步骤 5：工具层隔离——身份落地干活

工具运行时读 ContextVar 拿到当前 owner，派生其专属空间。**路径由代码算，模型碰不到。**

```
get_current_owner_key() 读 ContextVar → 派生 owner 专属空间
  ├─ 文件：钉死在 <workspace_root>/<owner_hash>
  │        file_tools.py:279-291 _multi_tenant_workspace_root()
  │        + _validate_multi_tenant_workspace_path()
  │        模型传绝对路径 / ../ 都会被强制/校验回 owner 根目录
  ├─ 记忆：memories/owners/<owner_hash>/   (memory_tool.py:69)
  ├─ 工具面：强制收敛到固定 allowlist
  │        constrain_toolsets_for_owner() multi_tenant.py:227-246
  │        多租户下不信平台/MCP/plugin 注入的工具，强收为 wecom_multi_tenant，
  │        关掉 terminal/process/execute_code/delegate_task/skill_manage/
  │        cronjob/browser/computer_use 等高风险工具
  └─ fail-closed：拿不到 owner_key 直接拒绝，绝不退化成共享
           get_current_owner_key() multi_tenant.py:199-204
           多租户开着却没 owner_key → raise OwnerKeyMissing
```

- **workspace 根**：`owner_workspace_root(owner)`（`multi_tenant.py:258`）= `<workspace_root>/<owner_hash>`，`workspace_root` 来自配置 `security.multi_tenant.workspace_root`，**默认 `/data/workspaces`**（是配置+默认值，不是写死路径；但**派生逻辑写死**）。
- **目录按需创建**：`owner_workspace_root()` 本身**只返回 Path、不 mkdir**；真正建目录发生在**写文件/落附件时**（lazy 创建），不是"首次交互显式建空间"。
- **fail-closed 是整条链的安全网**：万一某条冷路径 owner_key 没绑上，工具层不会用全局路径读写，而是**当场拒绝**——宁可失败，不泄漏。

> 你判断"创建工作环境要硬编码、不让模型推断"——**完全正确，这就是安全命门**：模型能"请求"操作，但"落在哪个目录、用哪些工具"全由代码按 owner_key 决定。

---

## 步骤 6：出站——按地址回复同一个人

循环结束（模型给出最终答案）后，gateway 用**整轮一直保留的原生地址**把结果发回。**出站不用 owner_key**（哈希单向，发不了消息）。

```python
# plugins/platforms/wecom/adapter.py:1504 send()
reply_req_id = self._reply_req_id_for_message(reply_to)   # 优先：按"回复哪条消息"的令牌
if reply_req_id:
    await self._send_reply_markdown(reply_req_id, content) # 精确回到那条消息的发送者
else:
    await self._send_request(APP_CMD_SEND, {"chatid": chat_id, ...})  # 兜底用 chat_id
```

回复为什么落到正确的人：整轮处理都在**这个用户隔离的 session** 里、它一直保留着原生 chat_id / reply_req_id；且**一个企业一条专属连接**，chat_id 只在本连接内解释，跨企业字符串撞名也送不错。前提：**gateway 必须把回复发回当初收消息的那个 adapter（原路返回）**。

---

## 关键不变量（改动红线）

1. **fail-closed**：多租户下拿不到 owner_key 必须拒绝，绝不退化成全局/共享。
2. **不泄漏存在性**：owner 校验失败返回 "not found" 而非 "forbidden"（防枚举）。
3. **owner 必须在路由层生效**：因为 agent 缓存复用在查 DB 之前，只过滤 DB 不够。
4. **session_search 两条路径都要校验**（registry + invoke_tool 特殊分支）。
5. **默认行为不变**：`enabled=false` 时全部退化为单用户老行为。
6. 仍守两条全局铁律：**prompt 缓存不可破** + **消息角色严格交替**。

---

## 认知纠正速查（你这版骨架里需要校准的点）

| 你的表述 | 实际 | 为什么 |
|---|---|---|
| 创建"chat_id + key"数据结构 | 是 `SessionSource`，含 platform/chat_type/chat_id/user_id/owner_key 等多字段 | owner_key、chat_id 只是其中两个字段 |
| 根据 key **创建 db** | **一个共享 DB**，owner 进 session_key / 作隔离列做**逻辑分区** | 不是一人一库；是共享账本加"归属人"列 |
| 根据 key 取 db 消息队列 | 按 **session_key**（含 owner_hash）取 + `assert_session_owner` 校验 | 取 + 校验两步，且防枚举 |
| ContextVar ≈ @dataclass 大结构 | ContextVar 是**按请求隔离的单值槽**，这里只装一个 owner_key **字符串** | 它是"工牌"，不是数据容器 |
| 根据 chat_id 回复 | ✅ 对，且优先用 reply_req_id（更精确）；出站不用 owner_key | owner_key 哈希单向，发不了消息 |

---

## 代码索引（快速定位）

| 环节 | 位置 |
|---|---|
| owner_key 构造 / 哈希 | `gateway/multi_tenant.py:79` `build_owner_key` / `:114` `hash_owner_key` |
| 企业微信入站生成 owner_key | `plugins/platforms/wecom/adapter.py:521,545` / `callback_adapter.py:351,372` |
| 路由键带 owner | `gateway/session.py:686` `build_session_key`（:730-734） |
| ContextVar 读 / 写 / 清 / fail-closed | `gateway/multi_tenant.py:152/185/163`，fail-closed `:199-204` |
| ContextVar 临时绑定（冷命令） | `gateway/multi_tenant.py:168` `scoped_owner_key` |
| 绑定 + 传播 + 清理 | `gateway/run.py:12805-12806 / 12830 / 12824` `_set_session_env` |
| owner 校验防枚举 | `gateway/multi_tenant.py:283` `assert_session_owner` |
| workspace 根 | `gateway/multi_tenant.py:258` `owner_workspace_root` |
| 文件工具钉死 owner | `tools/file_tools.py:279-291` |
| 记忆目录按 owner | `tools/memory_tool.py:69` |
| 工具面强制收敛 | `gateway/multi_tenant.py:227-246` `constrain_toolsets_for_owner` |
| 先存 DB 再执行工具 | `agent/conversation_loop.py:4051-4058` |
| 出站发送 | `plugins/platforms/wecom/adapter.py:1504` `send` |

---

## 一句话总纲

> **身份（owner_key，全局唯一）从入站起绑在 SessionSource 上，贯穿路由（防串号）→ ContextVar 传播 → 工具层隔离（文件/记忆/工具面/fail-closed）；地址（chat_id，连接内投递）管出站回复。一个共享 DB 按 owner 逻辑分区，不是一人一库。隔离的命门是 fail-closed + 路由层就带 owner——因为模型/厂商对多用户零隔离，区分用户全是 Hermes 自己的活。**
