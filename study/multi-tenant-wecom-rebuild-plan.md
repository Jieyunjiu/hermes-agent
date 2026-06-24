# Hermes 多租户改造方案（企业微信场景）——v7（九必改定稿）

> **核心目标**：在阶段 1 中，把 Hermes 从“个人助手”改造成“单个 gateway 进程服务多个企业微信用户”的内部助手入口，并确保每个企业微信用户的数据默认互相隔离。
>
> **阶段 1 必须闭环的隔离面**：路由、历史读取、会话恢复、记忆、工作区、上传附件、工具面收敛，全部按同一个 **owner key** 隔离。
>
> **阶段 1 不解决的范围**：不做 OS/container 级执行隔离，不做跨用户共享知识库，不做管理员审计，不做跨用户授权协作，不开放用户个性化 MCP / 动态 plugin toolset 生态。这些能力放到阶段 2/3。
>
> **本文所有断言均带源码行号**，已通过六轮验证（三个 Explore agent + 五轮安全审查，2026-06-24）。
>
> **v7 变更（对照 v6）**：① **新增必改 9：`/resume` + `switch_session` 必须校验 owner**（v6 漏了 gateway slash command 恢复路径，是主线隔离漏洞）；② compression 子 session 真实路径补入文件清单（`agent/conversation_compression.py:596`，v6 的 grep 范围漏了 `agent/`）；③ **plugin 禁用措辞精准化**——WeCom 本身是 platform plugin（`adapter.py:1830`），「禁用 plugin 系统」会误伤；`/reload-mcp`（`run.py:8240`）是 slash command，allowlist 管不到；④ 新增统一 helper `assert_session_owner()` 建议。

---

## 0. 九必改 × v6 对照（本次定稿）

| 必改 | 要求 | v6 状态 | v7 处理 |
|------|------|---------|---------|
| 1 统一 owner key | 所有隔离用同一个 key | ✅ | 保留（§1） |
| 2 owner key 进 session 路由 | `build_session_key` 含隔离维度 | ✅ | 保留（§2） |
| 3 历史读取四种形态 owner 校验 | list/search/read/scroll/locate | ✅ | 保留（§4） |
| 4 所有 session 创建写 owner | 含 compression 子 session | ⚠️ grep 范围不全 | 补 `agent/conversation_compression.py:596` + 全仓库 grep（§5） |
| 5 Memory 三路径 + fail-closed | live/offline/pending | ✅ | 保留（§6） |
| 6 运行时 allowlist 收敛 | gateway 创建 agent 前收敛 | ✅ | 保留（§8） |
| 7 file 真实路径校验 | resolve() + symlink | ✅ | 保留（§7） |
| 8 上传附件按 owner 隔离 | media 落 workspace | ✅ | 保留（§9） |
| **9 /resume 会话恢复校验 owner** | resume/switch/title 全校验 | ❌ **未覆盖** | **新增（§4b，主线漏洞）** |

---

## 1. 必改 1：统一 owner key（两方案任选）

```
owner_key = f"wecom:{corp_id}:{app_id}:{user_id}"   # 如 wecom:ww1234:1000001:zhangsan
```
- callback adapter 已能拿 corp_id（`plugins/platforms/wecom/callback_adapter.py:352`）；普通 adapter 需补（`adapter.py:506`）。
- 目录名对 owner_key 做 hash（`sha256[:16]`）防碰撞 + 路径注入。
- **全链路一律用 owner_key**。

| 方案 | 做法 |
|------|------|
| **A（推荐）** | `sessions` 新增 `owner_key TEXT` 列（`hermes_state.py:523` + migration）；`user_id` 保留平台原始值 |
| **B（最小 diff）** | 复用 `sessions.user_id` 列存 owner_key |

> 无论哪种，**全部隔离层用同一个 key**。

---

## 2. 必改 2：owner key 进 session 路由

### 问题
`build_session_key`（`gateway/session.py:678`）在 DB **之前**用 `source.chat_id`/`user_id` 复用 cached agent。WeCom DM 的 chat_id = 裸 sender_id（`adapter.py:507`），key = `agent:main:wecom:dm:zhangsan`。**两个 corp 的 zhangsan → key 相同 → DB 前就复用同一 agent（history bleed）**。

### 改动
1. `SessionSource`（`gateway/session.py:93`）携带 owner_key。
2. WeCom adapter 构造 source 时生成 owner_key（`adapter.py:546` + `callback_adapter.py`）。
3. `build_session_key`（`session.py:678`）多租户模式下把 owner_key 纳入 key 组成。
4. `_set_session_env`（`gateway/run.py:12792`）注入 owner_key → `copy_context()`(`run.py:12809`) 传播。
5. 统一读 `get_current_owner_key()`（§3）。

---

## 3. owner key 获取与 session owner 校验（fail-closed）

### `get_current_owner_key()`
读 §2 注入的 ContextVar。**fail-closed**：多租户模式下取不到 owner_key 必须拒绝请求。

### session owner 校验是必改，helper 是推荐实现方式
`/resume`、`session_search`、`_read_session`、`_scroll`、`_locate_session_db` 等入口都必须校验目标 session 是否属于当前 owner。

推荐新增 `assert_session_owner(session_id, owner_key)` 作为统一 helper，避免每个入口手写校验导致漏改：
```python
def assert_session_owner(db, session_id: str, owner_key: str) -> Optional[str]:
    """校验 session 归属。返回 None 通过，否则返回错误字符串。"""
    session = db.get_session(session_id)
    if not session:
        return f"session not found: {session_id}"
    if session.get("owner_key") != owner_key:
        return f"session not found: {session_id}"  # 故意返回 not found，不泄漏存在性
    return None
```
> 关键：必改的是「所有相关入口都必须完成 owner 校验」。`assert_session_owner()` 只是推荐的统一实现方式；如果不用这个 helper，也必须达到同等校验效果。owner 校验失败时返回「not found」而非「forbidden」，**不泄漏 session 存在性**（防枚举）。

session_search **两条调用路径**都必须拿到 owner_key 并完成 owner 校验，推荐复用上述 helper：
1. registry handler（`tools/session_search_tool.py:783`）
2. `invoke_tool` 特殊分支（`agent/agent_runtime_helpers.py:1807-1827`）——**不经 registry**

---

## 4. 必改 3：历史记录四种形态 owner 校验

### DB 查询加 owner 过滤（`hermes_state.py`）
`search_messages`(`:3466`)、`list_sessions_rich`(`:2105`)、`search_sessions`(`:3834`)、`search_sessions_by_id`(`:3787`) 加 `owner_key` 参数 + SQL `WHERE owner_key = ?`。

### 直读路径逐一点名（owner 校验，推荐复用 `assert_session_owner`）
| 形态 | 函数 | 行号 | 改动 |
|------|------|------|------|
| read | `_read_session` | `tools/session_search_tool.py:178` | owner 校验 |
| scroll | `_scroll` | `tools/session_search_tool.py:270` | 校验 owner；lineage rebind 校验目标 owner |
| locate | `_locate_session_db` | `tools/session_search_tool.py:134` | 多租户禁用跨 profile 扫描或带 owner 校验 |

---

## 4b. 必改 9：`/resume` 会话恢复校验 owner（v7 新增，主线漏洞）

### 问题（v6 漏掉）
`/resume` 的恢复路径**全程无 owner 校验**：
```
/resume <session_id 或 title>
  ├─ gateway/slash_commands.py:3128  get_session(name)        ← 直接按 id 查，无 owner
  ├─ gateway/slash_commands.py:3132  resolve_session_by_title ← 按 title 全局查（hermes_state.py:2005 WHERE title=?，无 owner）
  ├─ gateway/slash_commands.py:3138  resolve_resume_session_id← 跟随 compression continuation chain
  └─ gateway/session.py:1302         switch_session           ← 切换 session_key 到目标，加载旧历史
```
**用户 A 只要知道 B 的 session_id 或 title，`/resume B_session_id` 就把当前会话切到 B 的历史**。这是主线隔离漏洞——即使 session_search 加了过滤，`/resume` 仍能绕过。

### 改动
1. **`/resume <session_id>`**（`slash_commands.py:3128`）：`get_session(name)` 后立即做 owner 校验，不通过则返回 not found。
2. **`/resume <title>`**（`slash_commands.py:3132`）：`resolve_session_by_title` 加 `owner_key` 参数（`hermes_state.py:2005` 的 SQL 加 `AND owner_key = ?`），或解析后做等价 owner 校验。
3. **`resolve_resume_session_id`**（`slash_commands.py:3138`）：跟随 compression continuation 后，对**最终目标**再做 owner 校验。
4. **`/sessions <target>`**（委托 `/resume`）：由同一校验覆盖。
5. **`switch_session`**（`session.py:1302`）：作为 defense-in-depth，切换前也校验 owner（万一上游漏了）。

> **验收关键**：用户 A 不能通过 B 的 session id/title 恢复 B 的会话。这比列表过滤测试更重要。

---

## 5. 必改 4：所有 session 创建写 owner key（补全真实路径）

### 原则
- **子 session 继承父 owner_key**；新用户入口用当前 source owner_key。
- 调用点显式传 owner_key；**同时 `SessionDB.create_session`/`_insert_session_row` 对 `parent_session_id` 做 owner 继承兜底**；多租户模式下仍缺 owner_key 时 fail-closed。

### 覆盖范围（含 v6 漏掉的 compression 真实路径）
| 创建路径 | 源码 | v6 状态 |
|----------|------|---------|
| 普通 gateway session | `gateway/session.py:1056` | ✅ |
| `/branch` | `gateway/slash_commands.py:3288` | ✅ |
| `/reset`、`/resume`、`/fork` | gateway/ create_session | ✅ |
| **compression/continuation 子 session** | **`agent/conversation_compression.py:596`** | ❌ **v6 文件清单漏了** |

`conversation_compression.py:596` 的 `create_session(session_id=..., parent_session_id=old_session_id)` 在压缩后创建子 session。若不继承 owner_key，DB 过滤会让 continuation session 错归属或不可见。

### 验证命令（v6 范围不全，改为全仓库）
```bash
grep -rn "create_session(" gateway/ hermes_state.py agent/ tools/ hermes_cli/
```
逐一确认每个调用点都传 owner_key，或依赖 `create_session` 的 parent 继承兜底。

---

## 6. 必改 5：Memory 三路径 + fail-closed

| 路径 | 源码 | 改动 |
|------|------|------|
| live MemoryStore | `memory_tool.py:55,124,149,246,271` + `agent_init.py:1153` | `get_memory_dir(owner_key)` hash 目录 |
| offline `load_on_disk_store` | `memory_tool.py:758` + `slash_commands.py:2402`/`cli_commands_mixin.py:1458` | 透传 owner_key |
| pending 目录 | `write_approval.py:110` | 路径含 owner_key |
| pending payload | `memory_tool.py:805` + `write_approval.py:114` | 绑定 owner_key |
| `/memory approve`/`reject`/`pending` | `slash_commands.py:2402-2406` | 只处理当前 owner_key |

**fail-closed**：owner_key 缺失不退回全局。approval 是产品策略，不承担隔离责任。

---

## 7. 必改 6+7：Workspace（allowlist 收敛 + file 真实路径）

### 必改 7：file 真实路径校验（P0）
`_resolve_path_for_task`（`file_tools.py:259`）出口加 `validate_within_dir`（`path_security.py:15`）：绝对/相对/symlink 均 resolve 后校验。`search_files`（`:1562`）单独校验。workspace 根从 owner_key 推导。

### 必改 6：运行时 allowlist 收敛
gateway 在**最终拿到 enabled_toolsets 后、创建 AIAgent 前**运行时收敛。白名单外工具不进 schema，`tool_search` 找不到。详见 §10。

---

## 8. 必改 8：上传附件按 owner key 隔离

### 问题
WeCom `_extract_media`（`adapter.py:703`）下载 inbound media 到**全局** cache（`IMAGE_CACHE_DIR` `base.py:568`、`DOCUMENT_CACHE_DIR` `:936` 等），`media_urls`（`adapter.py:556`）暴露全局路径。多用户混放 + 与 workspace 校验矛盾。

### 改动
- media 落到 `/data/workspaces/<owner_hash>/uploads/<message_id_or_uuid>/<sanitized_filename>`。
- `media_urls` 只暴露 workspace 内路径。
- 沿用现有 `max_inbound_media_bytes`(`base.py:589`) + `validate_inbound_media_size`(`:610`)。
- file 工具通过 workspace 校验读取（天然在 workspace 内）。
- 改 `_cache_media`（`adapter.py:750`）+ `cache_*_from_bytes`（`base.py:684`）等接受 owner_hash 可选参数。**全局 cache 函数保留**（非多租户路径仍用，避免改坏其他平台）。

---

## 9. 阶段 1 附加边界：外部记忆、扩展工具（plugin 措辞精准化）

### 9.1 外部 memory provider 阶段 1 禁用
`agent_init.py:1168-1171` 配置 `memory.provider` 时加载外部插件。阶段 1 禁用：fail-closed 或忽略 + warning。

### 9.2 MCP / 动态 plugin toolset / skill_manage 禁用（v7 措辞精准化）

⚠️ **重要修正**：WeCom adapter **本身就是 bundled platform plugin**（`plugins/platforms/wecom/adapter.py:1830` 的 `register(ctx)` → `ctx.register_platform`）。**不能写成「关闭整个 plugin 系统」**，否则 WeCom 自身无法加载。

本节只约束「会进入模型工具面的扩展能力」，不关闭平台适配器加载机制。

精准措辞：
- ✅ **保留** bundled platform plugin（加载 WeCom/Telegram/Discord 等消息平台）
- ❌ **禁用** 用户/动态 plugin **toolset** 注入（plugin 注册的 model tools，不是平台 adapter）
- ❌ **禁用** MCP 工具发现
- ❌ **禁用 `/reload-mcp`**（`gateway/run.py:8240`）——这是 slash command，**allowlist 收敛管不到它**，多租户模式下必须让它直接返回拒绝
- ❌ **禁用** `skill_manage`（用户不能动态装改删 skills）
- ✅ **允许** 公司统一部署的 skills 被调用，但只允许调用，不允许用户通过工具安装、修改、删除 skills
- 最终模型工具面以运行时 allowlist 为准

> **关键**：`/reload-mcp`（`run.py:8240`）是 slash command 路径，不经过 AIAgent 的 toolset 构建。只做 allowlist 收敛**不够**，必须单独拦截这个命令。同理检查 `/reload-skills` 等其他动态注入入口。

---

## 10. 阶段 1 工具策略（运行时 allowlist）

### 允许（白名单）
| 工具 | 前提 |
|------|------|
| `memory` | owner_key 隔离完成 |
| `session_search` | owner_key 隔离完成 |
| `read_file` / `write_file` / `patch` / `search_files` | workspace 校验完成（含上传文件，必改 8） |
| `clarify` | — |
| `todo` | — |
| 可选 `web_search` / `web_extract` | 按业务评估 |
| 公司统一部署 skills 的调用 | 只读，skill_manage 禁用 |

### 禁用
`terminal`/`process`/`execute_code`/`delegate_task`；`skill_manage`；`cronjob`；`browser_*`/`computer_use`；**MCP**；**动态 plugin toolset**（注意：**不是** platform plugin）；**外部 memory provider**。

### 必须单独拦截的 slash command（allowlist 管不到）
- `/reload-mcp`（`run.py:8240`）
- `/reload-skills`（`run.py:8244`）
- 其他动态注入入口（grep 确认）

---

## 11. 并发与 ContextVar（已验证可靠）

- 注入：`SessionSource.owner_key` → `set_session_vars`（协程 `gateway/run.py:8989`）→ `copy_context()`(`run.py:12809`) → executor 线程
- set/clear 非栈式（`session_context.py:175` 用 `.set("")` 非 `.reset`），不可嵌套
- `_UNSET` 哨兵区分「未设」/「显式空」
- SessionDB 并发写需 WAL 模式
- `get_memory_dir` 每次动态调用（无模块缓存）✅

---

## 12. 不改的（避坑）

| ❌ 不要 | 原因 |
|--------|------|
| owner key 与 user_id 混用 | 多 corp 同名碰撞（必改 1） |
| 只在 DB 层过滤，不进 session 路由 | DB 前就复用 cached agent（必改 2） |
| **`/resume` 不校验 owner** | 直接切到他人历史，主线漏洞（必改 9） |
| media 落全局 cache | 无隔离 + 与 workspace 校验矛盾（必改 8） |
| 用 `_SESSION_CWD` 做 workspace 锚点 | 文件工具不读它 |
| 每用户一个 profile | 破坏 prompt caching |
| 仅靠 Python 层 cd 校验隔离 terminal | shell 能直接执行绝对路径 |
| 只改默认配置做 allowlist | 插件/MCP 会注入，必须运行时收敛（必改 6） |
| 只做 allowlist 不拦 `/reload-mcp` | slash command 绕过 allowlist（§9.2） |
| 「关闭整个 plugin 系统」 | WeCom 本身是 platform plugin，会误伤（§9.2） |
| 让 approval 承担隔离责任 | approval 是产品策略，不是安全边界 |
| 阶段 1 启用外部 memory provider | 走独立存储，不经 MEMORY.md（§9.1） |

---

## 13. 分阶段（v7）

### 阶段 1：最小安全闭环（P0）——九必改全落地，allowlist 工具集
| 必改 | 内容 |
|------|------|
| 1 | owner key 模型 + `get_current_owner_key()` + session owner 校验（推荐 `assert_session_owner()`，fail-closed） |
| 2 | owner key 进 `build_session_key` + SessionSource + adapter + ContextVar |
| 3 | 历史读取四种形态 owner 校验（DB + read/scroll/locate） |
| 4 | 所有 create_session 写 owner_key（**含 `conversation_compression.py:596`**） |
| 5 | Memory live + offline + pending 全绑 + fail-closed |
| 6 | gateway 创建 agent 前运行时 allowlist 收敛 |
| 7 | file 真实路径校验（含 symlink） |
| 8 | 上传附件落 workspace，media_urls 暴露 workspace 内路径 |
| **9** | **`/resume` + `switch_session` + `resolve_session_by_title` 全部 owner 校验** |
| §9 | 禁用外部 memory provider / MCP / **动态 plugin toolset（非 platform plugin）** / skill_manage / **`/reload-mcp`** |

**验收**：两用户路由/历史/记忆/文件/上传/会话恢复全隔离；白名单外工具不可用；**用户 A 知道 B 的 session id/title 也无法 `/resume` 到 B**；A 上传文件 B 读不到；不同 corp 同名张三路由到不同 agent。

### 阶段 2：执行隔离（OS 级）
per-user 容器或 OS namespace。

### 阶段 3：运维与企业能力
数据迁移、共享知识库、管理员审计、跨用户授权共享、横向扩展。

---

## 14. 验证不变量（E2E）

```bash
scripts/run_tests.sh tests/tools/test_memory_tool.py -q
scripts/run_tests.sh tests/hermes_state/ -q
scripts/run_tests.sh tests/gateway/ -q
scripts/run_tests.sh tests/agent/ -q
```

**基础不变量**：
- `get_memory_dir(owner_a) != get_memory_dir(owner_b)`
- `/branch` session.owner_key == 父 session.owner_key
- `read_file("/data/workspaces/<owner_b>/secret")` 被拦截

**历史隔离 + 会话恢复不变量（必改 3+9，最关键）**：
- 用户 A 用 `session_search(session_id=B_session_id)` 读不到 B
- 用户 A 用 `around_message_id` 指向 B 的消息读不到 B
- **用户 A 用 `/resume <B_session_id>` 无法恢复 B 的会话**（必改 9）
- **用户 A 用 `/resume <B_title>` 无法恢复 B 的会话**（必改 9）
- **`/resume` 经 compression continuation 后仍校验最终目标 owner**（必改 9）
- `build_session_key(corp_a_zhangsan) != build_session_key(corp_b_zhangsan)`（必改 2）

**上传附件不变量（必改 8）**：
- 用户 A 上传文件后路径位于 `workspace/<owner_a>/uploads/...`
- 用户 A 不能读取 `workspace/<owner_b>/uploads/...`
- 上传文件路径不暴露全局 cache

**工具/扩展不变量**：
- `tool_search` 搜不到白名单外工具
- 阶段 1 下 `skill_manage`/`cronjob`/`browser_cdp`/`computer_use`/MCP/动态 plugin toolset 不在工具列表
- **多租户模式下 `/reload-mcp` 被拒绝**（§9.2）
- 公司统一 skills 可被触发但不能通过工具修改
- 多租户模式下配置 `memory.provider` 时外部 provider 不被加载

**workspace 不变量**：
- symlink 指向 workspace 外时被拒绝
- owner_key 缺失时 memory 写入 fail-closed 拒绝

---

## 15. 改动文件清单（v7）

| 文件 | 改动 | 必改 | 优先级 |
|------|------|:---:|:---:|
| 新增 owner 上下文与校验能力 | `get_current_owner_key()` + session owner 校验（推荐 `assert_session_owner()`）+ owner_key 构造 + hash | 1,2,3,9 | P0 |
| `gateway/session.py`（`SessionSource:93` + `build_session_key:678` + `switch_session:1302`） | 携带 owner_key + key 纳入 + **switch 校验 owner** | 2,9 | P0 |
| `gateway/session_context.py` + `gateway/run.py`（`:12792,8989`） | owner_key ContextVar 注入 | 2 | P0 |
| `plugins/platforms/wecom/adapter.py`（`:506,546`） + `callback_adapter.py` | adapter 生成 owner_key（补 corp_id） | 2 | P0 |
| `hermes_state.py`（`:523,3466,2105,3834,3787,2005`） | 新增 owner_key 列 + migration + 读取 API 过滤 + **`resolve_session_by_title` 加 owner** + **`create_session` parent 继承兜底** | 1,3,4,9 | P0 |
| `tools/session_search_tool.py`（`:178,270,134,783`） | read/scroll/locate owner 校验 + handler 传 owner_key | 3 | P0 |
| `agent/agent_runtime_helpers.py`（`:1807-1827`） | invoke_tool session_search 分支传 owner_key | 3 | P0 |
| **`gateway/slash_commands.py`（`:3128,3132,3138,3288` + 其他 create_session）** | **`/resume` 三路径 owner 校验** + 所有 session 创建写 owner_key | 4,9 | P0 |
| **`agent/conversation_compression.py`（`:596`）** | **compression 子 session 继承 owner_key**（v6 漏了） | 4 | P0 |
| `tools/memory_tool.py`（`:55,124,149,246,271,758,805`） | live + offline + pending 绑 owner_key | 5 | P0 |
| `agent/agent_init.py`（`:1153,1168`） | 实例化传 owner_key；多租户禁用外部 provider | 5,§9 | P0 |
| `tools/write_approval.py`（`:110,114`） | pending 绑 owner_key | 5 | P0 |
| `gateway/slash_commands.py`（`:2402,2388`） | approve/reject/pending 按 owner_key 过滤 | 5 | P0 |
| `tools/file_tools.py`（`:259,1562`） | 真实路径校验 + symlink | 7 | P0 |
| `plugins/platforms/wecom/adapter.py`（`_extract_media:703` + `_cache_media:750`） | media 落 workspace uploads | 8 | P0 |
| `gateway/platforms/base.py`（`cache_*_from_bytes:684` + `get_*_cache_dir:661`） | 接受 owner_hash 可选参数，落点改 workspace | 8 | P0 |
| **`gateway/run.py`（`:8240,8244` AIAgent 创建 `:15316,11286`）** | **`/reload-mcp`/`/reload-skills` 多租户拒绝** + allowlist 收敛 | 6,§9 | P0 |
| `hermes_cli/config.py` DEFAULT_CONFIG | 多租户模式开关 + workspace_root + allowlist | 6 | P0 |
| `tools/environments/docker.py` 或新增沙箱 | per-user 容器隔离 | — | P1 |

---

## 附录：源码定位速查（★ = 审查新增，◎ = v7 新增）

| 要做的事 | 看哪里 |
|---------|--------|
| user_id 获取 | `plugins/platforms/wecom/adapter.py:505-547` |
| ★ corp_id（callback adapter） | `plugins/platforms/wecom/callback_adapter.py:352` |
| ◎ `build_session_key`（路由层） | `gateway/session.py:678` |
| ◎ SessionSource 定义 | `gateway/session.py:93` |
| ◎ **`switch_session`（必改 9）** | `gateway/session.py:1302` |
| 普通 session 写 user_id | `gateway/session.py:1056` |
| ★ `/branch` 缺 user_id | `gateway/slash_commands.py:3288` |
| ◎ **`/resume` 三路径（必改 9）** | `gateway/slash_commands.py:3128,3132,3138` |
| ◎ **`resolve_session_by_title` 无 owner** | `hermes_state.py:2005` |
| ★ session 读取 API 无过滤 | `hermes_state.py:3466,2105,3834,3787` |
| ★ `_read_session`/`_scroll`/`_locate_session_db` | `tools/session_search_tool.py:178,270,134` |
| ★ invoke_tool session_search 分支 | `agent/agent_runtime_helpers.py:1807-1827` |
| ◎ **compression 子 session 创建（必改 4 真实路径）** | `agent/conversation_compression.py:596` |
| Memory 目录收口 | `tools/memory_tool.py:55`（`:149,246,271`） |
| MemoryStore live/offline | `agent/agent_init.py:1153`；`tools/memory_tool.py:758` |
| ◎ 外部 memory provider 加载 | `agent/agent_init.py:1168-1171` |
| write_approval pending 全局 | `tools/write_approval.py:110,114` |
| file 路径解析 | `tools/file_tools.py:200,223,259,1562` |
| 路径校验（已有，核心工具未用） | `tools/path_security.py:15` |
| ◎ inbound media 落全局 cache（必改 8） | `adapter.py:703,750`；`base.py:568,684,936` |
| ◎ **WeCom 本身是 platform plugin（§9.2）** | `plugins/platforms/wecom/adapter.py:1830 register(ctx)` |
| ◎ **`/reload-mcp` slash command（§9.2）** | `gateway/run.py:8240` |
| ◎ `/reload-skills` slash command | `gateway/run.py:8244` |
| terminal workdir/无沙箱 | `tools/terminal_tool.py:1819`；`tools/environments/local.py:774` |
| ◎ AIAgent 创建点（allowlist 收敛） | `gateway/run.py:15316,11286` |
| 容器后端（阶段 2） | `tools/environments/docker.py` |
| session ContextVar set/clear | `gateway/session_context.py:124,175` |
| `_set_session_env` 调用（协程） | `gateway/run.py:12772,8989,10224` |
| `copy_context` 传播 | `gateway/run.py:12809` |
| 传播回归测试 | `tests/gateway/test_session_env.py:284-326` |
