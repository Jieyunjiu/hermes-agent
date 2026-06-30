# 通用知识笔记：工具调用工程 + 历史/记忆注入工程

## 笔记概览

这份笔记讲清两件接大模型时绕不开的工程：

1. **工具调用（tool calling / function call）的全套机制**——模型层（为什么模型能填对参数）+ 工程层（不稳定输出怎么兜底），以及**如何亲手创建一个合格的工具**（含通用工具与受限工具两个完整示例）。
2. **历史记录与记忆文件的注入工程**——它们怎么进到模型的上下文里。

核心心态贯穿全篇：**模型负责"聪明"（理解意图、选工具、填合理值），工程负责"可靠"（兜住它必然会犯的错）。把模型输出当成"不可信输入"来设计管道。**

用 Hermes Agent（Python）的代码作实例；行号基于当前版本，可能漂移，重点看原理。

---

## 一、工具调用的全貌：两个层面

让模型"返回我们要的工具名 + 参数"，靠两层叠加，缺一不可。

### 1. 模型层（为什么模型能填对）

三个子机制：

- **训练出的能力**：现代模型被专门做过 function calling 的指令微调/RLHF——它生成 tool_call 不是自由发挥，而是被训练成"对着 schema 填表"这个行为。
  > **使用前提**：你接的模型**必须受过 function calling / tool use 训练**。一个没训练过工具调用的纯文本模型，给它 schema 它也不会稳定地吐结构化 tool_call——这时只能退而求其次用"JSON 模式 + 提示词约束 + 自己解析"。选模型时这是硬条件。
- **推理时 schema 在上下文**：每次调用，把工具的 JSON Schema 和消息**一起发给模型**。模型"知道"参数要求，是因为要求当场就在它眼前。
- **受约束解码（constrained decoding，厂商侧）**：生成时按 schema 语法约束采样，多数情况下吐出来就是**合法 JSON、字段形状正确**。部分厂商有 "strict" 模式硬保证贴合 schema。

**关键认知**：即使 schema 完美，模型仍是**概率性**的——长上下文、弱模型、任务有歧义时照样会错。**正因为"不保证"，才需要第二层。**

### 2. 工程层（不稳定输出怎么兜底）——四层防御

没有任何单层 100%，所以叠四层，每层兜住上一层漏的：

```
L1 schema 在上下文 + 好描述   →  指引模型怎么填
L2 受约束解码(厂商)           →  多数情况吐合法 JSON
L3 应用层消毒/修复            →  兜住格式损坏(坏 JSON、尾逗号、截断)
L4 工具校验 + 错误回喂        →  兜住语义错误(漏必填、值非法),模型看到错误自己改对
```

- **L3 应用层修复**（Hermes 在发请求前后做）：`agent/conversation_loop.py` 里 `_sanitize_tool_call_arguments`（:707）、`_repair_tool_call_arguments`（:869）会修复损坏的 tool_call 参数 JSON。
- **L4 错误回喂（真正的安全网）**：工具**不信任模型输入**，自己校验；不过就返回一条**可执行的错误**，这条错误作为 tool 结果**回喂给模型**，模型下一轮自己改对：

```
模型填错/漏参 → 工具校验不过 → 返回错误文本 → 回喂模型 → 模型看懂 → 重新调用(改对)
```

> **错误信息的质量决定自愈效率**：差错误 "invalid input"（模型不知道改哪）；好错误 "Content is required for action='add'"（精确指出缺什么、怎么补）。**可执行的错误 = 好售后。**

---

## 二、如何创建一个合格的工具（步骤 + 两个完整示例）

### 通用步骤（5 步）

1. **写 Schema（JSON Schema）**：`name` + `description`（含 WHEN/HOW 引导）+ `parameters`（每个参数的 `type`、`enum` 限定取值、`required` 必填）。
2. **写 Handler（执行函数）**：① 防御性取参（`args.get("x", 默认)`）；② **校验**（非法/缺失就返回可执行错误）；③ 干活；④ **返回 JSON 字符串**（成败都是 JSON）。
3. **注册**：`registry.register(name, toolset, schema, handler, check_fn, requires_env)`。
4. **接入工具集**：把工具名加进某个 toolset（`toolsets.py`），否则注册了也不暴露给模型。
5. **（受限工具额外）**：在 Handler 里做路径/权限校验、fail-closed。

> 注册 ≠ 暴露：自动发现会 import 并注册 schema，但工具只有出现在某个**工具集**里才会真正发给模型。

### 示例 A：通用工具——一个"shell/命令"工具

最"强"的工具，参数是一坨自由文本命令（如 Hermes 的 `terminal`，参数就一个 `command` 字符串）。它强在灵活，**也最难做安全校验**——这正是受限场景要关掉它的原因。

```python
import json, subprocess
from tools.registry import registry

# 1) Schema —— 自由文本命令
RUN_SHELL_SCHEMA = {
    "name": "run_shell",
    "description": (
        "Execute a shell command and return its stdout/stderr. "
        "Use for ad-hoc system tasks. Prefer structured tools when one exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Max seconds (default 60)."},
        },
        "required": ["command"],     # command 必填
    },
}

# 2) Handler —— 防御取参 + 校验 + 返回 JSON 字符串
def run_shell(command: str = "", timeout: int = 60) -> str:
    if not command.strip():                                   # L4 校验:可执行错误
        return json.dumps({"success": False,
                           "error": "command is required and must be non-empty."})
    try:
        out = subprocess.run(command, shell=True, capture_output=True,
                             text=True, timeout=timeout)
        return json.dumps({"success": True, "stdout": out.stdout,
                           "stderr": out.stderr, "exit_code": out.returncode})
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": f"timed out after {timeout}s"})

# 3) 注册（handler 把模型给的 args 防御性地映射成函数参数）
registry.register(
    name="run_shell",
    toolset="terminal",
    schema=RUN_SHELL_SCHEMA,
    handler=lambda args, **kw: run_shell(
        command=args.get("command", ""),       # 漏传→空串→走校验分支,不崩
        timeout=int(args.get("timeout", 60)),
    ),
)
```

要点：① `args.get(... , 默认)` 防御取参，漏传不崩；② 非法输入**返回 JSON 错误**而非抛异常（错误会回喂模型）；③ handler **永远返回 JSON 字符串**。

### 示例 B：受限工具——路径钉死在用户工作区 + fail-closed

企业内部 / 多租户场景的主力形态：**结构化参数 + 路径由代码钉死 + 拿不到身份就拒绝**。模型能"请求"读某文件，但"落在哪个目录"由代码按当前用户身份决定，模型碰不到。

```python
import json
from pathlib import Path
from tools.registry import registry
from gateway.multi_tenant import (
    multi_tenant_enabled, get_current_owner_key, owner_workspace_root,
    OwnerKeyMissing,
)

READ_DOC_SCHEMA = {
    "name": "read_doc",
    "description": "Read a file from the current user's workspace (relative path).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Relative path inside YOUR workspace, e.g. 'reports/q1.txt'."},
        },
        "required": ["path"],
    },
}

def read_doc(path: str = "") -> str:
    if not path:
        return json.dumps({"success": False, "error": "path is required."})

    # —— 受限核心:身份来自可信 ContextVar,不是模型可填的参数 ——
    if multi_tenant_enabled():
        try:
            owner = get_current_owner_key()        # 拿不到→抛 OwnerKeyMissing
        except OwnerKeyMissing:
            return json.dumps({"success": False,
                               "error": "no owner context; refused."})   # fail-closed:拒绝
        root = owner_workspace_root(owner).resolve()
        target = (root / path).resolve()           # 相对路径强制落到 owner 根
        if not str(target).startswith(str(root)):  # 防 ../ 越界
            return json.dumps({"success": False, "error": "path escapes workspace."})
    else:
        target = Path(path).resolve()              # 单用户老行为

    if not target.is_file():
        return json.dumps({"success": False, "error": f"not found: {path}"})
    return json.dumps({"success": True, "content": target.read_text()[:100_000]})

registry.register(
    name="read_doc",
    toolset="wecom_multi_tenant",                  # 接进受限工具集
    schema=READ_DOC_SCHEMA,
    handler=lambda args, **kw: read_doc(path=args.get("path", "")),
)
```

受限工具的三条铁律（对比示例 A 的关键差异）：

1. **身份走可信 ContextVar，不做成模型可填参数**——否则用户话术能诱导模型填别人的身份越权。
2. **路径由代码派生 + 校验**——`owner_workspace_root(owner) / path` 再 `resolve()` + 起始前缀校验，挡 `../`、软链接越界。模型只能"请求",落点由代码定。
3. **fail-closed**——多租户开着却拿不到身份,**拒绝**,绝不退化成全局/共享路径。

### 通用 vs 受限：什么时候用哪个

| | 通用工具(示例 A) | 受限工具(示例 B) |
|---|---|---|
| 参数 | 自由文本(命令) | 结构化(路径/枚举) |
| 安全 | 几乎无法校验自由命令 | 路径/权限可逐项校验 |
| 适用 | 个人开发、信任环境 | 企业内部、多租户、面向不可信用户 |
| 原则 | 灵活优先 | 收敛优先:能关就关,能结构化就结构化 |

> **能力扩展的优先级（足迹阶梯）**：扩展现有工具 → CLI 命令 + skill → 受限的 service-gated 工具 → plugin → MCP → 新核心工具(最后)。越靠前越省事、越不污染核心。企业受限场景：**把每个业务流程包成"示例 B 那种结构化受限工具",绝不开放自由 shell。**

---

## 三、历史记录的注入工程

**历史 = 永久落盘的文本，每轮被当作消息列表发给模型。**

- **存**：SQLite（Hermes 的 `state.db`），两张表——`sessions`（一会话一行,含归属/元数据）+ `messages`（一会话多行 = 消息队列,靠 `session_id` 外键关联）。存的是**人类可读文本**（role/content),不是向量/logits。
- **取**：按 `session_id` 加载该会话的 messages，组成消息列表。热路径有内存里的会话对象持有列表;冷启动才查 DB。
- **注入**：直接作为 `messages` 发给模型，**append-only**（新内容往末尾加,不改前面),以保护 prompt 缓存前缀。
- **要点**：历史是"流水账",每轮全量发送;模型靠它"记得这次对话说过什么"。

---

## 四、记忆文件的注入工程

**记忆 = 开局冻结快照，焊进 system prompt，靠缓存每轮免费带上。**（注意:这是静态记忆文件,与"记忆 provider 插件的 RAG 动态召回"是两套机制。）

- **注入（读）**：会话开始时 `load_from_disk()` 读 `MEMORY.md`/`USER.md` → 捕获一份**冻结快照** → 注入 **system prompt**（一会话构建一次、逐字节稳定）。模型每轮从**缓存好的前缀**里免费看到记忆,**不需要调工具去读**。
- **为什么这么设计**：① 缓存——system prompt 逐字节稳定,记忆搭缓存前缀,每轮零成本;② 可靠——模型永远自带记忆;③ 安全——快照在加载时冻结,会话进行中即使工具被攻陷也注入不进当前 system prompt。
- **写（触发）**：由**模型主动调 `memory` 工具**(action=add/replace/remove)触发,模型按工具描述判断"值不值得长期记"(用户偏好/纠正/环境事实优先;进度/临时 TODO/可复用流程不写)。写入前有**注入扫描**(防把恶意 promptware 写进将来会注入的记忆)和**写入门控**(审批)。
- **最精妙处——写不破当前会话缓存**：模型这轮写的记忆**落盘**,但**当前会话的冻结快照不动** → 新记忆**不出现在当前会话**(缓存不破) → **下个会话**重载快照才生效。这就是"缓存感知的延迟生效"。

> 一句话对比：**历史 = 动态文本、每轮全量发、append-only；记忆 = 静态文件、开局冻结进 system prompt、写入延迟到下个会话生效。两者都靠"不改前缀"护住缓存。**

---

## 五、迁移到你自己的项目（如结构化返回 / 翻译）

同一套工具箱:

1. **L1+L2 先用对**:别让模型自由出文本再正则抠。用厂商的 **JSON 模式 / structured output / tool use**,给 schema,让受约束解码保证形状。(前提:模型受过相应训练。)
2. **L3 收到必校验**:`pydantic` / JSON Schema 校验返回;不过则轻量修复或进第 4 步。
3. **L4 错了带"可执行错误"回喂重试**:把"缺字段 X / 字段 Y 应为数组"塞回 prompt 让它重出。带具体错误重试,命中率远高于泛泛重来。
4. **心态**:把模型输出当**不可信输入**,设计一条"出错也能自愈"的管道。

---

## 速查清单

**创建一个合格工具的检查项**：
- [ ] Schema 有清晰 description(含 WHEN/SKIP 引导)、参数有 type / 必要时 enum / required
- [ ] Handler 防御取参(`args.get(.., 默认)`),漏传不崩
- [ ] Handler 校验非法/缺失输入,返回**可执行的错误**(不抛异常)
- [ ] Handler **永远返回 JSON 字符串**
- [ ] 受限工具:身份走可信 ContextVar、路径由代码派生+校验、fail-closed
- [ ] 注册后**接进某个工具集**(否则不暴露)
- [ ] 目标模型**受过 function calling 训练**(否则改用 JSON 模式 + 自解析)

**一句话总纲**：
> 工具调用 = 模型层(训练 + 上下文 schema + 受约束解码)打底 + 工程层(严格 schema/好描述 + 防御取参/校验 + 可执行错误回喂)兜底;创建工具的本质是"写一件严格的艺术品 + 配一套健全的售后";受限工具再加"身份走可信上下文、路径代码钉死、fail-closed"。历史靠 append-only 文本每轮全量发,记忆靠开局冻结进 system prompt、写入延迟生效——都为护住缓存前缀。
