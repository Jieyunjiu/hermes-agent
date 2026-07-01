# 多租户原生数据处理能力沙箱化方案（设计 V1）

> 状态：**设计 V1**——主线已获架构复审认可（把原生 Docker 后端按 owner/session 复制成多租户执行边界）；实施细节待拆成计划。**非"定稿"**。
> 取代：`study/already/multi-tenant-sandbox-exec-design.md`（受限工具 + Option A 的旧方案）
> 依赖但不再主要参考：`multi-tenant-wecom-rebuild-plan.md`（九必改——owner 隔离地基仍复用，但其"靠受限工具保能力"的核心逻辑已被本方案推翻）
> 日期：2026-07-01

---

## 1. 目标与非目标

### 北极星（最终方向）
> **让原生 Hermes 的数据处理主能力，服务多个企业微信用户；用户之间文件完全隔离；处理这类任务的体验和用私人版一样。实现方式是沙箱隔离，而不是砍工具的白名单。**

一句话：**用「沙箱做执行边界」而不是「阉割工具」来换隔离；在这个前提下逐步逼近"原生能力 − 联网"。**

> ⚠️ 措辞边界（P4）：**一期恢复的是文件/终端/代码执行/技能读取/记忆检索等「数据处理主能力」，不等于完整 Hermes 全工具面**。原生核心工具远不止一期这些（还有 browser / image_generate / delegate_task / cronjob / computer_use / kanban 等，见 `toolsets.py`），它们不在一期范围。

### 核心主张（推翻旧方案）
- **旧方案错在哪**：靠"受限工具白名单"换隔离，直接砍掉 execute_code 等大量工具，能力大幅削弱；且模型缺工具缺说明书，行为退化（幻觉、把 PDF 当文本读、乱 pip install）。
- **本方案主张**：**沙箱本身就是执行边界**。把碰文件/跑代码的工具全部下沉进「每 session 一个的 owner 容器」，隔离由容器墙保证，于是可以**在同一套隔离下还原数据处理主能力**，能力和隔离不再互相牺牲。

### 非目标（一期）
- 不追求让每个原生工具都能用——**联网类（web/浏览器）、桌面控制、平台集成、媒体生成一期不给**。
- 不做跨用户协作（共享文件/互看结果）。
- 不做用户自建 skills / 自加 plugins。

### 铁律（不可破）
- 每会话 prompt 缓存神圣不可破（唯一例外：上下文压缩）。
- 窄腰内核，能力长在边缘——**本方案不新增任何核心工具**。
- **不改工具的语义 / schema / 模型可见行为**——完全信任原生工具的功能与该项目 harness 工程水平（见 §11）。
  - 但**允许且必须**改的是**隔离接线**：file / execute_code / vision_analyze 的「执行环境选择」与 fail-closed 守卫（把它们从"进程内 / 全局 env / 任意路径"收敛到"owner 容器 / owner workspace"）。这不是改工具逻辑，是把工具接到正确的隔离边界上（P1 澄清）。

### 威胁模型与边界（一期，必须显式声明）
**Docker 在本方案里是"一期工程隔离边界"，不是"最高等级安全边界"。** 现有加固（`docker.py:327`：`--cap-drop ALL` + 选择性 cap-add、`--security-opt no-new-privileges`、`--pids-limit 256`、`--network=none`、资源上限、只挂 owner workspace）足以支撑一期，但它是**标准容器加固，不是抗逃逸沙箱**。

**一期防的是（威胁模型内）：**
- 跨 workspace 访问（用户 A 读/写 B 的文件）
- 误挂载 / 路径穿越导致的越权
- 工具执行退回宿主机（fail-closed 守卫兜底）
- 容器内用户代码的网络外传（`--network=none`）

**一期不承诺抵御（威胁模型外，接受的残余风险）：**
- 内核级容器逃逸 / 0-day（cap-drop + no-new-privileges 提高门槛但非绝对）
- 侧信道 / 资源耗尽型 DoS 的完全防护

**终局硬化选项（future work，非一期）**：若目标升级为"强不可信用户 + 高敏数据"，应评估 rootless + user namespace remap、gVisor、Firecracker microVM、或独立 worker 节点。**一期先不做，但在文档里认领这条演进路线，避免把 Docker 误当成终极边界。**

### 数据外传边界（egress 原则 + 视觉两条路线）
**原则（贯穿全局）**：禁网的本质是**防外泄**。egress 边界 =「数据不流向**未批准的外部**」，**on-prem 服务调用不算外泄**。于是：
- 允许的 egress：已批准的 LLM provider（Claude）、**on-prem 本地模型**。
- 禁止的 egress：容器内用户代码触达任意外部互联网；工具抓取任意外部 URL。

**看图有两条路线，都保留：**
1. **主模型原生多模态附图**：把用户上传的图**字节**直接进主模型（Opus 4.8 自带视觉）。egress = 主模型 provider，与消息/文件同信任类。
2. **`vision_analyze`（辅助视觉）**：图 → 独立视觉模型 → 文字描述 → 主模型消息列表。**provider 钉死为 on-prem 本地部署的小视觉模型**——数据不出内网 + 分担主模型成本。

**入站图片走哪条，由网关确定性预判（不是模型自选）**：`agent/image_routing.py:decide_image_input_mode()` 每轮读 `agent.image_input_mode`（auto/native/text）——auto 下：显式配了 `auxiliary.vision.provider` → 走路线 2；否则主模型 `supports_vision` → 走路线 1；否则路线 2。
> **部署选择**：把 `auxiliary.vision.provider` 指向 on-prem 本地视觉模型，即让入站图片默认走路线 2（本地模型预描述）→ 省主模型成本 + 不外泄，**靠 config 实现、无需改代码**。想让主脑亲自看像素则设 `image_input_mode: native`。主动权在 operator 配置。

**`vision_analyze` 的两道约束（缺一不可，注意是两个独立的轴）：**
- **外泄轴（本地 provider 已解决）**：provider 必须是 on-prem 本地视觉模型（部署约束：`auxiliary.vision` 指向内网端点）；**禁 HTTP/HTTPS URL 抓取**（防 SSRF + 防外联，用户上传的图本就是本地文件，无需 URL）。
- **文件越权轴（本地 provider 不解决，必须单独做）**：`vision_tools.py:872` 原生可读**任意主机路径** → 多租户下必须 owner 收敛：只允许读**当前 owner workspace/uploads** 内的图，越界拒。否则本地视觉模型会沦为"跨用户读图"帮凶。

---

## 2. 核心架构：大脑在主机，双手在容器

一个定调事实：**agent 的"大脑"（推理循环）必须调 LLM、需要联网；而容器要 `--network=none`。所以"把整个 agent 塞进容器"不可能**（无网容器连不上 LLM）。因此边界天然划成：

```
┌─ 主机（gateway 进程，有网，永远多租户）──────────────────┐
│  · 企业微信回调、owner_key 解析、session 路由            │  ← 靠 owner_key，fail-closed
│  · agent 推理循环（调 LLM）                             │  ← 大脑，必须在这
│  · vision_analyze（调视觉模型 API；仅读 owner workspace  │  ← 主机侧能力（受限，见§7）
│    内本地图，禁 HTTP URL）                               │
│  · memory / session_search（读 owner 隔离的库/目录）     │  ← 只读共享库，owner 收敛
│  · todo / clarify（纯逻辑）                             │
│                                                        │
│   每个 session ↓ 对应一个 owner 容器                     │
│  ┌─ owner 容器（--network=none，只挂自己的 /workspace）─┐  │
│  │  · terminal（前台）                      ← 双手      │  │
│  │  · read_file / write_file / patch / search_files    │  │  ← 碰文件/跑代码的，全在这
│  │  · execute_code（remote 模式）           ← 双手      │  │
│  │  · 技能脚本执行（靠上面几个）                          │  │
│  │  · 只读挂载：公司统一 skills                          │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**碰文件/跑代码的工具全部下沉进 owner 容器（双手在笼子里）；调 LLM/看图/读共享库的留在主机（大脑和记忆在外面，但按 owner 隔离）。** 容器无网不影响大脑，因为大脑本就不在容器里。

### 这不是新架构
原生单人 Hermes 开 docker 后端（`TERMINAL_ENV=docker`）时**就是这个结构**：大脑在主机调 LLM，terminal + file 工具都在容器里执行（`ShellFileOperations` 跑在 `DockerEnvironment` 上）。**本方案 = 把原生这套 docker 后端「按 owner 复制成多份、并强制启用」**。低风险，复用久经考验的原生机制，不是另起炉灶。

---

## 3. 组件划分

### 主机侧（有网，多租户）
| 组件 | 职责 | 隔离靠什么 |
|---|---|---|
| 企业微信入口 + owner 解析 | 收回调、算 `owner_key`、判 session 归属、路由 | fail-closed：取不到 owner 就拒 |
| agent 大脑 | 调 LLM、决定调哪个工具、拼消息列表 | 每 session 一个 AIAgent 对象 |
| host 侧工具 | `memory`、`session_search`、`todo`、`clarify` | owner_key 收敛 + 目录/库哈希隔离 |
| `vision_analyze`（受限）| 看图（辅助）| **只读 owner workspace/uploads 内本地图；禁 URL；provider 钉死 on-prem 本地视觉模型**（P3，见 §7 与 §1 外传边界）|
| 主模型原生多模态附图 | 主脑直接看图 | 走上传管线（已 owner 隔离）；无需改工具 |
| override 构建器 | `build_owner_sandbox_overrides(owner_key)` → 钉死的 docker 配置 | 集中设死 |

### 每 session 的 owner 容器（无网，只挂自己的 /workspace）
| 组件 | 职责 | 隔离靠什么 |
|---|---|---|
| terminal（前台）| 跑命令 | 容器墙 |
| read_file / write_file / patch / search_files | 结构化读写改搜文件 | 同容器、同 /workspace |
| execute_code | 跑 Python、可调用工具 | 同容器（remote 模式）|
| 技能脚本执行 | 靠上面三者跑 | 同容器 |
> `process`（后台进程管理）、`read_terminal`（桌面 App 专用）**一期不给**：前者依赖 background，而 background 在多租户 sandbox 已被拒（`terminal_tool.py:1925`）；后者无 in-app callback 时直接报错（`read_terminal_tool.py:24`）。见 §4（P2）。

---

## 4. 一期工具集（13 个）

`constrain_toolsets_for_owner` 选出的固定 allowlist（**这是选择/策略，不是改工具代码**）：

```
容器内执行：   read_file write_file patch search_files
              terminal（前台）
              execute_code
主机侧执行：   vision_analyze（受限：仅 owner 本地图、禁 URL）  memory  session_search  todo  clarify
只读公司资产： skills_list  skill_view   ← 加载公司统一开发的技能（共享只读、无 owner 数据；
                                          技能脚本的「执行」走容器 terminal/execute_code）
一期不给：     process  read_terminal（P2：process 依赖已被拒的 background；read_terminal 桌面 App 专用）
关掉：         web_search  web_extract（联网，一期不给）
              skill_manage（不许用户自建技能）
```

### 工具治理原则（运营方集中控制，用户零注入）
- **用户侧零注入**：owner 会话只看运营方 config 里钉死的 allowlist；plugin/MCP 运行时注入的工具一律忽略（`constrain_toolsets_for_owner` 已如此），动态 reload 被拒（`dynamic_reload_denied`）。
- **运营方集中添加**：要给全体用户加能力 = 把工具加进多租户 allowlist（config 层）。这是唯一添加通道。
- **加前过分类闸**：碰文件/跑代码→必须能在容器执行；要联网→一期不给；碰共享后端→必须 owner 收敛；调外部 API 无租户数据→可主机侧（且须 owner 收敛，见 vision）。
- **skills 与 plugins 同定位**：公司统一开发/选定、只读、用户只能用不能加。
- **skills 来源硬约束（P5/P4）**：多租户部署**必须**把 skills 来源锁定为 operator 管理的**只读目录**；禁用用户可写的 skill roots、`skills.external_dirs`、以及 plugin skill 来源（`skills_tool.py` 默认会读这些）。容器内 skills 为只读挂载（`docker.py:684`）成立，但**来源治理不能只写 config，要落到代码 enforce**——具体 enforce 层与实施任务见 §9 步骤 5。

---

## 5. 数据流（A、B 用户并发跑「PDF→报告」）

```
                        企 业 微 信
   用户A ─"上传PDF+出报告"─┐                    ┌─"上传PDF+出报告"─ 用户B
                          ▼                    ▼
╔════════════════════════════════════════════════════════════════════╗
║                  Gateway 主机进程（有网 · 永远多租户）                  ║
║   owner_key_A                           owner_key_B                 ║
║   ┌────────┐                            ┌────────┐                  ║
║   │AIAgent A│ 大脑(调LLM)                │AIAgent B│                  ║
║   │session sA│                           │session sB│                 ║
║   └───┬────┘                            └───┬────┘                  ║
║   memory/owners/hashA ◄─owner隔离─► memory/owners/hashB             ║
║   vision_analyze（主机侧，按owner分别调）                             ║
║   注册override(sA):docker,挂hashA,无网   注册override(sB):挂hashB,无网 ║
║       │ docker exec ↕ stdout                │ docker exec ↕         ║
╚═══════┼══════════════════════════════════════┼═════════════════════╝
   ┌────▼──────────────┐              ┌─────────▼─────────┐
   │ 容器A(--network=none)│            │ 容器B(--network=none)│
   │ terminal/file/     │              │ terminal/file/     │
   │ execute_code       │              │ execute_code       │
   │ →pdfplumber→docx   │              │ →pdfplumber→docx   │
   │ /workspace 只读挂skills│           │ /workspace 只读挂skills│
   └────┬──────────────┘              └─────────┬─────────┘
        │ bind mount                            │ bind mount
   /data/ws/hashA   ✕ 互相够不到 ✕   /data/ws/hashB
   ├uploads/A.pdf                              ├uploads/B.pdf
   └report_A.docx                              └report_B.docx
        │                                       │
   发回用户A                                 发回用户B
```

**隔离保证**：①容器A只挂hashA、容器B只挂hashB→物理够不到彼此文件（容器墙，非路径校验）；②memory/session按owner_hash分目录/库；③**容器内用户代码不能外传**（`--network=none`）；**host 侧 egress 仅限已批准的 LLM provider / on-prem 视觉模型**（见 §1 egress 原则）；④两容器执行/环境变量不串；⑤公司skills只读共享，改不了也加不了；⑥大脑/记忆/看图在主机侧按owner分别处理。

**一次任务时序**：回调→解析owner_key→取/建session、存PDF到owner uploads、注册容器override(键=session_id)→大脑注入owner记忆+工具schema、上传以/workspace/uploads/呈现→模型用execute_code/terminal跑pdfplumber→docker exec在容器执行、stdout回主机、产物写/workspace(=主机owner目录)→write_file/terminal出word(同容器同/workspace,中间文件都在)→主机把report.docx作附件发回。

---

## 6. 三个生命周期（解耦，各有各的时钟）

### ① 容器 —— 按 session，空闲即回收
- 粒度：每 session 一个（键 = `session_id`）；懒创建；同 session 内 terminal/file/execute_code 复用同一容器（同 /workspace，中间文件互通）。
- 回收：空闲 300s 自动回收；回收后同 session 下次执行重建新容器——**数据不丢**（工作文件在主机 bind mount 上）。
- 约束：无网、资源上限（cpus/memory_mb）、只挂 owner /workspace + 只读公司 skills。

### ② Session —— 永不自动重置，只有 /new 才结束
- `default_reset_policy.mode = none`；活会话（AIAgent 对象 + snapshot）键 = `session_id`。
- `/new`：杀 AIAgent 对象、清 snapshot、回收该 session 容器。
- "一 session 一任务，从不回头"；对话记录持久化进 `state.db`（owner 隔离），可被 `session_search` 事后检索。

### ③ Workspace —— 按 owner，永久
- 粒度：每 owner 一个（键 = `owner_hash`），跨 session 共享、永久。
- 位置：主机 `owner_workspace_root/<hash>`，bind 挂进容器 /workspace；上传落 `<root>/uploads/`。
- 容器回收、`/new` 都不删它。

### 语义确认
**同一 owner 的两个并发 session = 两个不同容器，但共享同一 /workspace**（同一个人的文件，互相可见，合理）；**跨 owner 完全隔离**（不同 workspace + 不同容器）。

---

## 7. 隔离边界与 fail-closed 全景

7 个关口，每个"拿不到隔离前提就拒绝，绝不降级成不安全路径"：

| # | 关口 | 触发 | 处置 |
|---|---|---|---|
| 1 | owner_key 缺失 | 多租户下取不到 owner | `OwnerKeyMissing` → 拒请求，不退回全局会话 |
| 2 | **override 缺失/注册失败** | 三个执行工具解析不到 `env_type=docker` | **拒绝执行**，绝不退回主机 local ← 本方案给 file/execute_code 也加这道守卫 |
| 3 | 上传路径越界 | `_to_workspace_view` 算出越界 | 返回 None → 跳过附件，不泄漏宿主机路径 |
| 4 | session 归属不符 | 访问别人的 session | 返回 "not found"（非 "forbidden"，防枚举）|
| 5 | docker 不可用/容器起不来 | 环境异常 | 拒绝并给清晰错误，不退回主机 |
| 6 | 动态 reload / plugin 注入 | 用户想加工具 | 多租户下拒绝 |
| 7 | **vision_analyze 文件越权（P3）** | 图片路径越出 owner workspace，或传入 HTTP/HTTPS URL | 越界路径拒绝（/workspace→owner_root 映射后校验）；URL 直接拒（防 SSRF/外联）|

**关口 2 是命门**：撤掉 Option A 后，file/execute_code 一旦拿不到 owner 容器**必须拒绝**——否则退回主机本地跑（`TERMINAL_ENV` 默认 local），当场破隔离。
**关口 7 是隐蔽漏洞（且与 provider 在哪无关）**：vision_analyze 在主机侧跑、原生可读任意路径（`vision_tools.py:872`）。即便 provider 已钉死为 on-prem 本地模型（解决了外泄），**文件越权仍在**——用户 A 可让它读 `/data/ws/hashB/*.png`，本地模型照样把 B 的图描述给 A。所以 owner 路径收敛不能省。

---

## 8. 极简环境提示（唯一的 prompt 注入）

恢复数据处理主能力后，原生说明书会**随工具自动回来**（门控在 `valid_tool_names` 上）；**缺哪些工具（web/process/read_terminal/skill_manage 等）由工具 schema 本身表达**——模型看不到就不会用，无需在 prompt 里逐条声明。**模型真正需要稳定知道、又无法从 schema 推断的差异，只有两件：sandbox 无网 + workspace/uploads 视图。** 因此只注入一段**极简英文**环境说明（英文以适配原生工具风格），放进 stable 段（全程不变，不破缓存）：

```text
# Execution environment (per-user isolated sandbox)
- Your terminal, code execution, and file operations run inside an isolated
  container that mounts only your own workspace at /workspace. You cannot see
  the host or any other user's files.
- The sandbox has no network access. Do not try to install packages or fetch
  from the internet; the libraries you need are already installed.
- User-uploaded files are under /workspace/uploads/.
```
> 中文备注（作用）：只交代"隔离沙箱 + 无网 + 上传在 /workspace/uploads"三件隔离事实，不教模型怎么用工具（那交给原生说明书）。这是隔离上下文，不是工具逻辑。

---

## 9. 从现状（旧方案 T1–T15）安全迁移的顺序

好消息：旧实现的 override 构建、请求入口注册、docker 挂载、fail-closed 模式**大部分复用**。本方案是"把同一套模式套到 file + execute_code，并撤掉 Option A"，增量、非重写。

按安全优先排序：
1. **调工具集**：多租户 sandbox toolset 换成一期 13 个（加 execute_code / vision_analyze，去 web_search/web_extract/skill_manage；**不加** process/read_terminal）。当前 `toolsets.py:567` 还是旧集（受限 + terminal + web），这一步替换它。
2. **file 工具下沉 + 加守卫（必须原子做）**：移除 `_get_file_ops` 的 Option A 进程内短路（`file_tools.py:801`）→ file 工具走和 terminal 同一条 `_create_environment` 路径 → 按 session override 解析到 owner 容器；**同时**给 file 工具加 fail-closed 守卫（关口 2）。⚠️ 分开做会出现"file 工具在主机裸奔"的窗口。
3. **execute_code 下沉**：当前 `code_execution_tool.py:1105` 读全局 `_get_env_config()["env_type"]`，**改成先看 session override**（`resolve_task_overrides`）解析到 owner 容器的 remote 模式；同样加 fail-closed 守卫。
4. **vision_analyze 收敛（P3，两轴）**：①**文件越权轴**——把**模型主动调用时自己给的** `/workspace/...` path 映射到 owner_root 并校验不越界，越界拒（代码）；②**外泄轴**——禁 HTTP/HTTPS URL 抓取（代码）+ 部署约束 `auxiliary.vision` 指向 on-prem 本地视觉模型（config）。
   > 注：**入站上传图的自动路由**（`decide_image_input_mode`）走已 owner 隔离的上传管线，天然安全；本收敛只针对**模型主动调 vision_analyze 时给的 path**。原生多模态附图那条路无需改动（走 `image_routing.build_native_content_parts`）。
5. **skills/plugin 来源治理（P4——落到代码，不只写 config）**：enforce 分两层——
   - **toolset 层（已定）**：allowlist 不含 `skill_manage`，用户无创建/编辑技能的工具。
   - **来源解析层（要做）**：多租户模式下，对 skills 来源（`SKILLS_DIR` + `get_external_skills_dirs()`@`skill_utils.py:416` + plugin skill 来源）做**只读来源校验**——只允许 operator 管理的只读目录，拒绝/忽略用户可写路径与 plugin 动态来源。实施计划里要点名这个 enforce 位置，并决定"启动期断言 vs 部署清单 gate"。
6. **验证三者共用一个容器**：terminal 写的文件，file/execute_code 立刻能读到。
7. **能力清单收敛**：把旧的完整能力清单换成 §8 的极简英文版。
8. **path-check 处置**：容器成为主边界后，保留 `/workspace` 路径校验作为第二道防线（defense-in-depth），后续若挡到合理的容器内操作再放松。

---

## 10. 错误处理（防"假装成功"）
- 容器 OOM / 超时：工具返回结构化错误（output + exit_code），模型知道失败、不编造；配资源上限 + 执行超时。
- 并发过载：信号量（`max_concurrent`）限制同时执行数，超出排队。
- **不修 read_file 的 PDF 行为**——原生模型有完整工具集会自己用 execute_code/terminal，不会走到 read_file 读 PDF（见 §11）。

---

## 11. 改与不改的边界（信任原生，但接对隔离边界）

**不改（工具的语义 / schema / 模型可见行为）：**
- read_file / write_file / patch / search_files / terminal / execute_code 的**功能与输出格式**一律不动。完全信任原生工具能力与该项目 harness 工程水平。
- **不补"教模型用工具"的说明**：原生说明书门控在工具存在性上，工具还原 → 说明书自动回来。
- **不修 read_file 的 PDF 行为**：原生模型有完整工具集会自己用 execute_code/terminal，不会走到 read_file 读 PDF。

**要改（隔离接线——不是工具逻辑，是把工具接到正确的边界，P1/P3）：**
- `file_tools._get_file_ops`：撤 Option A，改走 owner 容器 + 加 fail-closed 守卫。
- `code_execution_tool`：环境选择从全局 `env_type` 改为按 session override 进 owner 容器 + 加守卫。
- `vision_analyze`：加 owner workspace 路径收敛 + 禁 URL。

**依据**：单人私人版处理 PDF 完全可行、无需改工具语义。旧方案退化是因为砍了工具 + 丢了说明书，而非工具本身有问题。把隔离接线做对（本方案），模型行为即与单人版一致。

---

## 12. 测试策略（尊重项目"定向 pytest、ruff/mypy/全量暂跳"约定）

**第 1 层 · dev 环境可测（mock docker，用 `scripts/run_tests.sh`）**
- toolset 选出的是一期 13 个（且不含 process/read_terminal/web/skill_manage）。
- file / execute_code 在无 override 时拒绝（关口 2）。
- 撤 Option A 后 file 工具走 docker 路径（mock `_create_environment` 验证解析到 docker env，而非 LocalEnvironment）。
- execute_code 按 session override 解析到 owner 容器（而非全局 env_type）。
- **execute_code 子工具收敛**：容器内可调的子工具 = `SANDBOX_ALLOWED_TOOLS ∩ 当前 owner 的 13 工具 allowlist`（`code_execution_tool.py:264`）；断言 **web_search/web_extract 等不会因掉参回退/全局默认被带进沙箱**。
- **skills 来源治理**：多租户下**拒绝** `skills.external_dirs`、plugin skill、用户可写 skill root，只认 operator 只读目录（enforce 在 §9 步骤5 的来源解析层）。
- **vision_analyze 直接工具调用**：覆盖 ①模型给的越界 path 拒绝、②HTTP/HTTPS URL 拒绝、③`/workspace/...` → owner_root 映射正确（P3）。
- override 构建正确（挂 owner 目录、无网、资源上限、skills 只读）。
- 上传越界返 None、session 防枚举、memory/session 按 owner_hash 隔离。

**第 2 层 · 需真实 docker（本地已有 docker 29.6.1，或服务器）**
- A/B 双 owner 真隔离（A 容器读不到 B 文件）。
- 三工具共用一个容器（terminal 写→file/execute_code 读）。
- `--network=none` 真断网（容器里 pip install 外网失败）。
- 越界拒绝、fail-closed 真拒绝。
- 端到端冒烟：PDF → Word 报告。
- 单用户回归：`multi_tenant` 关时行为零变化。
