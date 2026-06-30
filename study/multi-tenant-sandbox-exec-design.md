# 多租户企业微信 · 沙箱执行能力恢复方案（设计 spec）

> 状态：待审核（草案 v4，含两轮代码审查 + 一轮设计细化，见 §14）
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
- **已定（容器粒度 = 每 owner 一个）**：`task_id = hash_owner_key(owner)`。同一 owner 的所有 session 共用一个容器、共享同一 owner workspace；跨 owner 不同 hash → 不同容器，隔离不变。
  - 为什么不是 per-session：安全/文件连续性/依赖三方面 per-session 与 per-owner **完全等价**（都 owner 级隔离、共享 workspace、同镜像）；per-owner 更省（同 owner 切 session 复用热容器、免冷启动），且更贴合现有 `_resolve_container_task_id` 把"每会话表面"塌缩为共享容器的取向。
  - **容器内路径**：bind-mount 是 `owner_workspace_root(owner)` → `/workspace`；hash 只在宿主机路径里，容器内统一是 `/workspace`（不出现 hash）。隔离靠"该容器只挂了这个 owner 的目录"，不靠容器内路径名。
  - **已知小代价（非安全问题）**：同一 owner 若**并发**在多 session 发命令，会进同一容器，可能在 shell 状态（cwd/env）上互串。同人同数据、不涉越权，最坏是工作目录混淆。可选兜底：加**每 owner 串行锁**让同 owner 命令排队（见开放项；默认不加，按需开启）。
- spec 要求：
  1. 注入 override 时写一条断言：该 task_id（owner hash）当前若已绑定到别的 owner_key，必须拒绝（防串号；正常不会发生，做防御）。
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

### 5.5 沙箱触发条件 + 路径词汇统一

#### 5.5.1 触发条件（分层，惰性）

沙箱不是"会话一开就建"，分层触发：

1. **配置门槛**：`multi_tenant.enabled=true` 且 `sandbox.enabled=true`。
2. **owner_key**：是**整个多租户请求的上游 fail-closed 前提，不是沙箱的触发开关**。多租户开着却拿不到 owner_key → 直接拒绝整个请求（连受限工具都不给）。所以它是"门外硬闸"。
3. **配置注册（每请求入口，很便宜）**：`register_task_env_overrides(task_id, owner 沙箱配置)` 只写注册表，**还没有容器**。
4. **容器惰性创建**：只在**第一个"需要在 workspace 环境里执行"的工具被实际调用**时才 spin up——即 `terminal`。纯对话、或只调 `web_search`/`memory`/`todo`/`session_search`/`clarify` 等不碰执行环境的工具 → **永远不建容器，零成本**。之后同 owner（task_id）复用热容器，空闲 300s 回收。

> 触发判据是"调了执行/workspace 类工具"，**不是"工具列表非空"**——非执行类工具不该、也不会触发容器。

#### 5.5.2 哪些工具进容器（A 方案，已定）

- **`terminal`** → 进 owner 容器执行。
- **file 工具（`read_file`/`write_file`/`patch`/`search_files`）** → **留在 gateway 进程内**跑，直接读 bind-mount 的宿主机 owner workspace，**不进容器**。
  - 理由：纯读/小改**零容器开销**；`patch`（模糊匹配）/`search_files`（结构化搜索）比 bash 一行流**更可靠**（结构化 > 自由文本）。
  - 实现：需解开现有"`env_type=docker` 时 file 工具自动进容器"的耦合（`file_tools.py:478-503` / `:792-896`），让多租户 file 工具走进程内路径。
- **一致性保证**：容器内 `/workspace` 与宿主机 `owner_workspace_root` 是 bind-mount 的**同一块磁盘**，所以 terminal 在容器里写的文件，进程内 file 工具立刻能读（反之亦然），不会分裂。

#### 5.5.3 路径词汇统一（A 方案成立的关键）

**问题**：file 工具（进程内，宿主机路径词汇）与 bash（容器内，`/workspace` 词汇）路径词汇不一致——模型若混用，`read_file("/workspace/out.csv")` 会被当宿主机绝对路径校验、判定越界被拒。

**解法（不靠提示词，靠路径别名）**：让 file 工具把 **`/workspace` 当作 `owner_workspace_root` 的别名**，模型在两个世界**统一只用 `/workspace/...`（或相对路径）**。翻译规则：**去掉 `/workspace` 前缀，拼到 `owner_workspace_root` 后面**（`<hash>` 已在 `owner_workspace_root` 内，不在 `/workspace` 后补 hash）。

**写法：确定性归一化 → 单点硬校验（fail-closed），不要 try-fail-guess。**

```python
def _normalize_owner_path(filepath: str, owner_root: Path) -> Path:
    p = filepath
    # 1) /workspace 是约定别名 → 最先显式处理（不是校验失败后的兜底猜测）
    if p == "/workspace" or p.startswith("/workspace/"):
        rel = p[len("/workspace"):].lstrip("/")
        target = owner_root / rel
    elif Path(p).is_absolute():
        target = Path(p)            # 已是宿主机绝对路径
    else:
        target = owner_root / p     # 相对路径 → owner_root（现有 file_tools.py:273 逻辑）
    return target.resolve()

# 2) 归一化之后，永远跑这一道硬校验（唯一安全闸）
target = _normalize_owner_path(filepath, owner_root)
if validate_within_dir(target, owner_root):   # 越界 → 拒绝
    reject()
```

三条原则：
1. **归一化在前、校验在后、校验只一处**；不要"校验→失败→改写→再校验"。
2. **`/workspace` 当头等约定别名最先处理**，不是猜测。
3. **最后的 `validate_within_dir` 兜住一切**：`/workspace/../../etc/passwd` → 去前缀→拼 owner_root→resolve→越界→拒绝。别名不削弱安全。

改动很小：现有 `_resolve_path_for_task`（`file_tools.py:259-276`）已做"相对→owner_root、绝对→校验"，只在最前面**加一个 `/workspace` 别名分支**即可。

> 这一并解决了"上传文件路径翻译"：上传文件在宿主机 `owner_workspace_root/uploads/...`，模型统一看到/使用 `/workspace/uploads/...`，file 工具按别名归一化、bash 原生可见。

#### 5.5.4 同一回复内 terminal + file 的顺序保证（依赖现有机制，本项无需改动）

担心点：模型在**同一次回复**里同时发 `terminal(跑脚本)` 和 `file 工具(看结果)`，会不会出现"容器在跑脚本、外部 file 工具同时在读"的竞态？

**不会**——Hermes 调度层已挡住，三重保证：

1. **含 terminal 的批次强制串行**：`_should_parallelize_tool_batch`（`agent/tool_dispatch_helpers.py`）里 `terminal` 不在 `_PARALLEL_SAFE_TOOLS`、不在 `_PATH_SCOPED_TOOLS` → 整批判为不可并行 → 走 `_execute_tool_calls_sequential`，按模型发出顺序逐条执行。
2. **前台阻塞 + 禁后台**：terminal 前台执行阻塞到命令真正完成（`terminal_tool.py:847`），且多租户已禁 `background=True`（R1#5）→ 脚本跑完才轮到下一个工具 → "先跑完后读"成立。
3. **同文件读写不并发**：`read_file`/`write_file`/`patch` 为路径感知工具，目标路径重叠时 `_paths_overlap` 判定 → 串行（`tool_dispatch_helpers.py`）。

**A 方案不破坏它**：串行判定在**调度层**（决定执行顺序），位于"进程内 vs 容器"之上。terminal 的容器 exec 跑完 → file 工具才在进程内读 bind-mount 的 workspace（已写完）→ 一致。

**残留（非问题）**：若模型把顺序写反（先 read 后 terminal），read 读到"文件不存在"→ 返回 not found → 模型下一轮自愈重试（L4 错误回喂）。是模型顺序错误，非竞态，无损坏、无安全问题。

> **本项无需任何代码改动**，仅在 spec 里强调方案依赖这套现有保证；写文档时点明，避免实现期误以为要自己加锁。

---

## 6. 安全设计

### 6.1 fail-closed（最高优先级红线）

当前 `TERMINAL_ENV` 默认 `"local"`=**宿主机直接执行**。多租户下这是灾难性逃逸路径。要求：

- **当 `multi_tenant_enabled()` 且存在 owner 时，terminal 必须在 owner 专属 docker 沙箱里执行。**
- 若 docker 不可用、override 未命中、或解析不到 owner → **拒绝执行并返回可执行错误**，**绝不退化成 local 宿主机执行**。
- 实现层面：在 terminal 执行入口加一道守卫——`multi_tenant_enabled()` 为真时，若最终选定的 `env_type != "docker"`（或没有 owner override），直接返回错误。
- **file 工具的 fail-closed（A 方案下）**：file 工具走进程内、不进容器，其 fail-closed 是另一种形态——多租户下所有路径必须经 §5.5.3 归一化后 `validate_within_dir(owner_root)` 通过，越界即拒绝；且解开"自动进 docker"耦合后，要确保 file 工具不会落到"无 owner 约束的全局路径"。即：terminal 是"非 docker 即拒"，file 工具是"非 owner 根内即拒"。两条执行路径都不留缺口。

### 6.2 加固清单（面向"部分不可信"用户）

owner 容器一律强制：

| 项 | 设定 | 目的 |
|---|---|---|
| 网络 | `--network=none`（一期已定，完全禁网） | 防数据外传；web 检索走 web_search 工具（经 gateway） |
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
> - **全局 cache**（`get_cache_directory_mounts`，含所有用户上传的 documents/images/audio/screenshots）：直接挂 = 用户 A 读到用户 B 的上传 = 跨租户泄漏 → **必关**。注意：多租户上传文件**不走全局 cache**，已 owner-scoped 写入 `owner_workspace_root/uploads/`（`adapter.py:_cache_owner_upload`），随 workspace 挂载天然进沙箱（见 §13 已决 #5），所以关掉全局 cache **不影响**用户读自己上传的文件。
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
- [ ] （A 方案）file 工具走进程内、未被自动路由进 docker；terminal 与 file 工具在同一 `/workspace`（bind-mount）上互通。
- [ ] （路径别名）file 工具对 `/workspace/...` 归一化到 `owner_root` 后做单点 `validate_within_dir`；`/workspace/../...` 越界被拒。

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
      image: "<安全镜像名>"    # 必须预装全部运行期依赖（默认禁网，运行期不能装）：
                              #   数据分析: pandas numpy openpyxl
                              #   文档生成: python-docx python-pptx reportlab
                              #   转换/可视化: libreoffice matplotlib
                              #   PDF/图像: pdfplumber PyPDF2 Pillow
      network: false          # 默认禁网（一期已定，完全禁网）
      cpus: "2"               # 单容器 CPU 上限（上限非预留）
      memory_mb: 4096         # 单容器内存上限 4G（libreoffice/pandas 大表会吃内存）
      exec_timeout_s: 120
      max_concurrent: 24      # 全局"同时执行"容器数（信号量）。峰值≈24×4G=96G/48核，
                              # 占 128c/192G 服务器约一半，留足 OS+gateway+page cache。
                              # 压测后可上调到 32（峰值 128G/64核，仍在预算内）。
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
8b. **上传文件路径呈现（已决 #5，并入 §5.5.3 别名）**：入站文件已落 `owner_workspace_root/uploads/...`（`adapter.py:_cache_owner_upload`）。交给模型时统一呈现为 `/workspace/uploads/...`（由 §5.5.3 的 `/workspace` 别名负责翻译），bash 原生可见、file 工具按别名归一化可见。定位呈现点（adapter 媒体路径回传处 / 注入 prompt 时）。
8c. **file 工具 A 方案改造（§5.5.2 / §5.5.3）**：(a) 解开"`env_type=docker` 时 file 工具自动进容器"的耦合（`file_tools.py:478-503` / `:792-896`），多租户下 file 工具走进程内；(b) 在 `_resolve_path_for_task`（`file_tools.py:259-276`）最前面加 `/workspace` 别名归一化分支，归一化后单点 `validate_within_dir`（fail-closed）。
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
- （A 方案）file 工具不进容器：多租户下调 read_file 不创建/不进入 docker 容器（断言 `_active_environments` 未因 read_file 新增容器）；纯读任务零容器。
- （路径别名互通）terminal 在容器内写 `/workspace/out.csv`，随后 `read_file("/workspace/out.csv")` 能读到同一文件；`write_file("/workspace/x")` 后 bash `cat /workspace/x` 一致。
- （路径别名安全）`read_file("/workspace/../../etc/passwd")` 归一化后越界被拒。

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

## 13. 开放问题与已决事项

### 已决（拍板完成）

1. **联网**：✅ **一期完全禁网**（`--network=none`）。web 检索走工具集里的 `web_search`/`web_extract`（经 gateway，不进沙箱）。
2. **镜像预装库**：✅ 四类全装——数据分析基础（pandas/numpy/openpyxl）、文档生成（python-docx/python-pptx/reportlab）、文档转换+可视化（libreoffice/matplotlib）、PDF+图像（pdfplumber/PyPDF2/Pillow）。因禁网，运行期不能临时装，必须全预装进镜像（见 §8）。
3. **process / 后台进程**：✅ 一期不开（见 §11、§6.2 R1#5）。
4. **skills 进沙箱**：✅ 中央开发、公共共享、带可执行脚本 → **保留 RO 全局挂载**。配套纪律：skill 脚本内不得硬编码密钥。（若未来引入用户私有 skill，需改"公共 RO + 私有 owner-scoped"，届时另议。）
5. **上传文件进沙箱**：✅ **已实现，无需额外挂载/拷贝**。多租户下企业微信入站文件已写入 `owner_workspace_root(owner)/uploads/<upload_id>/<file>`（`adapter.py` 的 `_cache_owner_upload`，含 `validate_within_dir` 防越界）；沙箱挂的就是 owner workspace，故 `/workspace/uploads/...` 天然可见。
   - **唯一待处理细节（路径翻译）**：`_cache_owner_upload` 返回给模型的是**宿主机绝对路径**（`/data/workspaces/<hash>/uploads/...`），但容器内同一文件在 `/workspace/uploads/...`。**实施时必须把交给模型的路径翻译成容器内路径**（或 workspace 相对路径），否则模型在沙箱里按宿主机路径打不开文件。列为一期实现项。
6. **task_id 来源 / 容器粒度**：✅ **每 owner 一个容器**，`task_id = hash_owner_key(owner)`，同 owner 多 session 共用、共享 workspace（详见 §5.3）。
7. **资源默认值**：✅ 部署于 128c/192G、~100 在线。单容器 cpu=2 / memory=4096MB，全局 `max_concurrent=24`（峰值约占一半，留足余量），空闲回收 300s（详见 §8）。压测后可上调并发到 32。

8. **每 owner 串行锁**：✅ **不加**。主要依据：用户为业务同事，几乎不会"开新窗口并发任务再 resume 回来"，同 owner 并发多 session 属极低概率。即便偶发也只影响 shell 状态、非安全。如未来实测出现并发问题再加。
9. **file 工具走哪（A vs B）**：✅ **A 方案**——file 工具留进程内、只有 terminal 进容器；靠 `/workspace` 路径别名让两者在同一块磁盘上互通（详见 §5.5）。
10. **业务能力做成 skill 还是 file 工具**：✅ **业务流程一律做成 skill**（skill 文档 + `scripts/` 脚本，经 bash 在沙箱内跑）；file 这类受限工具**只保留通用原语**（读/写/改/搜），不为单个业务流程新增 model-tool（守窄腰；skill 可无限加而零工具面成本）。

### 相关但在本 spec 之外（需单独跟踪）

11. **workspace 保留/清理策略**：删 session 只清 DB 会话历史，**不清 owner workspace 磁盘文件**（`uploads/` 每个上传文件 + 中间产物随用持续增长，且 workspace 与 session 解耦——这正是"长期项目文件放 workspace、新 session 直接读"能成立的原因）。若在意长期存储膨胀，需为 workspace 设独立保留策略（如 uploads 超期清理 / 培训用户自清）。**本 spec 不实现，仅记录。**
12. **new/resume 会话命令调整（未来设想，当前版本不做）**：曾讨论过"砍掉 resume、只留 new + 做完即删旧 session"来控制会话历史增长、长期任务靠 workspace 项目文件延续。**这只是未来设想，需考察业务部门实际需求后再决定，当前版本不改动现有 new/resume 行为。** 本沙箱 spec 不依赖该调整——无论是否保留 resume，沙箱设计都成立（per-owner 容器 + owner workspace 与 session 解耦的事实不变）。
13. **分段并行调度（未来优化，本 spec 之外，需单独立项）**：当前"批次含 terminal → 整批串行"是保守安全选择。曾设想按串行工具为屏障把批次切成"并行段—串行点—并行段"逐段执行，提升含 terminal 批次的并行度。评估结论：**不在本 spec 做**——这是改核心调度器（全平台爆炸半径，违背窄腰铁律单独立项门槛）、对企业微信工作负载收益边际、且给安全关键路径加复杂度；YAGNI，待实测确认批次串行是真实瓶颈再单独立项 + 全平台回归。仅记录设想。

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

### 14.3 第三轮（设计细化，v3 → v4）

与用户讨论后确定的设计细化（非代码 bug，而是把执行模型讲透并定型）：

| 主题 | 结论 | 落点 |
|---|---|---|
| 沙箱触发条件 | 分层惰性：配置门槛 + owner_key 上游 fail-closed 前提 + 实际调 terminal 才惰性建容器；非执行类工具不触发 | §5.5.1 |
| file 工具走哪（A/B） | **A 方案**：file 工具留进程内、只 terminal 进容器（纯读零容器、patch/search 更可靠） | §5.5.2、§9.13 |
| 路径词汇统一 | `/workspace` 作为 `owner_workspace_root` 别名；确定性归一化 → 单点 `validate_within_dir`（不 try-fail-guess）；统一替代上传文件路径翻译 | §5.5.3、§9.8b/9.13 |
| file 工具 fail-closed | terminal "非 docker 即拒"；file 工具 "非 owner 根内即拒"，两路径无缺口 | §6.1 |
| 业务能力形态 | 业务流程做 **skill**（+脚本，bash 在沙箱跑）；file 工具只留通用原语，不为业务流程加 model-tool（守窄腰） | §13 已决 #10 |
