# 多租户企业微信 · 沙箱执行能力恢复方案（设计 spec）

> 状态：待审核（草案 v3，已纳入两轮代码审查反馈，见 §14）
> 日期：2026-06-29
> 关联文档：`study/multi-tenant-wecom-rebuild-plan.md`（多租户主方案）、`study/hermes-agent-architecture-analysis.md`、`study/summary/tool-calling-and-injection-engineering-notes.md`
> 本文只覆盖"在多租户模式下，安全地把执行类能力（一期：`terminal`）还给智能体"这一件事。

---

## 1. 背景与问题

多租户改造（按 `owner_key` = `wecom:{corp_id}:{app_id}:{user_id}` 做数据隔离）落地后，`wecom_multi_tenant` 工具集只保留了受限工具：

```
web_search, web_extract, read_file, write_file, patch, search_files,
skills_list, skill_view, todo, memory, session_search, clarify
```

（见 `toolsets.py` 中 `wecom_multi_tenant` 定义。）

实测接入企业微信、两个测试账号做文件隔离后，暴露两个问题：

- **问题 1（能力被大幅削弱）**：砍掉 `terminal` 后，数据处理类任务做不了。例：用户发一个 Excel，想要一份 Word 数据分析报告——当前无任何代码/命令执行能力，无法完成。这只是一个例子，背后是"为了安全，把智能体一大类能力整体砍掉了"。
- **问题 2（模型幻觉自有能力）**：模型一开始仍会声称自己能跑代码/上网等（训练先验脑补），被质疑后才去查工具列表。

本方案解决问题 1（恢复执行能力且不破坏隔离），并顺带解决问题 2（让模型对自身能力诚实）。

---

## 2. 目标与非目标

### 目标

- 在**多租户模式**下恢复 `terminal`（一期），让智能体能跑 shell / python / pip 等，覆盖数据处理类任务。
- 恢复能力**不削弱隔离**：每个用户的执行环境只能看到自己的 workspace，出不了容器、默认无网络。
- 复用 Hermes 已有的 docker 执行基础设施，**最小 diff**，不新增核心工具（符合"窄腰 + 足迹阶梯"铁律）。
- 让模型对"自己到底有哪些能力"诚实（问题 2）。

### 非目标（本期不做）

- 不开 `code_execution`（PTC）和 `process`（后台进程管理）——见 §11 分期。
- 不改 `owner_key` 构造、不改 workspace 路径布局、不改 session 归属校验逻辑（沿用主方案）。
- 不改单用户（`security.multi_tenant.enabled=false`）默认行为——单用户体验必须零变化。
- 不引入新的沙箱技术栈（不上 gVisor / firecracker / bubblewrap）；用现有 `DockerEnvironment`。

---

## 3. 关键认知（方案为什么这么设计）

这三条决定了方案的对错，审核时请重点看。

### 3.1 "受限脚本执行工具 + 路径校验"挡不住任意代码

只把 cwd 钉死在 owner workspace，只影响**相对路径**的默认落点。一旦允许提交任意 Python / shell，下面几行就绕开了一切路径校验：

```python
open("/data/workspaces/<别人的hash>/secret.xlsx")  # 绝对路径，绕开 cwd
import os; os.system("cat /etc/passwd")              # 直接读宿主机
import urllib.request; urllib.request.urlopen(...)   # 外传数据
```

所以"在进程内/同宿主机用户下跑任意代码 + 路径校验"在安全性上 ≈ 重开 bash，等于白砍。**要安全地恢复任意计算，必须上 OS 级沙箱边界。**

### 3.2 不是"命令白名单"，是"完整 shell 关进笼子"

命令级白名单（只允许 `ls`/`cat`/`python`…）是漏的：`python -c "..."` 一句就绕过。本方案**不限制沙箱内能跑什么**，安全完全靠**容器边界**——无论跑啥都只看得到自己 `/workspace`、出不了容器、默认无网。**笼子是边界，不是命令清单。**

### 3.3 安全边界从"校验模型输入"转移到"容器隔离"

有了真正的沙箱，就能安全地把"自由文本命令"这种强能力还回去（对应笔记里的"示例 A 自由 shell"，但放进安全笼子）。这也是为什么我们能直接复用 `terminal` 工具，而不必把每个业务流程包成结构化受限工具。

---

## 4. 现有基础设施盘点（带行号，便于审核核对）

> 行号基于当前版本，可能漂移，重点核对原理。

Hermes 已内置可插拔执行环境抽象 `tools/environments/`，关键能力**已存在**：

- `DockerEnvironment`（`tools/environments/docker.py:503`），`__init__`（`:515`）支持：
  - 资源上限 `--cpus` / `--memory`（`:561-563`）
  - 断网 `--network=none`（`:572`，由 `network=False` 触发；构造默认 `network=True`，见 `:528`）
  - 工作区 bind-mount 到 `/workspace`（`:575+`），支持 `host_cwd` / `volumes` / `auto_mount_cwd`
  - 非 root 运行、安全参数加固（`_build_security_args`，`:355`）
  - 孤儿容器回收 `reap_orphan_containers`（`:138`）
- `terminal` 工具（`tools/terminal_tool.py`）后端由 `TERMINAL_ENV` 选择（默认 `"local"`=宿主机直跑，`:1093`）：
  - `_get_env_config()`（`:1089`）从环境变量读 `TERMINAL_DOCKER_IMAGE`、`TERMINAL_DOCKER_VOLUMES`、`container_persistent`（默认 True，`:1192`）等。
  - `_create_environment()`（`:1225`）按 `env_type` 实例化后端；docker 分支在 `:1260-1274`，传入 `task_id`（`:1228`）用于**环境复用与快照键控**（`:1241`）。
- **per-task 沙箱注入点（关键）**：`register_task_env_overrides(task_id, overrides)`（`:952`）。注释明说："在 agent loop 启动前，为某个 task_id 配置沙箱镜像；当 terminal/file 工具为该 task_id 创建新沙箱时，会先查这个注册表"。配套 `resolve_task_overrides`（`:1037`）、`clear_task_env_overrides`（`:993`）。
- **环境复用缓存**：`_active_environments.get(task_id)`（`:988`），按 `task_id` 缓存活动环境；`_cleanup_inactive_envs` 回收空闲的持久环境（`is_persistent_env`，`:1481`）。

多租户侧（`gateway/multi_tenant.py`）已有：

- `multi_tenant_enabled()`（`:140`）、`get_current_owner_key()`（`:185`）、`OwnerKeyMissing`（`:221`）
- `set_current_owner_key()`（`:152`）、owner ContextVar
- `owner_workspace_root(owner_key) -> Path`（`:258`）、`hash_owner_key()`（`:104`）
- `assert_session_owner()`（`:283`）

**结论：本方案不是"造沙箱"，是"接线"——把现有 docker 后端按当前 owner 动态注入、强制加固、补 fail-closed。落在足迹阶梯最高一档（扩展现有代码）。**

---

## 5. 架构设计

### 5.1 核心思路

现有 docker 后端是**进程级全局配置**（读 `TERMINAL_ENV` 等环境变量），不按 owner 区分。多租户要的是**按当前 owner 动态决定挂哪个 workspace**。解决办法：在请求入口（已经 `set_current_owner_key` 的地方）**额外调用 `register_task_env_overrides`**，为本次请求的 `task_id` 注入一份 owner 专属的 docker 沙箱配置。

> **【审查修订 #1，P0】现有 override 接不住整套配置，必须先扩展合并逻辑。**
> 实测当前 `terminal_tool`：`env_type` 取自全局 `config["env_type"]`（`terminal_tool.py:1899`，即 `TERMINAL_ENV`）；`register_task_env_overrides` 的 override 在 `:1916-1927` **只覆盖了 `*_image` 和 `cwd`**；创建环境时 `host_cwd=config.get("host_cwd")`（`:2045`）、`env_type=env_type`（`:2037`）**都不读 override**。
> 所以"只在入口注册 override"不够。**前置必做项**：扩展 terminal 的配置合并逻辑，让 owner override 能覆盖 `env_type / docker_image / cwd / host_cwd / network / docker_volumes / docker_extra_args / docker_env / forward_env / container_*` 与资源限制；并满足两条约束——
> 1. **仅当 `multi_tenant_enabled()` 且存在 owner override 时**才应用这套覆盖，单用户/其它平台路径零变化（最小 diff）。
> 2. **fail-closed**：覆盖应用后若 `env_type` 仍不是 `docker`（或 override 缺失关键字段），拒绝执行（见 §6.1）。

### 5.2 数据流

```
企业微信回调
  → 解析出 corp_id/app_id/user_id → build_owner_key → owner_key
  → set_current_owner_key(owner_key)              # 已有
  → (新增) 若 multi_tenant_enabled:
        register_task_env_overrides(task_id, {
            env_type: "docker",                            # R1#1：override 须真正驱动 env_type
            docker_image: <配置的安全镜像>,
            # —— 挂 owner workspace 到 /workspace：host_cwd 单独不够！——
            host_cwd:    str(owner_workspace_root(owner_key)),  # R2#1
            docker_mount_cwd_to_workspace: True,           # R2#1：没这个，host_cwd 不会挂到 /workspace
            cwd:         "/workspace",
            network:     False,                            # 默认禁网（R2连带：构造函数也要透传 network）
            docker_volumes: [],                            # R2#2：键名是 docker_volumes，不是 volumes
            container_persistent: True,                    # 按任务持续（进程内复用）
            docker_persist_across_processes: False,        # R1#4：关跨进程复用
            # R1#2：自动挂载拆成三个独立开关，只关该关的
            mount_credentials: False,                      # 全局凭证，绝不进用户容器
            mount_skills:      True,                       # 公共业务 skills（RO，带可执行脚本）→ 保留
            mount_cache:       False,                      # 全局 cache 含他人上传文件 → 关（或改 owner-scoped）
            # 资源上限：cpu / memory / timeout（见 §8；pids 见 R2#5，当前硬编码 256）
        })
```
> **【审查修订 R2#1，P0】`host_cwd` 单独不够，必须配 `docker_mount_cwd_to_workspace=True`。**
> `DockerEnvironment` 只在 `bind_host_cwd = auto_mount_cwd and host_cwd 有效 and not workspace_explicitly_mounted` 时才把 host_cwd 挂到 `/workspace`（`docker.py:597`），而 `auto_mount_cwd` 来自 `docker_mount_cwd_to_workspace`（`terminal_tool.py:1273`，默认 False）。**只设 `host_cwd` → 退回 `get_sandbox_dir()/docker/<task_id>/workspace`，根本没挂 owner workspace = 隔离失效。** 故 override 必须同时设 `docker_mount_cwd_to_workspace=True`。
> 另：`auto_mount_cwd` 还要求 `os.path.isdir(host_cwd)`——**owner workspace 目录必须在建容器前已存在**（由多租户 workspace provisioning 保证）。
> **连带（P0）**：docker 分支的 `_create_environment` 当前**根本没把 `network=` 传给构造函数**（构造默认 `network=True`，`docker.py:528`），`pids` 也硬编码。所以 R1#1 的"扩展 override 合并"必须连同**给构造函数补 `network`（及需要的 pids）透传**一起做，否则 `network:False` 永远不生效。
```
  → agent loop 运行
  → terminal 工具被调用 → 查 task_env_overrides → 命中 → 用 owner 专属 docker 沙箱执行
  → 请求结束/会话空闲 → 容器按 _cleanup_inactive_envs 回收
  → (新增) clear_task_env_overrides(task_id)             # 收尾，防注册表泄漏
```

### 5.3 task_id 与隔离键（硬不变量）

- 环境复用按 `task_id` 缓存（`_active_environments`）。**task_id 必须按 owner 唯一**，否则两个 owner 命中同一缓存容器 = 跨租户泄漏。
- 多租户下 session 本就按 owner 隔离，`task_id` 应取**会话级、且与 owner 绑定**的标识。spec 要求：
  1. 选定 task_id 来源后，写一条断言/校验：注入 override 时，该 task_id 当前若已绑定到别的 owner，必须拒绝（防串号）。
  2. `owner_workspace_root` 路径来自 `hash_owner_key`（sha256 前 16 位），天然 path-safe，不接受模型输入。

> **【审查修订 #4，P1】跨进程容器复用只比 label，不比 mounts，必须关掉。**
> `DockerEnvironment` 的跨进程复用（`docker.py:823-828` 注释明示）**只按 `hermes-task-id` + `hermes-profile` label 匹配，故意不比较 image / mounts / resources**。多租户下若一个 stale 容器挂着旧的 owner workspace，仅凭 task_id 命中就被复用 = 挂错目录的隔离事故。
> **处置**：多租户 sandbox 模式**默认 `docker_persist_across_processes=false`**（在 owner override 里设死）。进程内的 `container_persistent=True` 仍提供同一任务多步的容器复用与状态保留；只放弃"跨 gateway 重启复用同一容器"这一点收益，换取隔离正确性。

### 5.4 生命周期：文件持久 vs 容器持久（必须分清的两件事）

这是使用体验上最容易混淆、也最关键的一节。**"容器生命周期"和"文件是否丢失"是两件完全独立的事**，分开看才不会踩坑。

#### 5.4.1 两个独立概念

| | 它是什么 | 容器回收后会丢吗 | 由什么控制 |
|---|---|---|---|
| **文件持久** | owner workspace 目录 bind-mount 到宿主机磁盘 | **永远不丢** | 与容器生死无关，只跟宿主机磁盘有关 |
| **容器/进程持久** | 一个临时的执行容器（外壳） | 空闲超时即回收 | 空闲时间（默认 300s） |

#### 5.4.2 原理：bind-mount 的本质

容器里的 `/workspace` 就是宿主机上 `owner_workspace_root(owner)` 那个目录的**同一块磁盘**。容器只是个**临时执行外壳**；外壳销毁，磁盘上的文件**纹丝不动**。

所以容器被回收时，**真正丢的只有"外壳里的临时状态"**：

- `pip install` 装的包、后台进程、环境变量、shell 的 cwd；
- 写在 `/workspace` **外面**的东西（如容器内 `/tmp`）。

**只要中间文件写进 `/workspace`（沙箱默认 cwd 即 `/workspace`，见 §5.2 override），它就落在宿主机磁盘上永久保存。** 代码注释也印证了这点（`tools/terminal_tool.py:22`）："persistent filesystem 不保证同一个 live sandbox 或长进程能熬过 cleanup / idle reaping / Hermes 退出"——即：**文件系统持久，但活的沙箱/进程不保证持久**。

#### 5.4.3 回收判定：纯按空闲时间，不看任务语义

回收逻辑是 `_cleanup_inactive_envs(lifetime_seconds=300)`（`tools/terminal_tool.py:1375`）——**默认 300 秒无活动就回收**。它**只看"多久没动"，不看任务是长是短**。

**因此本方案不需要、也不会去"判定长任务 vs 短任务"。** 生命周期纯粹由空闲时间驱动，文件持久与它解耦。用户的多步长周期任务天然就能工作。回收阈值**沿用现有 300s 默认，本期不改动**（见 §8）。

#### 5.4.4 长周期任务的实际表现（用典型场景说明）

场景：用户发文件 → 处理出中间文件 → 隔一段时间再让其在中间文件上继续处理。

1. 第一次：模型在 `/workspace` 处理，生成 `中间结果.csv`（落宿主机磁盘）→ 用户查看结果。
2. 中间隔了半小时，容器因空闲（>300s）被回收。**`中间结果.csv` 仍在磁盘上。**
3. 第二次：用户说"在中间结果上继续" → 新容器拉起 → 仍挂同一个 owner workspace → `中间结果.csv` 就在 `/workspace` 里（模型 `ls` 即可看到）→ 直接接着干。

- **回收丢什么**：只丢容器内的进程态 / cwd / 环境变量等"外壳态"，**不影响文件**。
- **依赖（审查 R2#4，重要修正）**：**默认 `--network=none`，运行期 pip 连不上 PyPI，不能假设"重装一遍 pip 包"可行**。所以**所有运行所需依赖必须预装进镜像**（pandas/openpyxl/python-docx/libreoffice 等，见 §8/§13）；如确需运行期装包，须提供**内部 wheelhouse / 离线包**或显式开网（见开放问题 #1）。换言之容器回收**也不涉及"重装"**——因为运行期本就不该联网装包。
- **"叙述时要不要再强调中间文件"**：同一会话内对话历史还在，模型记得文件名，不用强调；跨会话模型可 `ls /workspace` 自行找回，文件一定在。

#### 5.4.5 workspace 共享范围

当前设计是"**同一 owner 的所有任务共享同一个 workspace 目录**"（bind-mount `owner_workspace_root(owner)`）。这恰好给最大连续性——中间文件跨任务、跨会话都在。代价是文件会累积，需要时让模型清理即可。

#### 5.4.6 实现要点

复用现有机制即可，无需新增生命周期代码：`container_persistent=True`（override 里设，对应 `terminal_tool.py:1192` 默认 True）让同一任务多步间复用容器、保留外壳状态；`_cleanup_inactive_envs` 负责空闲回收；`reap_orphan_containers` 兜底清理泄漏容器。

---

## 6. 安全设计

### 6.1 fail-closed（最高优先级红线）

当前 `TERMINAL_ENV` 默认 `"local"`=**宿主机直接执行**。多租户下这是灾难性逃逸路径。要求：

- **当 `multi_tenant_enabled()` 且存在 owner 时，terminal 必须在 owner 专属 docker 沙箱里执行。**
- 若 docker 不可用、override 未命中、或解析不到 owner → **拒绝执行并返回可执行错误**，**绝不退化成 local 宿主机执行**。
- 实现层面：在 terminal 执行入口加一道守卫——`multi_tenant_enabled()` 为真时，若最终选定的 `env_type != "docker"`（或没有 owner override），直接返回错误。

### 6.2 加固清单（面向"部分不可信"用户）

owner 容器一律强制：

| 项 | 设定 | 目的 |
|---|---|---|
| 网络 | `--network=none`（默认） | 防数据外传；确需联网再 opt-in（见开放问题） |
| 文件可见性 | 只 bind-mount `owner_workspace_root(owner)` → `/workspace` | 看不到宿主机、看不到别的 owner |
| operator volumes | 丢弃 `TERMINAL_DOCKER_VOLUMES` | 防把宿主机其他路径挂进来 |
| **自动挂载（审查 #2，P0）** | **拆成三开关**：credential **关**、cache **关/owner-scoped**、skills **保留 RO**（`docker.py:639+`） | 见下方专条——只有 credential/全局 cache 破隔离；公共 skills 是需求本身 |
| 用户 | 非 root | 降低容器内提权影响 |
| 资源 | `--cpus` / `--memory` / `--pids-limit` / 执行超时 封顶 | 防单用户打满宿主机 |
| 并发 | 全局信号量限制同时活动容器数 | 防多用户并发把宿主机压垮 |
| 临时性 | 任务结束/空闲回收，`reap_orphan_containers` 兜底 | 防容器泄漏堆积 |
| **后台进程（审查 #5，P1）** | **多租户下禁用 `terminal` 的 `background=True` / `notify_on_complete` / `watch_patterns`** | 见下方专条——一期前台足够，后台进程需完整 owner 校验 |

> **【审查修订 #2，P0】Docker 全局自动挂载——拆成三个独立开关，分别处置。**
> `DockerEnvironment` 默认把三类**全局**资源挂进容器（`docker.py:639` 起，`credential_files.py` 提供路径）。单用户合理，多租户下分别处理：
> - **credential files**（`get_credential_file_mounts`）：operator/全局凭证 → **绝不进任何用户容器**，必关。
> - **全局 cache**（`get_cache_directory_mounts`，含所有用户上传的 documents/images/audio/screenshots）：直接挂 = 用户 A 读到用户 B 的上传 = 跨租户泄漏 → **必关**；若需"沙箱读本用户上传文件"，改成 **owner-scoped 挂载**（只挂该 owner 的 upload 子目录），见 §13 开放问题 #6。
> - **skills 目录**（`get_skills_directory_mount`，挂 `<hermes_home>/skills` RO，含 `scripts/`/`templates/`）：本项目 skills 为**中央开发、公共共享、且带需在沙箱内执行的脚本** → **保留 RO 挂载**，它正是需求本身。RO 保证用户不可篡改；代码已自带软链接消毒。
>   - **配套纪律**：公共 skills 被 RO 挂进每个用户容器、人人可读 → **skill 脚本文件内绝不可硬编码密钥/token**，需密钥走环境注入。
> **处置**：把原计划的单一 `mount_host_extras` 拆成 `mount_credentials` / `mount_skills` / `mount_cache` 三个开关；多租户 sandbox 传 `mount_credentials=False, mount_cache=False, mount_skills=True`。owner workspace 仍按 §6.2 正常挂。

> **【审查修订 #5，P1】不开 process 工具 ≠ 没有后台进程。**
> `terminal_tool()`（`:1843`）签名自带 `background` / `notify_on_complete` / `watch_patterns`；`background=True` 时（`:2147`）直接把进程登记进 `process_registry`（`spawn_local` / `spawn_via_env`）。即使工具集里不含 `process` 工具，模型仍能用 terminal 的参数起后台进程，且 notify/poll 路径未纳入 owner 校验。
> **处置（一期）**：多租户 sandbox 模式下，terminal 收到 `background=True`（或 notify/watch）时**拒绝或忽略并降级为前台**，并返回可执行提示。前台 + 高 `timeout`（命令完成即刻返回，见 `:847`）足以覆盖数据处理类任务。后台进程能力连同 `process` 工具、process_registry / notify / poll 的完整 owner 校验，留到二期一起做。

### 6.3 隔离不变量（审核 checklist）

- [ ] task_id 按 owner 唯一，跨 owner 不复用容器。
- [ ] bind-mount 路径完全由 `owner_workspace_root(ContextVar owner)` 派生，模型/用户无法影响。
- [ ] 多租户开启时，terminal 不存在任何"退化到 local"的代码路径。
- [ ] 单用户模式（默认 `enabled=false`）下，上述逻辑全部不生效，行为零变化。
- [ ] （#1）owner override 确实驱动了 `env_type/host_cwd/network/volumes/资源`，不是只换了 image/cwd。
- [ ] （#2）credential 与全局 cache 自动挂载已禁用；skills 保留 RO 挂载；如挂上传文件则为 owner-scoped 子目录。
- [ ] （#4）跨进程容器复用已关（`docker_persist_across_processes=false`）。
- [ ] （#5）多租户下 terminal 的 `background=True` 已被拒绝/降级，无未校验的 process_registry/notify 路径。

---

## 7. 问题 2：能力清单 + 诚实锚点

**根因**：system prompt 里只泛泛说"你的工具"，模型用训练先验脑补"我能跑代码/上网/调各种 API"。

**修法**：在多租户 system prompt 里加一段**静态能力清单**（cache 友好，属稳定前缀，不破 prompt 缓存），要点：

- 明确列出当前确切启用的工具集。
- 显式声明边界，例如：
  - "你的 shell 运行在一个隔离沙箱里，只挂载了当前用户的工作区；你无法访问宿主机或其他用户的数据。"
  - "沙箱默认无网络。"
  - "你的工具仅限 schema 中列出的那些；schema 里没有的能力你就是没有，不要声称或暗示你拥有它。如不确定，先检查你的工具，不要假设。"

**约束**：这段文本必须是**会话内静态**的（随 toolset 固定），不得在会话中途变化，避免破坏 prompt 缓存（铁律 1）。落点为多租户分支的 system prompt 组装处（`agent/prompt_builder.py` / `agent/system_prompt.py`，具体注入点在实现阶段定位）。

---

## 8. 配置项（建议，最终命名实现时定）

挂在现有 `security.multi_tenant` 配置块下，新增子块（草案）：

```yaml
security:
  multi_tenant:
    enabled: false            # 总开关（不变）
    sandbox:
      enabled: true           # 多租户下是否启用 owner 沙箱执行（关掉则回到纯受限工具集）
      image: "<安全镜像名>"    # 预装 python/pandas/openpyxl/python-docx/libreoffice 等（必须，因默认禁网）
      network: false          # 默认禁网
      cpus: "1"
      memory_mb: 1024
      exec_timeout_s: 120
      max_concurrent: 4       # 全局并发容器上限（信号量）
      # pids 上限：当前 DockerEnvironment 硬编码 --pids-limit 256（docker.py:333），
      # 本期不暴露为配置（审查 R2#5）。256 已是合理加固默认；如需可配，留作后续改 DockerEnvironment。
      # 空闲回收阈值沿用现有默认 300s（_cleanup_inactive_envs(lifetime_seconds=300)），
      # 本期不新增配置、不改动。文件持久与该阈值无关（见 §5.4）。
```

- 镜像构建：仓库内提供 Dockerfile（源码部署到服务器时构建一次），**预装全部运行期依赖**（因默认禁网，运行期无法 pip install，见审查 R2#4）。
- 不复用 `TERMINAL_DOCKER_*` 全局环境变量做 owner 配置——owner 配置只走 `register_task_env_overrides`，与全局解耦。

---

## 9. 改动清单（文件级，最小 diff）

> 实现阶段细化为可执行步骤；此处给范围，便于审核判断 diff 大小。

1. **`toolsets.py`（改自审查 #3）**：**新增** `wecom_multi_tenant_sandbox` 工具集 = `wecom_multi_tenant` 全部受限工具 + `terminal`。**`wecom_multi_tenant` 保持纯受限不变**（`process` 两者都不加）。
2. **`gateway/multi_tenant.py` 的 `constrain_toolsets_for_owner()`（审查 #3）**：按 `security.multi_tenant.sandbox.enabled` 在两个 toolset 间二选一返回——开则 `wecom_multi_tenant_sandbox`，关则 `wecom_multi_tenant`。这样"关 sandbox = 回滚到纯受限"语义自洽，且收敛点唯一。
3. **`tools/terminal_tool.py` — 扩展 override 合并（审查 R1#1 + R2#1 连带，P0 前置）**：让 owner override 能覆盖 `env_type/host_cwd/docker_mount_cwd_to_workspace/network/docker_volumes/资源/container_*/docker_persist_across_processes/mount_credentials/mount_skills/mount_cache` 等；仅在多租户+有 owner override 时生效，单用户路径零变化。**特别注意两个当前接不住的字段**：(a) `docker_mount_cwd_to_workspace` 必须设 True，否则 host_cwd 不挂 `/workspace`（R2#1）；(b) docker 分支的 `_create_environment` 当前**没把 `network=` 传给 `_DockerEnvironment` 构造函数**（`terminal_tool.py:1268-1280`），需补上 `network=`（及如需可配的 pids）透传，否则 `network:False` 不生效。
4. **`tools/terminal_tool.py` — fail-closed 守卫**：多租户+有 owner 时，最终 `env_type` 必须为 docker、且关键 override 齐备，否则拒绝；**绝不退化 local**。
5. **`tools/terminal_tool.py` — 禁后台进程（审查 #5）**：多租户下 `background=True`/`notify_on_complete`/`watch_patterns` 拒绝或降级为前台。
6. **`tools/environments/docker.py`（审查 #2，P0）**：把自动挂载拆成 `mount_credentials` / `mount_skills` / `mount_cache` 三开关（`docker.py:639+`）；多租户传 `credentials=False, cache=False, skills=True`（公共业务 skills 带可执行脚本，RO 保留）。
7. **owner 沙箱配置组装**：新增小函数（建议 `gateway/multi_tenant.py` 或新文件），把 owner_key → docker override dict，集中校验 + 加固（含 `docker_persist_across_processes=False`、`mount_credentials=False`、`mount_cache=False`、`mount_skills=True`），避免散落。
8. **请求入口**（企业微信回调路径，`set_current_owner_key` 附近）：多租户时调 `register_task_env_overrides(task_id, owner 沙箱配置)`；收尾 `clear_task_env_overrides`。
9. **配置读取**：`security.multi_tenant.sandbox.*`（best-effort，缺省值兜底）。
10. **system prompt**：多租户 sandbox 分支加能力清单段落（§7）。
11. **镜像**：新增 `Dockerfile`（或复用现有沙箱镜像约定），预装数据处理库。
12. **并发信号量**：terminal 创建 docker 环境处加全局信号量（或复用现有限流，如有）。

---

## 10. 测试与验证计划

按项目约定，验证以**定向 pytest** 为主（ruff/mypy/全量暂略），不把定向通过写成整体验收。

- 隔离：两个 owner 各自在沙箱内写文件 / 读文件，断言互相看不到（对应已有文件隔离测试，扩展到 terminal 路径）。
- fail-closed：多租户开启但人为制造 owner 缺失 / docker 不可用 → terminal 返回拒绝，**不**在宿主机执行（断言没有宿主机副作用）。
- 路径钉死（审查 R2#3，口径修正）：**注意容器内 `/etc/passwd` 是容器自己的系统文件，读到不代表读了宿主机**，不能用它当判据。正确断言：
  - 读不到**宿主机**敏感路径（容器里看不到宿主机文件系统）；
  - 读不到**其它 owner 的 workspace**；
  - `/workspace` 内容只对应**当前 owner**；
  - `docker run` 参数里**没有**挂载宿主机根目录 / 全局 cache / credential 文件。
- 禁网：容器内访问外网 → 失败（除非显式开 network）。
- task_id 串号防护：构造两个 owner 命中同一 task_id 的场景 → 断言拒绝，不复用容器。
- 单用户回归：`enabled=false` 时 terminal 行为与改造前一致（docker/local 由原配置决定）。
- 端到端冒烟：发 Excel → 让智能体在沙箱内用 pandas 生成 Word 报告（手动验证步骤）。
- 问题 2：检查多租户 system prompt 含能力清单段；（可选）行为测试模型不再声称未拥有的工具。
- （审查 #1）override 生效：注册 owner override 后断言实际 `env_type=docker`、`host_cwd` 指向 owner workspace、`network=none`，而非全局 `TERMINAL_ENV` 的值。
- （审查 #2）自动挂载：多租户容器的 `docker run` 参数中**不含** credential、**不含全局 cache** 的 `-v`；**含 skills 的 RO `-v`**（验证公共 skills 可用且 skill 脚本可在沙箱内执行）。
- （审查 #3）toolset 切换：`sandbox.enabled=true` 时 owner 拿到 `wecom_multi_tenant_sandbox`（含 terminal）；`false` 时拿到纯 `wecom_multi_tenant`（无 terminal）。
- （审查 #4）跨进程不复用：override 里 `docker_persist_across_processes=false` 生效，断言不会 attach 到既存 label 容器。
- （审查 #5）后台进程拒绝：多租户下 `terminal(background=True)` 返回拒绝/降级前台，且 process_registry 未新增条目。

---

## 11. 分期

- **一期（本 spec）**：只开 `terminal`，跑通 owner 沙箱 + fail-closed + 加固 + 能力清单。terminal 对数据处理类任务已够用（模型写 python 脚本用 pandas/python-docx，再 `python3` 跑）。
- **二期（后续单独 spec）**：评估 `code_execution`（PTC）与 `process`（后台进程）。重点是 code_execution 的"脚本回调 Hermes 工具"RPC 通道**必须同样按 owner 校验**（否则脚本借回调越权），surface 更大，想清楚再上。

---

## 12. 风险与回滚

- **风险**：
  - docker 启动延迟带来首条命令变慢（按任务持续可摊薄）。
  - 镜像维护成本（需预装数据处理库）。
  - 并发上限设置不当 → 要么排队久、要么压垮宿主机；需压测调参。
  - fail-closed 若漏一条退化路径 = 逃逸——§6.3 checklist 必须逐条过。
- **回滚**：
  - 配置级：`security.multi_tenant.sandbox.enabled=false` → 立即回到纯受限工具集（不重开 terminal）。
  - 代码级：所有改动集中在上述文件，`git diff` 可见，可整体 revert。
  - 单用户路径不受影响，回滚风险局限在多租户分支。

---

## 13. 开放问题（审核时请定）

1. **联网**：默认禁网。是否需要"按 owner / 按任务 opt-in 联网"的口子？还是一期完全禁网？
2. **task_id 来源**：用现有 session id？还是 `hash_owner_key` 派生的稳定键？需确认它在企业微信请求路径里能稳定取到且按 owner 唯一。
3. **镜像**：自建 Dockerfile（仓库内）还是复用某个已有沙箱镜像约定？预装哪些库（pandas/openpyxl/python-docx/libreoffice…）？
4. **资源默认值**：§8 的 cpu/memory/并发默认值是否符合你服务器规格？
5. **`process` 是否一期就要**：若用户任务常有长耗时后台作业，可能需要；当前按"一期不开"。
6. **（审查 #2 衍生）沙箱要不要读到本用户上传的文件**：企业微信发来的文件落在全局 cache，沙箱默认看不到。一期是否需要做 **owner-scoped upload 子目录挂载**让沙箱能直接读用户刚发的文件？还是先让上层把文件拷进 owner workspace 再进沙箱？
7. **（审查 #2，已定）skills 进沙箱**：已确认 skills 中央开发、公共共享、带可执行脚本 → **保留 RO 全局挂载**。配套纪律：skill 脚本内不得硬编码密钥。（若未来引入用户私有 skill，需改"公共 RO + 私有 owner-scoped"，届时另议。）

---

## 14. 代码审查处置记录

### 14.1 第一轮（v1 → v2）

第一轮提了 5 条，全部对照当前代码核实**成立**，已纳入本 spec。

| # | 级别 | 问题 | 代码证据 | 处置（落点） |
|---|---|---|---|---|
| R1#1 | P0 | 仅注册 override 接不住整套沙箱配置（env_type/host_cwd/network/volumes/资源不走 override） | `terminal_tool.py:1899/1916-1927/2037/2045` | 扩展 override 合并逻辑，gated + fail-closed（§5.1、§9.3-9.4） |
| R1#2 | P0 | Docker 自动挂载 credential/skills/全局 cache | `docker.py:639+` | 拆三开关：credential/cache 关、**skills 保留 RO**（公共业务 skills 带脚本）；cache 若用须 owner-scoped（§6.2、§9.6） |
| R1#3 | P0 | `sandbox.enabled=false` 回滚语义与"toolset 直接加 terminal"冲突 | `multi_tenant.py:227` `constrain_toolsets_for_owner` | 改双 toolset，由 constrain 按配置二选一（§9.1-9.2） |
| R1#4 | P1 | 跨进程容器复用只比 label、不比 mounts | `docker.py:823-828` | 多租户默认 `docker_persist_across_processes=false`（§5.3 修订） |
| R1#5 | P1 | 不开 process 工具 ≠ 没后台进程（terminal 自带 background） | `terminal_tool.py:1843/2147` | 一期多租户下禁 `background=True`，二期再做完整 owner 校验（§6.2、§9.5） |

### 14.2 第二轮（v2 → v3）

第二轮提了 5 条 + 1 连带，全部核实**成立**，已纳入。多数是 R1#1"override 接不住"的**具体字段实证**，把抽象修复落到了可执行细节。

| # | 级别 | 问题 | 代码证据 | 处置（落点） |
|---|---|---|---|---|
| R2#1 | P0 | 只设 `host_cwd` 不挂 owner workspace，须配 `docker_mount_cwd_to_workspace=True` | `docker.py:597`、`terminal_tool.py:1273` | override 补该字段 + 要求 workspace 目录预先存在（§5.2 修订块、§9.3） |
| R2连带 | P0 | docker 分支 `_create_environment` 没透传 `network=`，`network:False` 不生效 | `terminal_tool.py:1268-1280`、`docker.py:528` | R1#1 的 override 合并须连带补构造函数 `network`（及 pids）透传（§5.2、§9.3） |
| R2#2 | P1 | override 示例键名 `volumes` 错误，实际是 `docker_volumes` | `terminal_tool.py:1252` | 全文统一 `docker_volumes`（§5.2） |
| R2#3 | P1 | `cat /etc/passwd` 测试口径不准（容器自有该文件，非宿主机） | — | 改成"读不到宿主机/他人 workspace + docker 参数无越权挂载"（§10） |
| R2#4 | P1 | 默认禁网下 pip 连不上 PyPI，"重装 pip 包"表述错误 | — | 改为"依赖必须预装进镜像 / 离线 wheelhouse"（§5.4.4、§8、§13） |
| R2#5 | P2 | `pids_limit` 配置无接线（硬编码 256） | `docker.py:333` | 本期从配置删去，沿用硬编码 256（§8） |

> 两轮合计：方案从 v1 的"接线即可"修正为"接线 + 前置改造（override 合并含 `docker_mount_cwd_to_workspace`/`network` 透传、docker 自动挂载三开关、双 toolset、后台进程闸门）"，并明确**镜像必须预装全部运行期依赖**（因默认禁网）。这些都是隔离正确性/可运行性的必要项。
