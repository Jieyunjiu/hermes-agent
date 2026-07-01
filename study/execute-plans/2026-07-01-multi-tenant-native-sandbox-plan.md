# 多租户原生数据处理沙箱化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐个实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把 file / execute_code / vision_analyze 三个工具的「执行环境选择」收敛到 owner 容器/owner workspace，恢复原生数据处理能力的同时保证多租户隔离；不改任何工具的语义/schema。

**Architecture:** 复用原生 Docker 后端（`DockerEnvironment`）与已有 owner 隔离地基（`gateway/multi_tenant.py`）。terminal 已走 owner 容器；本计划把 file/execute_code 也接到同一 owner 容器（撤掉 file 的 Option A 进程内短路），vision_analyze 做 owner 路径收敛 + 禁 URL，skills 来源在多租户下锁死为 operator 只读目录，能力清单换成极简英文环境说明。

**Tech Stack:** Python，pytest（**只经 `scripts/run_tests.sh` 调用**），Docker 后端（`tools/environments/docker.py`）。

**权威设计：** `study/multi-tenant-full-native-sandbox-design.md`（V1，经三轮审查）。

## Global Constraints

- **测试只用 `scripts/run_tests.sh <path>`**，**绝不**直接调 `pytest`（wrapper 强制 CI 一致的 hermetic 环境）。
- 本阶段 **ruff / mypy / 全量 pytest 跳过**，验证以目标逻辑的**定向 pytest** 为主；「定向通过」≠「整体验收」。
- **fail-closed**：多租户 sandbox 模式下取不到 owner docker override 必须**拒绝**，绝不退回主机 local。
- **默认行为不变**：`security.multi_tenant.enabled` 默认 `false`；单用户路径零改动。
- **prompt 缓存**：能力清单在 stable 段，改其内容仅部署时一次性刷新前缀，不得中途改。
- **注释用中文**；技术术语/变量名/路径保留英文。
- **每任务本地 commit（已授权），绝不 push**；commit body 末行 `2220603830@qq.com`。
- **一期工具集（13）**：`read_file write_file patch search_files terminal execute_code skills_list skill_view vision_analyze memory session_search todo clarify`。**不含** `process read_terminal web_search web_extract skill_manage`。
- **测试 patch 目标（P4，关键）**：本计划的实现守卫都用**函数内** `from gateway.multi_tenant import ...`。因此测试**必须 patch `gateway.multi_tenant.multi_tenant_enabled` / `.sandbox_enabled` 等原始模块属性**，而**不是** patch 工具模块上的同名引用（后者 pat 不到函数内 import）。所有 test 统一 `monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)` 形式。
- **owner 容器 override 合并（P2/P3 同根因）**：`resolve_task_overrides(task_id)` 返回的是 `build_owner_sandbox_overrides` 产出的**完整** owner override（`env_type/docker_image/host_cwd/docker_mount_cwd_to_workspace/cwd/network=False/docker_volumes=[]/mount_credentials=False/mount_skills=True/mount_cache=False/container_cpu/container_memory`）。**凡是创建容器的地方（terminal / file / execute_code）都必须把这份 override 合并进 container 创建参数，只改 env_type 会建出非 owner、有网、带全局挂载的容器。** 见 Task 2 的共享 helper。

---

## 文件结构（每个文件的职责）

- `toolsets.py` — 修改 `wecom_multi_tenant_sandbox` 定义（`:567`）为 13 工具集。
- `tools/file_tools.py` — 修改 `_get_file_ops`（`:783`）：撤 Option A、让 owner override 驱动 docker、加 fail-closed；file 工具 handler 增加取不到容器时的错误返回。
- `tools/code_execution_tool.py` — 修改 env 选择（`:1105`）为按 session override 进 owner 容器 + 加 fail-closed；子工具 allowlist 用 owner enabled_tools（`:264`）。
- `tools/vision_tools.py` — 多租户下：owner 路径收敛（`/workspace`→owner_root，越界拒）+ 禁 HTTP/HTTPS URL。
- `agent/skill_utils.py` — `get_external_skills_dirs`（`:416`）多租户下返回空/只读 operator 目录；plugin skill 来源同样收敛。
- `agent/prompt_builder.py` — `build_capability_manifest`（`:1897`）改成极简英文环境说明。

---

## Task 1: 一期工具集收敛为 13 个

**Files:**
- Modify: `toolsets.py:567`（`wecom_multi_tenant_sandbox` 的 `tools` 列表）
- Test: `tests/test_toolsets_multi_tenant_sandbox.py`

**Interfaces:**
- Produces: `TOOLSETS["wecom_multi_tenant_sandbox"]["tools"]` == 上述 13 工具（无 web/skill_manage/process/read_terminal）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_toolsets_multi_tenant_sandbox.py`：
```python
from toolsets import TOOLSETS

EXPECTED = {
    "read_file", "write_file", "patch", "search_files",
    "terminal", "execute_code",
    "skills_list", "skill_view",
    "vision_analyze", "memory", "session_search", "todo", "clarify",
}
FORBIDDEN = {"process", "read_terminal", "web_search", "web_extract", "skill_manage"}

def test_sandbox_toolset_is_exactly_13():
    tools = set(TOOLSETS["wecom_multi_tenant_sandbox"]["tools"])
    assert tools == EXPECTED, tools ^ EXPECTED

def test_sandbox_toolset_excludes_forbidden():
    tools = set(TOOLSETS["wecom_multi_tenant_sandbox"]["tools"])
    assert tools & FORBIDDEN == set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_toolsets_multi_tenant_sandbox.py`
Expected: FAIL（当前含 web_search/web_extract、缺 execute_code/vision_analyze）

- [ ] **Step 3: 改实现**

把 `toolsets.py:567` `wecom_multi_tenant_sandbox` 的 `tools` 列表改成：
```python
        "tools": [
            "read_file", "write_file", "patch", "search_files",
            "terminal", "execute_code",
            "skills_list", "skill_view",
            "vision_analyze", "memory", "session_search", "todo", "clarify",
        ],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_toolsets_multi_tenant_sandbox.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add toolsets.py tests/test_toolsets_multi_tenant_sandbox.py
git commit -m "feat(toolsets): 多租户 sandbox 工具集收敛为一期 13 个"
```

---

## Task 2: file 工具下沉 owner 容器 + fail-closed（撤 Option A，原子改）

**Files:**
- Modify: `tools/file_tools.py`（`_get_file_ops` @ `:783`；新增 `_multi_tenant_sandbox_guard` 辅助；`read_file`/`write_file`/`patch`/`search_files` handler 在调用 `_get_file_ops` 前先过守卫）
- Test: `tests/test_file_tools_multi_tenant_sandbox.py`

**Interfaces:**
- Consumes: `gateway.multi_tenant.multi_tenant_enabled/sandbox_enabled`；`tools.terminal_tool.resolve_task_overrides/_create_environment`。
- Produces: 多租户 sandbox 下 `_get_file_ops` 返回**基于 owner docker 容器**的 `ShellFileOperations`；无 owner docker override 时 handler 返回 `{"error": "...refused...", "status": "error"}`。

> **实施者必读**：先读 `tools/file_tools.py:783-860` 全貌与 `tools/terminal_tool.py:1955-1965` 的守卫原型。正常路径当前从**全局** `_get_env_config()["env_type"]` 取后端、且**不让 override 驱动 env_type**——本任务要让多租户 sandbox 下由 owner override 驱动 docker。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_file_tools_multi_tenant_sandbox.py`：
```python
import json
import tools.file_tools as ft

OWNER_OVERRIDE = {
    "env_type": "docker", "docker_image": "img", "host_cwd": "/data/ws/hashA",
    "docker_mount_cwd_to_workspace": True, "cwd": "/workspace", "network": False,
    "docker_volumes": [], "mount_credentials": False, "mount_skills": True,
    "mount_cache": False, "container_cpu": 2, "container_memory": 4096,
}


def _enable_mt(monkeypatch, *, override):
    # P4：patch 原始模块属性（实现用函数内 import）
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.sandbox_enabled", lambda: True)
    monkeypatch.setattr("tools.terminal_tool.resolve_task_overrides", lambda tid: override)


def test_no_owner_override_refuses(monkeypatch):
    # 多租户 sandbox 但 override 无 docker -> 守卫拒绝
    _enable_mt(monkeypatch, override={})
    out = ft._handle_read_file({"path": "/workspace/x.txt"}, task_id="sess-1")
    assert json.loads(out)["status"] == "error"
    assert "refused" in json.loads(out)["error"]


def test_owner_override_drives_full_container_config(monkeypatch):
    # 有 owner docker override -> 创建容器时必须用 override 的 host_cwd/network/mount，而非全局 config
    captured = {}
    _enable_mt(monkeypatch, override=OWNER_OVERRIDE)

    def fake_create_env(*, env_type, image, cwd, container_config=None, host_cwd=None, **kw):
        captured["env_type"] = env_type
        captured["cwd"] = cwd
        captured["cc"] = container_config or {}
        captured["host_cwd"] = host_cwd  # host_cwd 是独立 kwarg，不在 cc 内
        class _Env: pass
        return _Env()
    monkeypatch.setattr("tools.terminal_tool._create_environment", fake_create_env)

    ft._get_file_ops(task_id="sess-1")
    assert captured["env_type"] == "docker"
    # 关键断言：容器参数来自 owner override，不是全局 config
    assert captured["cc"].get("docker_mount_cwd_to_workspace") is True
    assert captured["cc"].get("network") is False
    assert captured["cc"].get("mount_credentials") is False
    assert captured["cc"].get("mount_cache") is False
    assert captured["host_cwd"] == "/data/ws/hashA"   # 独立参数，workspace 真正挂载靠它
```

> 注：`_create_environment(..., host_cwd=None)` 的 `host_cwd` 是独立形参（`terminal_tool.py:1247`）；断言意图是「容器参数（含 host_cwd）来自 owner override」。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_file_tools_multi_tenant_sandbox.py`
Expected: FAIL（当前多租户走 LocalEnvironment，无守卫）

- [ ] **Step 3: 改实现**

3a. 在 `tools/file_tools.py` 加守卫辅助（放在 `_get_file_ops` 之前）：
```python
def _multi_tenant_sandbox_guard(task_id: str) -> "str | None":
    """多租户 sandbox 下，若解析不到 owner docker override，返回结构化错误串（否则 None）。

    与 terminal 的 fail-closed 守卫同源（terminal_tool.py:1957）：override 缺 env_type=docker
    一律拒绝，绝不退回主机 local。
    """
    from gateway.multi_tenant import multi_tenant_enabled, sandbox_enabled
    if not (multi_tenant_enabled() and sandbox_enabled()):
        return None
    from tools.terminal_tool import resolve_task_overrides
    overrides = resolve_task_overrides(task_id or "default")
    if overrides.get("env_type") != "docker":
        import json
        return json.dumps({
            "error": "refused: multi-tenant sandbox requires an owner docker override, "
                     "but none was resolved for this session. File op blocked to avoid "
                     "running on the host or in a non-owner container.",
            "status": "error",
        }, ensure_ascii=False)
    return None
```

3b. **抽共享 helper（P2/P3 复用，DRY）**——在 `tools/terminal_tool.py` 新增（放在 `_create_environment` 附近），把「owner override 合并进 env_type + container 创建参数」的逻辑集中，供 terminal / file / execute_code 三处调用：
```python
def apply_owner_override(env_type: str, config: dict, overrides: dict) -> tuple[str, dict, "str | None"]:
    """把 owner override 合并到 (env_type, container_config, host_cwd) 上。

    override 缺省时退回 config（单用户/非多租户不受影响）。凡创建容器处都要用它，
    避免只改 env_type 却用了全局的 host_cwd/网络/挂载（P2/P3）。
    ⚠️ host_cwd 是 `_create_environment` 的**独立参数**（terminal_tool.py:1247），不在
    container_config 内——所以单独返回，调用方以 `host_cwd=` 传入。
    返回 (effective_env_type, container_config, host_cwd)。
    """
    if overrides.get("env_type"):
        env_type = overrides["env_type"]
    host_cwd = overrides.get("host_cwd", config.get("host_cwd"))
    cc = {
        "container_cpu": overrides.get("container_cpu", config.get("container_cpu", 1)),
        "container_memory": overrides.get("container_memory", config.get("container_memory", 5120)),
        "container_disk": config.get("container_disk", 51200),
        "container_persistent": overrides.get("container_persistent", config.get("container_persistent", True)),
        "docker_volumes": overrides.get("docker_volumes", config.get("docker_volumes", [])),
        "docker_mount_cwd_to_workspace": overrides.get("docker_mount_cwd_to_workspace", config.get("docker_mount_cwd_to_workspace", False)),
        "network": overrides.get("network", config.get("network", True)),
        "mount_credentials": overrides.get("mount_credentials", config.get("mount_credentials", True)),
        "mount_skills": overrides.get("mount_skills", config.get("mount_skills", True)),
        "mount_cache": overrides.get("mount_cache", config.get("mount_cache", True)),
        "docker_forward_env": config.get("docker_forward_env", []),
        "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
    }
    return env_type, cc, host_cwd
```
> 实施者核对 `_create_environment` / `_DockerEnvironment` 是否已消费 `network` / `mount_*`（老工作 T3/T4 已给 docker.py 加过 `mount_credentials/skills/cache` 开关与 network 透传，均经 container_config）。`host_cwd` 走独立 kwarg。若创建链尚未透传某键，一并补透传——这是 terminal 已走通的路，file/execute_code 复用同一条。

3c. 撤掉 `_get_file_ops`（`:800-803`）的 Option A 短路——删除：
```python
    _mt_root = _multi_tenant_workspace_root()
    if _mt_root is not None:
        from tools.environments.local import LocalEnvironment
        return ShellFileOperations(LocalEnvironment(cwd=str(_mt_root)))
```
删除后，在 `_get_file_ops` 构建容器处（`file_tools.py:866-880` 那段 `env_type = config["env_type"]` / `container_config = {...}`）改为调用共享 helper，并把 `host_cwd` 以独立 kwarg 传入 `_create_environment`：
```python
            overrides = resolve_task_overrides(raw_task_id)
            env_type, container_config, host_cwd = apply_owner_override(env_type, config, overrides)
            cwd = overrides.get("cwd") or config["cwd"]
            # ... 后续 _create_environment(...) 调用补上 host_cwd=host_cwd
```
（替换原先手搓 `container_config = {...}` 的那段；`_create_environment(env_type=env_type, image=image, cwd=cwd, ..., container_config=container_config, host_cwd=host_cwd)` 确保 file_ops 与 terminal 落到**同一** owner 容器、同挂载、同禁网、workspace 真正挂进去。）

3d. 在 `read_file` / `write_file` / `patch` / `search_files` 四个 handler 的最前面（解析 path 之前）加：
```python
    _mt_refused = _multi_tenant_sandbox_guard(task_id)
    if _mt_refused is not None:
        return _mt_refused
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_file_tools_multi_tenant_sandbox.py`
Expected: PASS

- [ ] **Step 5: 回归——单用户与既有 file 测试不破**

Run: `scripts/run_tests.sh tests/ -k "file_tools or file_ops"`
Expected: PASS（`multi_tenant` 关时走原路径，行为不变）

- [ ] **Step 6: Commit**

```bash
git add tools/file_tools.py tests/test_file_tools_multi_tenant_sandbox.py
git commit -m "feat(file-tools): 多租户下 file 工具下沉 owner 容器 + fail-closed（撤 Option A）"
```

---

## Task 3: execute_code 按 session override 进 owner 容器 + fail-closed + 子工具收敛

**Files:**
- Modify: `tools/code_execution_tool.py`（env 选择 @ `:1105`；子工具 stub 生成 `:259-267`）
- Test: `tests/test_execute_code_multi_tenant_sandbox.py`

**Interfaces:**
- Consumes: `tools.terminal_tool.resolve_task_overrides`；`gateway.multi_tenant.multi_tenant_enabled/sandbox_enabled`。
- Produces: 多租户 sandbox 下 execute_code 的 `env_type` 来自 owner override（docker）；无 override 拒绝；容器内可调子工具 = `SANDBOX_ALLOWED_TOOLS ∩ owner enabled_tools`（不含 web）。

> **实施者必读**：真正创建容器的是 `_get_or_create_env()`（`code_execution_tool.py:600`），它在 `:634-635` 仍 `config = _get_env_config(); env_type = config["env_type"]`（全局），并被 `_execute_remote()`（`:877,:899`）调用。**只改 `execute_code()` 入口的 env_type 不够（P3）——必须同步改 `_get_or_create_env()` 用 override 合并**。`:264` 已保证子工具 = `SANDBOX_ALLOWED_TOOLS ∩ enabled_tools`——本任务确保传入的 `enabled_tools` 是 owner 的 13 工具 allowlist。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_execute_code_multi_tenant_sandbox.py`：
```python
import json
import tools.code_execution_tool as ce


def test_no_owner_override_refuses(monkeypatch):
    # 真实入口是 ce.execute_code（注册 handler 是 lambda args,**kw: execute_code(...)）
    # P4：patch 原始模块属性
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.sandbox_enabled", lambda: True)
    monkeypatch.setattr("tools.terminal_tool.resolve_task_overrides", lambda tid: {})
    out = ce.execute_code(code="print(1)", task_id="sess-1")  # 按 execute_code 现签名对齐入参
    assert json.loads(out)["status"] == "error"


def test_subtools_exclude_web():
    # 真实签名：generate_hermes_tools_module(enabled_tools, transport="uds")
    from tools.code_execution_tool import generate_hermes_tools_module
    owner_allow = ["read_file", "write_file", "terminal"]  # 13 集的子集，无 web
    module = generate_hermes_tools_module(owner_allow)  # transport 默认 "uds"
    assert "web_search" not in module
    assert "web_extract" not in module
```

> 注：`generate_hermes_tools_module` 只生成 `SANDBOX_ALLOWED_TOOLS ∩ enabled_tools` 的 stub——传入不含 web 的 owner allowlist，web 就不会进沙箱。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_execute_code_multi_tenant_sandbox.py`
Expected: FAIL

- [ ] **Step 3: 改实现**

3a. 在 `execute_code()` 入口（`:1105` 附近）加 fail-closed 守卫（早拒，避免进创建路径）：
```python
    from tools.terminal_tool import resolve_task_overrides
    from gateway.multi_tenant import multi_tenant_enabled, sandbox_enabled
    if multi_tenant_enabled() and sandbox_enabled():
        if resolve_task_overrides(task_id or "default").get("env_type") != "docker":
            return tool_error("refused: multi-tenant sandbox requires an owner docker override; "
                              "execute_code blocked to avoid host/non-owner execution.")
```

3b. **（P3 核心）改 `_get_or_create_env()`（`:634-635`）**——让它用 override 合并，而非全局 env_type，落到 owner 容器：
```python
        config = _get_env_config()
        env_type = config["env_type"]
        from tools.terminal_tool import resolve_task_overrides, apply_owner_override
        overrides = resolve_task_overrides(effective_task_id)
        env_type, container_config, host_cwd = apply_owner_override(env_type, config, overrides)
        # 下方原来手搓 container_config 的分支改用上面这份；image/cwd 优先取 overrides；
        # _create_environment(...) 调用补 host_cwd=host_cwd（独立参数，workspace 真正挂载靠它）
```
复用 Task 2 的共享 `apply_owner_override` helper——确保 execute_code 与 terminal/file 落到**同一** owner 容器。

3c. 确保生成子工具 stub 时传入的 `enabled_tools` 是 owner 的 allowlist（多租户下即 13 集）。定位 `generate_hermes_tools_module(...)` 的调用点，把 `enabled_tools` 用当前会话 owner-constrained 的工具名列表传入（不是全局默认）。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_execute_code_multi_tenant_sandbox.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/code_execution_tool.py tests/test_execute_code_multi_tenant_sandbox.py
git commit -m "feat(execute-code): 按 session override 进 owner 容器 + fail-closed + 子工具收敛"
```

---

## Task 4: vision_analyze owner 路径收敛 + 禁 URL（多租户）

**Files:**
- Modify: `tools/vision_tools.py`（图片解析入口，`:867` 附近判定 local path / URL 处）
- Test: `tests/test_vision_multi_tenant_sandbox.py`

**Interfaces:**
- Produces: 多租户下 vision_analyze：①`http(s)://` URL 直接拒；②`/workspace/...` 映射到 owner_root 后校验不越界，越界拒。

> **实施者必读（P5——两个入口）**：vision 有**两条前置路径**都要收敛：①注册 handler `_handle_vision_analyze`（`:1197`，async）；②真正干活的 `vision_analyze_tool`（run_agent 会**直接调**它）+ native fast path（`_should_use_native_vision_fast_path` `:603`，主模型自带视觉时会短路辅助 LLM）。**必须抽一个共享的多租户 source resolver，同时挂在这两个入口的最前面**——否则从直调路径或 native 快路进来会绕过收敛。收敛复用 `gateway.multi_tenant.owner_workspace_root` + 现有路径校验（参考 `tools/file_tools.py:_validate_multi_tenant_workspace_path` / `tools/path_security.py`）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_vision_multi_tenant_sandbox.py`：
```python
import asyncio
import json
import tools.vision_tools as vt


def _enable_mt(monkeypatch, owner_root):
    # P4：patch 原始模块属性
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.owner_workspace_root", lambda k: owner_root)
    monkeypatch.setattr("gateway.multi_tenant.get_current_owner_key", lambda: "wecom:c:a:u")


def test_http_url_rejected(monkeypatch, tmp_path):
    # _handle_vision_analyze 是 async（返回 Awaitable[str]）→ 用 asyncio.run 驱动
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt._handle_vision_analyze({"image_url": "https://evil/x.png", "prompt": "?"}))
    assert json.loads(out).get("status") == "error"


def test_out_of_workspace_path_rejected(monkeypatch, tmp_path):
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt._handle_vision_analyze({"image_url": "/etc/passwd", "prompt": "?"}))
    assert json.loads(out).get("status") == "error"
```

> 注：`_handle_vision_analyze(args, **kw)` 是 **async**；越界/URL 判定要在该 handler 内 `await` 真正下载/读盘**之前**完成。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_vision_multi_tenant_sandbox.py`
Expected: FAIL

- [ ] **Step 3: 改实现**

3a. 抽共享 resolver（放 `vision_tools.py` 顶部，供两入口调用）：
```python
def resolve_owner_image_source(image_url: str) -> "str":
    """多租户下把 image_url 收敛到 owner workspace 内的本地图；越界/URL 抛错。

    返回可用的本地路径；非多租户直接原样返回。抛 ValueError 表示拒绝（调用方转 tool_error）。
    """
    from gateway.multi_tenant import multi_tenant_enabled, get_current_owner_key, owner_workspace_root
    if not multi_tenant_enabled():
        return image_url
    if image_url.startswith(("http://", "https://")):
        raise ValueError("refused: remote image URLs are disabled in multi-tenant mode.")
    root = owner_workspace_root(get_current_owner_key())
    resolved = _map_workspace_to_owner_root(image_url, root)  # /workspace→owner_root + 越界校验
    if resolved is None:
        raise ValueError("refused: image path is outside your workspace.")
    return str(resolved)
```
3b. 在**两个入口**（`_handle_vision_analyze` 与 `vision_analyze_tool`，含 native fast path 之前）最前面调用它：
```python
    try:
        image_url = resolve_owner_image_source(image_url)
    except ValueError as e:
        return tool_error(str(e))
```
> 放在 `await 下载/读盘` 与 `_should_use_native_vision_fast_path()` 判定**之前**，确保 native 快路也被收敛。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_vision_multi_tenant_sandbox.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/vision_tools.py tests/test_vision_multi_tenant_sandbox.py
git commit -m "feat(vision): 多租户下 owner 路径收敛 + 禁远程 URL"
```

---

## Task 5: skills 来源治理（多租户锁死 operator 只读目录）

**Files:**
- Modify: `agent/skill_utils.py`（`get_external_skills_dirs` @ `:416`）
- Modify: `tools/skills_tool.py`（plugin skill 发现路径 `:905-940`：`discover_plugins()` + `find_plugin_skill()` + `_serve_plugin_skill()`）——**必改**（P6）
- Test: `tests/test_skills_source_multi_tenant.py`

**Interfaces:**
- Produces: 多租户下 ①`get_external_skills_dirs()` 不返回 `skills.external_dirs`/用户可写来源；②`skill_view` 对 `plugin:skill` 限定名**拒绝加载**（不 discover/serve plugin skill）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skills_source_multi_tenant.py`：
```python
import json
import agent.skill_utils as su
import tools.skills_tool as st


def test_external_dirs_empty_under_multi_tenant(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)  # P4
    # 即便 config 里配了 external_dirs，多租户下也不采纳
    monkeypatch.setattr("agent.skill_utils._read_external_dirs_from_config", lambda: [tmp_path], raising=False)
    assert su.get_external_skills_dirs() == []


def test_plugin_skill_load_rejected_under_multi_tenant(monkeypatch):
    # 多租户下 skill_view 一个 plugin:skill 限定名 -> 拒绝，不 discover/serve
    # 真实注册 handler 是 _skill_view_with_bump(args, **kw)（skills_tool.py:1606/:1635）
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    out = st._skill_view_with_bump({"name": "superpowers:writing-plans"})
    data = json.loads(out)
    assert data.get("status") == "error" or "not available" in json.dumps(data).lower()
```

> 注：`_read_external_dirs_from_config` 若无此私有函数，按 `get_external_skills_dirs` 现有读取点对齐 patch。真实入口是 `skill_view()`（`:862`）/ 注册 handler `_skill_view_with_bump()`（`:1606`），**无** `_handle_skill_view`。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_skills_source_multi_tenant.py`
Expected: FAIL

- [ ] **Step 3: 改实现**

3a. `agent/skill_utils.py` `get_external_skills_dirs()` 开头加多租户短路：
```python
    try:
        from gateway.multi_tenant import multi_tenant_enabled
        if multi_tenant_enabled():
            # 多租户：不采纳 skills.external_dirs / 用户可写来源，只留内置只读 SKILLS_DIR。
            return []
    except ImportError:
        pass
```

3b. **（P6）`tools/skills_tool.py` plugin 发现路径（`:905-940`）加多租户守卫**——在 `discover_plugins()` / `find_plugin_skill()` 之前拒绝：
```python
            from gateway.multi_tenant import multi_tenant_enabled
            if multi_tenant_enabled():
                return json.dumps({
                    "error": f"plugin skills are not available in multi-tenant mode: {name}",
                    "status": "error",
                }, ensure_ascii=False)
            # 原有：from hermes_cli.plugins import discover_plugins, get_plugin_manager ...
```
放在 `skill_view` 处理 `plugin:` 限定名的分支入口（即 `discover_plugins()` 调用之前），确保多租户下**根本不 discover/serve** plugin skill。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_skills_source_multi_tenant.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/skill_utils.py tools/skills_tool.py tests/test_skills_source_multi_tenant.py
git commit -m "feat(skills): 多租户下锁死 skills 来源为 operator 只读目录 + 禁 plugin skill 加载"
```

---

## Task 6: 能力清单换成极简英文环境说明

**Files:**
- Modify: `agent/prompt_builder.py`（`build_capability_manifest` @ `:1897`）
- Test: `tests/test_capability_manifest.py`

**Interfaces:**
- Produces: `build_capability_manifest()` 返回 §8 的极简英文文本（含 sandbox/no-network/uploads 三条），纯函数。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_capability_manifest.py`：
```python
from agent.prompt_builder import build_capability_manifest


def test_manifest_minimal_english_facts():
    m = build_capability_manifest()
    assert "isolated" in m and "/workspace" in m
    assert "no network" in m.lower()
    assert "/workspace/uploads/" in m
    assert build_capability_manifest() == m  # 纯函数，稳定
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_capability_manifest.py`
Expected: FAIL（当前是旧的中文完整清单）

- [ ] **Step 3: 改实现**

把 `build_capability_manifest` 返回值改成：
```python
    return (
        "# Execution environment (per-user isolated sandbox)\n"
        "- Your terminal, code execution, and file operations run inside an isolated "
        "container that mounts only your own workspace at /workspace. You cannot see "
        "the host or any other user's files.\n"
        "- The sandbox has no network access. Do not try to install packages or fetch "
        "from the internet; the libraries you need are already installed.\n"
        "- User-uploaded files are under /workspace/uploads/.\n"
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_capability_manifest.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/prompt_builder.py tests/test_capability_manifest.py
git commit -m "feat(prompt): 能力清单换成极简英文环境说明"
```

---

## Task 7: 真实 docker 端到端验收（需 docker 主机，本地已有 docker 29.6.1）

**Files:** 无代码改动——按设计 §12 第 2 层清单人工逐条过。

- [ ] 构建镜像：`docker build -t hermes-sandbox:latest docker/sandbox/`
- [ ] 断网依赖验证：`docker run --rm --network=none hermes-sandbox:latest python3 -c "import pandas, docx, pdfplumber, PIL; print('deps ok')"`
- [ ] 起网关：`HERMES_HOME=~/.hermes-dev hermes gateway`（config 打开 `multi_tenant.enabled`、`sandbox.enabled`、`workspace_root` 指向可写路径、`auxiliary.vision.provider` 指向 on-prem 视觉模型）
- [ ] A/B 双 owner 真隔离：A 容器读不到 B 文件。
- [ ] 三工具共用一个容器：terminal 写的文件，file/execute_code 立刻读到。
- [ ] `--network=none` 真断网：容器里 `pip install` 外网失败。
- [ ] vision：模型主动调 vision_analyze 传越界 path / URL → 被拒。
- [ ] skills：`skills.external_dirs`/plugin 来源在多租户下不生效。
- [ ] 端到端冒烟：上传 PDF → 出 Word 报告（最初的目标任务）。
- [ ] 单用户回归：`multi_tenant.enabled=false` 时行为与原生一致。
- [ ] 记一笔进度到 `study/process/`（做了什么 / 已验证 / 未验证）。

---

## 实施顺序与依赖

T1 →（T2、T3 必须成对推进，因为 file/execute_code 要落到同一 owner 容器）→ T4 → T5 → T6 → T7（最后，需 docker）。
T2/T3 是隔离命门（fail-closed），reviewer 重点核：无 override 是否真拒、单用户路径是否零变化。
