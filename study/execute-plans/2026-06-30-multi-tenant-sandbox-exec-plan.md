# 多租户企业微信 · 沙箱执行能力恢复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤用 `- [ ]` 复选框跟踪。
>
> **本仓库约定（覆盖默认）：**
> - 沟通/注释/文档默认**中文**（技术术语/路径/标识保留英文）。
> - **不自动执行 git**：每个任务末尾的 commit 步骤给出命令，由**你确认后手动执行**（或在你明确授权下执行）。
> - 测试**只用 wrapper**：`scripts/run_tests.sh <路径>`，不要直接调 pytest。
> - 当前阶段 **ruff / mypy / 全量 pytest 跳过**，验证以**目标逻辑的定向 pytest** 为主；不要把"定向通过"写成"整体验收"。
> - 编辑前确认 `git status` 无重要未保存改动。

**Goal:** 多租户模式下安全地把 `terminal` 执行能力还给智能体——按 owner 把命令关进只挂自己 workspace、默认禁网的 docker 沙箱，并让模型对自身能力诚实。

**Architecture:** 复用 Hermes 现有 `DockerEnvironment` + `register_task_env_overrides` 注入机制：请求入口按当前 owner 注册一份沙箱 override；扩展 terminal 让 override 真正驱动 `env_type/host_cwd/network` 等并透传给容器构造；file 工具留进程内、用 `/workspace` 别名与容器互通；双 toolset 由 `constrain_toolsets_for_owner` 按配置开关。

**Tech Stack:** Python、Docker、pytest（经 `scripts/run_tests.sh`）。源文件：`gateway/multi_tenant.py`、`gateway/run.py`、`tools/terminal_tool.py`、`tools/environments/docker.py`、`tools/file_tools.py`、`toolsets.py`、`plugins/platforms/wecom/adapter.py`、`agent/prompt_builder.py`。

## Global Constraints

> 这些是项目级红线，**每个任务都隐含遵守**：

- **prompt 缓存神圣**：不中途改过去上下文、不切工具集、不重建 system prompt；新增的能力清单文本必须**会话内静态**（随 toolset 固定）。
- **内核窄腰**：不新增核心 model-tool；本方案只"接线"现有工具与后端。业务流程走 skill，不为业务加 model-tool。
- **fail-closed**：多租户开启但取不到 owner / 选不到 docker → **拒绝**，绝不退化成宿主机 local 执行 / 无 owner 全局路径。
- **不泄漏 session 存在性**：owner 校验失败返回 not found 语义（沿用现有 `assert_session_owner`）。
- **默认行为不变**：`security.multi_tenant.enabled` 默认 `false`；单用户 / 其它平台路径**零变化**——所有新逻辑都 gated 在 `multi_tenant_enabled()` 且有 owner override 时。
- **owner 隔离不变量**：**容器 per-session**（override 注册在 `task_id=session_id` 下，与 terminal 收到的对齐）、**workspace per-owner**（bind-mount 只挂 `owner_workspace_root(owner)` → `/workspace`，与容器键解耦）；跨 owner 自然隔离（不同 owner 不同 workspace）；路径校验最终都过 `validate_within_dir(owner_root)`。**不**把 task_id 改成 owner hash（它还键控 AIAgent 实例缓存，见 §5.3）。
- **配置默认值（128c/192G、~100 在线）**：单容器 `cpus=2`、`memory_mb=4096`、`max_concurrent=24`、`network=false`、空闲回收沿用 300s、pids 沿用硬编码 256。

**关联 spec（权威设计）：** `study/multi-tenant-sandbox-exec-design.md`（v4）。每个任务标注对应 spec 小节。

---

## 任务总览与依赖顺序

```
Phase 0 基础（纯函数，先做）
  T1 sandbox 配置读取
  T2 owner 沙箱 override 构建
Phase 1 后端接线（P0 核心）
  T3 DockerEnvironment 三挂载开关        (依赖无)
  T4 terminal override 合并 + network 透传 (依赖 T3)
  T5 terminal fail-closed 守卫            (依赖 T4)
  T6 terminal 禁后台进程
Phase 2 file 工具 A 方案
  T7 file 工具 /workspace 别名归一化 + 单点校验
  T8 file 工具多租户下不进容器（解耦 auto-docker）
Phase 3 工具集与入口
  T9 wecom_multi_tenant_sandbox 工具集
  T10 constrain_toolsets_for_owner 按 sandbox.enabled 选择   (依赖 T1, T9)
  T11 请求入口注册/清理 override                            (依赖 T2)
  T12 上传文件路径呈现为 /workspace/...
Phase 4 加固与体验
  T13 并发信号量
  T14 system prompt 能力清单
  T15 沙箱镜像 Dockerfile
```

**P0 关键路径**：T1 → T2 → T3 → T4 → T11。没有 T4（override 真正生效）+ T11（入口注册），"挂对 workspace""禁网"全是纸面的。建议严格按 Phase 顺序，且 T4 完成后立刻做 T4 的端到端校验再继续。

---

## Task 1: sandbox 配置读取

**对应 spec：** §8、§13 已决 #1/#7。

**Files:**
- Modify: `gateway/multi_tenant.py`（在 `_config_multi_tenant()` 附近，约 `:121-149`）
- Test: `tests/gateway/test_sandbox_config.py`（新建）

**Interfaces:**
- Produces:
  - `sandbox_config() -> dict`：返回 `security.multi_tenant.sandbox` 配置块（缺失返回 `{}`）。
  - `sandbox_enabled() -> bool`：`multi_tenant_enabled() and sandbox_config().get("enabled", False)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/gateway/test_sandbox_config.py
from unittest.mock import patch
import gateway.multi_tenant as mt


def test_sandbox_config_reads_block():
    # _config_multi_tenant() 返回 security.multi_tenant 块；sandbox_config 取其 .sandbox
    with patch.object(mt, "_config_multi_tenant",
                      return_value={"enabled": True, "sandbox": {"enabled": True, "memory_mb": 4096}}):
        cfg = mt.sandbox_config()
    assert cfg.get("enabled") is True
    assert cfg.get("memory_mb") == 4096


def test_sandbox_config_missing_returns_empty():
    with patch.object(mt, "_config_multi_tenant", return_value={}):
        assert mt.sandbox_config() == {}


def test_sandbox_enabled_requires_both_flags():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_config", return_value={"enabled": True}):
        assert mt.sandbox_enabled() is True
    with patch.object(mt, "multi_tenant_enabled", return_value=False), \
         patch.object(mt, "sandbox_config", return_value={"enabled": True}):
        assert mt.sandbox_enabled() is False
```

> patch 目标用 **`mt._config_multi_tenant`**（`gateway/multi_tenant.py:121`，它返回 `security.multi_tenant` 块）——比 patch 其内部 loader 稳。R3#4。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/gateway/test_sandbox_config.py -v`
Expected: FAIL（`AttributeError: module ... has no attribute 'sandbox_config'`）

- [ ] **Step 3: 最小实现**

在 `gateway/multi_tenant.py` 复用现有 `_config_multi_tenant()`（它已返回 `security.multi_tenant` 块）：

```python
def sandbox_config() -> dict:
    """返回 security.multi_tenant.sandbox 配置块（缺失返回空 dict）。"""
    sb = _config_multi_tenant().get("sandbox", {}) or {}
    return sb if isinstance(sb, dict) else {}


def sandbox_enabled() -> bool:
    """多租户开启 且 sandbox.enabled=true 才为真。"""
    return bool(multi_tenant_enabled() and sandbox_config().get("enabled", False))
```

> 若 `_config_multi_tenant` 内部 loader 名与测试 patch 的不一致，改测试 patch 目标对齐到 `_config_multi_tenant` 这一层（直接 `patch.object(mt, "_config_multi_tenant", return_value=...)` 更稳）。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/gateway/test_sandbox_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add gateway/multi_tenant.py tests/gateway/test_sandbox_config.py
git commit -m "feat(multi-tenant): add sandbox_config/sandbox_enabled readers"
```

---

## Task 2: owner 沙箱 override 构建函数

**对应 spec：** §5.2、§9.7。集中产出注册给 `register_task_env_overrides` 的 dict，避免散落。

**Files:**
- Modify: `gateway/multi_tenant.py`
- Test: `tests/gateway/test_owner_sandbox_overrides.py`（新建）

**Interfaces:**
- Consumes: `owner_workspace_root(owner_key)`（`:258`）、`sandbox_config()`（T1）。
- Produces: `build_owner_sandbox_overrides(owner_key: str) -> dict`，键值固定为：
  - `env_type="docker"`、`docker_image=<sandbox_config.image>`
  - `host_cwd=str(owner_workspace_root(owner_key))`、`docker_mount_cwd_to_workspace=True`、`cwd="/workspace"`
  - `network=False`、`docker_volumes=[]`
  - `container_persistent=True`、`docker_persist_across_processes=False`
  - `mount_credentials=False`、`mount_skills=True`、`mount_cache=False`
  - `container_cpu`、`container_memory`（来自 sandbox_config，默认 2 / 4096）

- [ ] **Step 1: 写失败测试**

```python
# tests/gateway/test_owner_sandbox_overrides.py
from pathlib import Path
from unittest.mock import patch
import gateway.multi_tenant as mt


def test_build_owner_sandbox_overrides_shape():
    with patch.object(mt, "sandbox_config", return_value={
        "image": "hermes-sandbox:latest", "cpus": "2", "memory_mb": 4096,
    }), patch.object(mt, "owner_workspace_root", return_value=Path("/data/workspaces/abc123")):
        ov = mt.build_owner_sandbox_overrides("wecom:c:a:u")

    assert ov["env_type"] == "docker"
    assert ov["docker_image"] == "hermes-sandbox:latest"
    assert ov["host_cwd"] == "/data/workspaces/abc123"
    assert ov["docker_mount_cwd_to_workspace"] is True
    assert ov["cwd"] == "/workspace"
    assert ov["network"] is False
    assert ov["docker_volumes"] == []
    assert ov["container_persistent"] is True
    assert ov["docker_persist_across_processes"] is False
    assert ov["mount_credentials"] is False
    assert ov["mount_skills"] is True
    assert ov["mount_cache"] is False
    assert ov["container_cpu"] == 2
    assert ov["container_memory"] == 4096


def test_build_owner_sandbox_overrides_creates_workspace_dir(tmp_path):
    """审查 R3#2：owner workspace 目录不存在时应被创建（否则 docker 不挂）。"""
    owner_root = tmp_path / "data" / "workspaces" / "abc123"
    assert not owner_root.exists()
    with patch.object(mt, "sandbox_config", return_value={"image": "img"}), \
         patch.object(mt, "owner_workspace_root", return_value=owner_root):
        ov = mt.build_owner_sandbox_overrides("wecom:c:a:u")
    assert owner_root.is_dir()           # 已创建
    assert ov["host_cwd"] == str(owner_root)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/gateway/test_owner_sandbox_overrides.py -v`
Expected: FAIL（无 `build_owner_sandbox_overrides`）

- [ ] **Step 3: 最小实现**

```python
def build_owner_sandbox_overrides(owner_key: str) -> dict:
    """owner_key → 一份钉死的 docker 沙箱 override（供 register_task_env_overrides）。

    所有隔离/加固选项在这里集中设死，调用方不再散落配置。
    """
    sb = sandbox_config()
    root = owner_workspace_root(owner_key)
    # 审查 R3#2：owner workspace 目录必须先存在，否则 docker.py:597 的 isdir 检查
    # 不通过 → host_cwd 不挂到 /workspace → 退回 sandbox 临时目录（隔离失效）。
    root.mkdir(parents=True, exist_ok=True)
    return {
        "env_type": "docker",
        "docker_image": sb.get("image", ""),
        "host_cwd": str(root),
        "docker_mount_cwd_to_workspace": True,   # 没这个 host_cwd 不会挂到 /workspace
        "cwd": "/workspace",
        "network": False,                         # 默认禁网
        "docker_volumes": [],                     # 丢弃 operator 任意 volumes
        "container_persistent": True,             # 进程内复用
        "docker_persist_across_processes": False, # 关跨进程复用
        "mount_credentials": False,               # 全局凭证绝不进容器
        "mount_skills": True,                      # 公共业务 skills（RO）保留
        "mount_cache": False,                      # 全局 cache 含他人上传，关
        "container_cpu": int(sb.get("cpus", 2)),
        "container_memory": int(sb.get("memory_mb", 4096)),
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/gateway/test_owner_sandbox_overrides.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add gateway/multi_tenant.py tests/gateway/test_owner_sandbox_overrides.py
git commit -m "feat(multi-tenant): add build_owner_sandbox_overrides"
```

---

## Task 3: DockerEnvironment 三个挂载开关

**对应 spec：** §6.2 审查 #2、§9.6。让多租户能关掉 credential/cache 自动挂载、保留 skills。

**Files:**
- Modify: `tools/environments/docker.py`（`DockerEnvironment.__init__` 约 `:515`；自动挂载块约 `:639`）
- Test: `tests/tools/test_docker_mount_switches.py`（新建）

**Interfaces:**
- Produces: `DockerEnvironment.__init__` 新增三个关键字参数（默认 True，保持旧行为）：
  `mount_credentials: bool = True, mount_skills: bool = True, mount_cache: bool = True`。
  三者分别 gate `get_credential_file_mounts()` / `get_skills_directory_mount()` / `get_cache_directory_mounts()` 的挂载循环。

- [ ] **Step 1: 写失败测试**（断言关掉后 `docker run` 参数里不含对应挂载）

```python
# tests/tools/test_docker_mount_switches.py
from unittest.mock import patch
from tools.environments.docker import DockerEnvironment


def _real_mounts(tmp_path):
    """造真实的 creds 文件 + skills/cache 目录（docker.py:648 会 is_file()/is_dir() 校验，
    假路径会被直接跳过，测不出开关——必须用真实路径，R3#4）。"""
    creds = tmp_path / "creds.json"; creds.write_text("{}", encoding="utf-8")
    skills = tmp_path / "skills"; skills.mkdir()
    cache = tmp_path / "cache"; cache.mkdir()
    return creds, skills, cache


def _run_args_for(tmp_path, **kwargs):
    """构造 DockerEnvironment 但不真正起容器，截获 docker run 参数。"""
    creds, skills, cache = _real_mounts(tmp_path)
    captured = {}
    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        class R:
            returncode = 0; stdout = "containerid\n"; stderr = ""
        return R()
    with patch("tools.environments.docker.subprocess.run", side_effect=fake_run), \
         patch("tools.environments.docker.find_docker", return_value="/usr/bin/docker"), \
         patch("tools.environments.docker._ensure_docker_available", return_value=None), \
         patch("tools.environments.docker.get_credential_file_mounts", return_value=[
             {"host_path": str(creds), "container_path": "/root/.creds.json"}]), \
         patch("tools.environments.docker.get_skills_directory_mount", return_value=[
             {"host_path": str(skills), "container_path": "/root/.hermes/skills"}]), \
         patch("tools.environments.docker.get_cache_directory_mounts", return_value=[
             {"host_path": str(cache), "container_path": "/root/.cache"}]):
        DockerEnvironment(image="img", cwd="/workspace", timeout=10, **kwargs)
    return " ".join(captured.get("cmd", [])), str(creds), str(skills), str(cache)


def test_mount_switches_off_excludes_creds_and_cache_keeps_skills(tmp_path):
    args, creds, skills, cache = _run_args_for(
        tmp_path, mount_credentials=False, mount_cache=False, mount_skills=True)
    assert creds not in args
    assert cache not in args
    assert skills in args
```

> 截获方式以源码为准：`DockerEnvironment` 在哪一步拼挂载参数、用什么调用 docker，先读 `docker.py:515-660` 与启动处，把 patch/断言对齐真实调用点（必要时断言 `self._all_run_args` 等对象属性）。**关键修正（R3#4）**：用 `tmp_path` 真实文件/目录，否则 `docker.py:648` 的 `is_file()/is_dir()` 会把假路径跳过，开关测不出来。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_docker_mount_switches.py -v`
Expected: FAIL（当前无开关，三类都会挂上 → skills 在、但 creds/cache 也在 → 断言 `not in` 失败）

- [ ] **Step 3: 最小实现**

在 `DockerEnvironment.__init__` 签名加三参数（默认 True），存到 `self`；在自动挂载块（`docker.py:639+`）把三个 `for ... in get_*():` 循环分别用对应开关包起来：

```python
def __init__(self, ..., mount_credentials: bool = True,
             mount_skills: bool = True, mount_cache: bool = True, ...):
    ...
    self._mount_credentials = mount_credentials
    self._mount_skills = mount_skills
    self._mount_cache = mount_cache
```

```python
# 自动挂载块（docker.py:639 起）
if self._mount_credentials:
    for mount_entry in get_credential_file_mounts():
        ...   # 原逻辑不动
if self._mount_skills:
    for skills_mount in get_skills_directory_mount():
        ...
if self._mount_cache:
    for cache_mount in get_cache_directory_mounts():
        ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_docker_mount_switches.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/environments/docker.py tests/tools/test_docker_mount_switches.py
git commit -m "feat(docker): add mount_credentials/skills/cache switches"
```

---

## Task 4: terminal override 合并 + network 透传（P0 核心）

**对应 spec：** §5.1、§5.2、§9.3；审查 R1#1 + R2#1 连带。让 owner override 真正驱动 `env_type/host_cwd/docker_mount_cwd_to_workspace/network/mount_*`，并把 `network`（及 mount 开关）透传给 `DockerEnvironment`。

**Files:**
- Modify: `tools/terminal_tool.py`
  - override 应用区（`:1898-1927`）：env_type 改读 override。
  - container_config 构建区（`:2012-2026`）：合并 override 的 network/mount/persist/volumes/cpu/memory。
  - 环境创建调用（`:2036-2046`）：host_cwd 改读 override。
  - `_create_environment` docker 分支（`:1260-1280`）：把 `network`、`mount_credentials/skills/cache` 透传给 `_DockerEnvironment`。
- Test: `tests/tools/test_terminal_owner_override.py`（新建）

**Interfaces:**
- Consumes: `resolve_task_overrides(task_id)`（`:1037`）、T3 的 DockerEnvironment 三开关。
- Produces: terminal 在有 owner override 时，最终 `env_type/host_cwd/docker_mount_cwd_to_workspace/network` 全部来自 override；docker 容器以 `network=False` + 三挂载开关创建。

- [ ] **Step 1: 写失败测试**（验证 override 真正驱动，而非全局 TERMINAL_ENV）

```python
# tests/tools/test_terminal_owner_override.py
from unittest.mock import patch, MagicMock
import tools.terminal_tool as tt


def test_override_drives_env_type_and_network():
    """注册 docker override 后，即使全局 TERMINAL_ENV=local，也应走 docker 且 network 透传。"""
    task_id = "owner-hash-xyz"
    tt.register_task_env_overrides(task_id, {
        "env_type": "docker",
        "docker_image": "img",
        "host_cwd": "/data/workspaces/abc",
        "docker_mount_cwd_to_workspace": True,
        "cwd": "/workspace",
        "network": False,
        "docker_volumes": [],
        "container_persistent": True,
        "docker_persist_across_processes": False,
        "mount_credentials": False, "mount_skills": True, "mount_cache": False,
        "container_cpu": 2, "container_memory": 4096,
    })
    try:
        captured = {}
        def fake_create_environment(**kwargs):
            captured.update(kwargs)
            env = MagicMock()
            env.execute.return_value = {"output": "ok", "exit_code": 0}
            return env
        with patch.dict("os.environ", {"TERMINAL_ENV": "local"}), \
             patch.object(tt, "_create_environment", side_effect=fake_create_environment), \
             patch.object(tt, "_active_environments", {}):
            tt.terminal_tool(command="echo hi", task_id=task_id)
        assert captured["env_type"] == "docker"
        assert captured["host_cwd"] == "/data/workspaces/abc"
        cc = captured["container_config"]
        assert cc["network"] is False
        assert cc["docker_mount_cwd_to_workspace"] is True
        assert cc["mount_credentials"] is False and cc["mount_cache"] is False and cc["mount_skills"] is True
    finally:
        tt.clear_task_env_overrides(task_id)
```

> 这是集成测试，需对齐 `terminal_tool` 的真实执行路径。先通读 `terminal_tool()`（`:1843` 起）确认：①环境创建分支前后哪些值进 `_create_environment`；②`container_config` 在哪个分支构建。`_resolve_container_task_id` 可能把 task_id 折叠成 `"default"`——本任务 override 带了 `env_type` 等隔离键，应按 raw task_id 解析（`resolve_task_overrides` 已 raw 优先）。若测试因执行路径复杂难以驱动，可拆成更小的单测：直接测"override 合并后的 container_config 构建"那段被抽出的纯函数（见 Step 3 建议抽函数）。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_terminal_owner_override.py -v`
Expected: FAIL（当前 env_type 来自全局 config，network 未透传）

- [ ] **Step 3: 最小实现**

(a) override 应用区（`:1899` 之后、`overrides = resolve_task_overrides(...)` 之后）——让 env_type 可被 override 驱动：

```python
overrides = resolve_task_overrides(task_id)
# 多租户 owner override 可改写后端类型（R1#1）
if overrides.get("env_type"):
    env_type = overrides["env_type"]
```

(b) host_cwd 改读 override（`:2045`）：

```python
host_cwd=overrides.get("host_cwd") or config.get("host_cwd"),
```

(c) container_config 构建区（`:2012-2026`）——合并 override（建议抽一个小函数 `_merge_container_config(config, overrides)` 便于单测）：

```python
container_config = {
    "container_cpu": overrides.get("container_cpu", config.get("container_cpu", 1)),
    "container_memory": overrides.get("container_memory", config.get("container_memory", 5120)),
    "container_disk": config.get("container_disk", 51200),
    "container_persistent": overrides.get("container_persistent", config.get("container_persistent", True)),
    "modal_mode": config.get("modal_mode", "auto"),
    "docker_volumes": overrides.get("docker_volumes", config.get("docker_volumes", [])),
    "docker_mount_cwd_to_workspace": overrides.get("docker_mount_cwd_to_workspace",
                                                   config.get("docker_mount_cwd_to_workspace", False)),
    "docker_forward_env": config.get("docker_forward_env", []),
    "docker_env": config.get("docker_env", {}),
    "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
    "docker_extra_args": config.get("docker_extra_args", []),
    "docker_persist_across_processes": overrides.get("docker_persist_across_processes",
                                                     config.get("docker_persist_across_processes", True)),
    "docker_orphan_reaper": config.get("docker_orphan_reaper", True),
    # 新增：网络与挂载开关（R2 连带）
    "network": overrides.get("network", True),
    "mount_credentials": overrides.get("mount_credentials", True),
    "mount_skills": overrides.get("mount_skills", True),
    "mount_cache": overrides.get("mount_cache", True),
}
```

(d) `_create_environment` docker 分支（`:1260-1280`）——把新字段透传给构造函数：

```python
return _DockerEnvironment(
    image=image, cwd=cwd, timeout=timeout,
    cpu=cpu, memory=memory, disk=disk,
    persistent_filesystem=persistent, task_id=task_id,
    volumes=volumes,
    host_cwd=host_cwd,
    auto_mount_cwd=cc.get("docker_mount_cwd_to_workspace", False),
    forward_env=docker_forward_env, env=docker_env,
    run_as_host_user=cc.get("docker_run_as_host_user", False),
    extra_args=docker_extra_args,
    persist_across_processes=cc.get("docker_persist_across_processes", True),
    network=cc.get("network", True),               # R2 连带：透传 network
    mount_credentials=cc.get("mount_credentials", True),
    mount_skills=cc.get("mount_skills", True),
    mount_cache=cc.get("mount_cache", True),
)
```

> 全部改动只在"有 override / docker 分支"生效，单用户（无 override、env_type=local）路径不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_terminal_owner_override.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/terminal_tool.py tests/tools/test_terminal_owner_override.py
git commit -m "feat(terminal): owner override drives env_type/host_cwd/network + passthrough"
```

---

## Task 5: terminal fail-closed 守卫

**对应 spec：** §6.1、§9.4。多租户 + 有 owner 时，最终必须 docker，否则拒绝；绝不退化 local。

**Files:**
- Modify: `tools/terminal_tool.py`（`terminal_tool()` 入口，env_type 确定之后、创建/执行环境之前）
- Test: `tests/tools/test_terminal_fail_closed.py`（新建）

**Interfaces:**
- Consumes: `multi_tenant_enabled()`、`get_current_owner_key()`/`sandbox_enabled()`（T1）。
- Produces: 多租户 sandbox 模式下，若解析出的 `env_type != "docker"` → 返回结构化错误，**不执行**。

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_terminal_fail_closed.py
import json
from unittest.mock import patch
import tools.terminal_tool as tt


def test_multi_tenant_refuses_non_docker():
    with patch("tools.terminal_tool.multi_tenant_enabled", return_value=True), \
         patch("tools.terminal_tool.sandbox_enabled", return_value=True), \
         patch("tools.terminal_tool.get_current_owner_key", return_value="wecom:c:a:u"), \
         patch.dict("os.environ", {"TERMINAL_ENV": "local"}):
        # 没有注册 docker override → env_type 仍是 local → 必须拒绝
        out = json.loads(tt.terminal_tool(command="echo hi", task_id="nope"))
    assert out.get("status") == "error" or out.get("error")
    assert "sandbox" in (out.get("error", "") + out.get("status", "")).lower() \
        or "refus" in out.get("error", "").lower() or "拒绝" in out.get("error", "")
```

> import 路径：`terminal_tool` 里要 `from gateway.multi_tenant import multi_tenant_enabled, sandbox_enabled, get_current_owner_key`（按现有 import 风格，可能是函数内延迟 import 以避免循环依赖——先看文件顶部既有 import 习惯）。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_terminal_fail_closed.py -v`
Expected: FAIL（当前会照常以 local 执行）

- [ ] **Step 3: 最小实现**

**先在 `terminal_tool.py` 顶部加模块级 import（R3#7，让测试能 patch `tools.terminal_tool.*`）：**

```python
# tools/terminal_tool.py 顶部
from gateway.multi_tenant import (
    multi_tenant_enabled, sandbox_enabled, get_current_owner_key,
)
```

> 若存在循环依赖导致顶部 import 失败，则改为函数内 import，并把 **T5/T6/T13 测试里的 patch 目标改成 `gateway.multi_tenant.multi_tenant_enabled` 等**（而非 `tools.terminal_tool.*`）。二选一，全计划保持一致。本计划测试默认 `tools.terminal_tool.*`，故**首选模块级 import**。

在 `terminal_tool()` 里 env_type 最终确定后、创建环境前加守卫：

```python
# fail-closed：多租户 sandbox 模式下必须 docker，绝不退化 local（§6.1）
if multi_tenant_enabled() and sandbox_enabled() and env_type != "docker":
    return json.dumps({
        "error": "refused: multi-tenant sandbox requires a docker environment "
                 "but none was resolved (owner sandbox override missing). "
                 "Execution is blocked to avoid running on the host.",
        "status": "error",
    }, ensure_ascii=False)
```

> 放在 §6.1 要求的"执行入口守卫"位置——确保任何会落到 local 的分支都被它拦在前面。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_terminal_fail_closed.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/terminal_tool.py tests/tools/test_terminal_fail_closed.py
git commit -m "feat(terminal): fail-closed guard for multi-tenant sandbox"
```

---

## Task 6: terminal 禁后台进程

**对应 spec：** §6.2 审查 #5、§9.5。多租户下 `background=True`/`notify_on_complete`/`watch_patterns` 拒绝或降级前台。

**Files:**
- Modify: `tools/terminal_tool.py`（`terminal_tool()` 入口处，参数解析后）
- Test: `tests/tools/test_terminal_no_background_multitenant.py`（新建）

**Interfaces:**
- Produces: 多租户 sandbox 下，`background=True` 返回可执行错误（提示改前台 + 高 timeout），不进 process_registry。

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_terminal_no_background_multitenant.py
import json
from unittest.mock import patch
import tools.terminal_tool as tt


def test_background_rejected_in_multi_tenant():
    with patch("tools.terminal_tool.multi_tenant_enabled", return_value=True), \
         patch("tools.terminal_tool.sandbox_enabled", return_value=True):
        out = json.loads(tt.terminal_tool(command="sleep 1", background=True, task_id="x"))
    assert out.get("error")
    assert "background" in out["error"].lower() or "前台" in out["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_terminal_no_background_multitenant.py -v`
Expected: FAIL

- [ ] **Step 3: 最小实现**

在 `terminal_tool()` 入口（守卫 Task 5 附近）加：

```python
if multi_tenant_enabled() and sandbox_enabled() and (background or notify_on_complete or watch_patterns):
    return json.dumps({
        "error": "background processes are disabled in multi-tenant sandbox mode. "
                 "Run in the foreground with a higher timeout instead "
                 "(commands return as soon as they finish).",
        "status": "error",
    }, ensure_ascii=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_terminal_no_background_multitenant.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/terminal_tool.py tests/tools/test_terminal_no_background_multitenant.py
git commit -m "feat(terminal): disable background processes in multi-tenant sandbox"
```

---

## Task 7: file 工具 /workspace 别名归一化 + 单点校验

**对应 spec：** §5.5.3、§9.8c(b)。让 file 工具把 `/workspace` 当 `owner_workspace_root` 别名，归一化后单点 `validate_within_dir`。

**Files:**
- Modify: `tools/file_tools.py`（`_resolve_path_for_task` 约 `:259-276`，及/或 `_multi_tenant_workspace_root` 附近加归一化 helper）
- Test: `tests/tools/test_file_tools_workspace_alias.py`（新建）

**Interfaces:**
- Consumes: `_multi_tenant_workspace_root()`（`:279`）、`tools.path_security.validate_within_dir`。
- Produces: `_normalize_owner_path(filepath: str, owner_root: Path) -> Path`（去 `/workspace` 前缀→拼 owner_root；相对→owner_root；绝对→原样）；`_resolve_path_for_task` 在多租户分支调用它。

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_file_tools_workspace_alias.py
from pathlib import Path
from unittest.mock import patch
import tools.file_tools as ft


def test_workspace_alias_maps_to_owner_root(tmp_path):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        p = ft._resolve_path_for_task("/workspace/uploads/x.xlsx")
    assert p == (owner_root / "uploads/x.xlsx").resolve()


def test_relative_still_maps_to_owner_root(tmp_path):
    owner_root = tmp_path / "owner"; owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        p = ft._resolve_path_for_task("report.csv")
    assert p == (owner_root / "report.csv").resolve()


def test_workspace_alias_traversal_rejected(tmp_path):
    """/workspace/../../etc/passwd 归一化后越界，validate 必须判错。"""
    owner_root = tmp_path / "owner"; owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        target = ft._resolve_path_for_task("/workspace/../../etc/passwd")
        err = ft._validate_multi_tenant_workspace_path(target)
    assert err is not None  # 越界被拒
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_workspace_alias.py -v`
Expected: FAIL（当前 `/workspace/...` 被当宿主机绝对路径，不映射到 owner_root）

- [ ] **Step 3: 最小实现**

在 `_resolve_path_for_task`（`:259`）多租户分支最前面加 `/workspace` 别名（确定性归一化，不 try-fail-guess）：

```python
def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path:
    p = Path(_expand_tilde(filepath))
    multi_tenant_root = _multi_tenant_workspace_root()
    if multi_tenant_root is not None:
        raw = str(p)
        # /workspace 别名 → owner_root（容器视角统一到宿主机视角）
        if raw == "/workspace" or raw.startswith("/workspace/"):
            rel = raw[len("/workspace"):].lstrip("/")
            return (multi_tenant_root / rel).resolve()
        if p.is_absolute():
            return p.resolve()
        return (multi_tenant_root / p).resolve()
    if p.is_absolute():
        return p.resolve()
    return (_resolve_base_dir(task_id) / p).resolve()
```

> `_validate_multi_tenant_workspace_path`（`:295`）保持不变——它仍是归一化之后的**单一硬校验**，`/workspace/../../etc/passwd` 归一化成 `owner_root/../../etc/passwd` → resolve 越界 → 它判错。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_workspace_alias.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/file_tools.py tests/tools/test_file_tools_workspace_alias.py
git commit -m "feat(file-tools): /workspace alias maps to owner workspace root"
```

---

## Task 8: file 工具多租户下不创建 docker 容器（解耦 auto-docker）

**对应 spec：** §5.5.2、§9.8c(a)；审查 R3#5。A 方案：多租户下 file 工具走进程内。**目标明确为"不创建 docker 容器"**（进程内读写 owner_root 即可），而非"完全不调任何环境抽象"。

**Files:**
- Modify: `tools/file_tools.py`（`_get_file_ops`，约 `:785-900`——它内部 `from tools.terminal_tool import _create_environment` 并按 `env_type` 决定是否建 docker 环境）
- Test: `tests/tools/test_file_tools_inprocess_multitenant.py`（新建）

**Interfaces:**
- Produces: 多租户（`_multi_tenant_workspace_root() is not None`）时，`_get_file_ops` 直接返回**进程内** file ops（基于 `LocalEnvironment(cwd=owner_root)`），**不进入 docker 创建分支**（不调 `tools.terminal_tool._create_environment`）。读写经 Task 7 归一化 + 校验。

> **真实入口与 patch 目标（R3#5）**：模型侧工具是 `read_file_tool()`（`file_tools.py:1069`），它走 `_get_file_ops(task_id)` 拿 file_ops；`_create_environment` 是在 `_get_file_ops` 内**从 `tools.terminal_tool` 局部 import** 的（`file_tools.py:791`）——所以测试要 patch **`tools.terminal_tool._create_environment`**（不是 `tools.file_tools._create_environment`，后者不存在）。

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_file_tools_inprocess_multitenant.py
from unittest.mock import patch
import tools.file_tools as ft


def test_read_file_does_not_create_docker_even_when_env_type_docker(tmp_path):
    """R3#5：即使 env_type=docker，多租户下 read_file_tool 也不应创建 docker 容器。"""
    owner_root = tmp_path / "owner"; owner_root.mkdir()
    (owner_root / "a.txt").write_text("hello", encoding="utf-8")
    def fake_create_environment(*a, **k):
        raise AssertionError("should not create docker container in multi-tenant A-mode")
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root), \
         patch.dict("os.environ", {"TERMINAL_ENV": "docker"}), \
         patch("tools.terminal_tool._create_environment", side_effect=fake_create_environment):
        result = ft.read_file_tool("/workspace/a.txt")   # 真实入口（file_tools.py:1069）
    assert "hello" in str(result)   # 进程内读到内容，且未触发 _create_environment（否则上面会抛）
```

> `read_file_tool` 的确切签名以源码为准；patch 目标必须是 `tools.terminal_tool._create_environment`（局部 import 的源模块）。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_inprocess_multitenant.py -v`
Expected: FAIL（当前 env_type=docker → `_get_file_ops` 会建 docker 容器 → fake 抛 AssertionError）

- [ ] **Step 3: 最小实现**

在 `_get_file_ops`（`file_tools.py:785` 起）**最前面**加多租户短路：拿到 owner_root 就直接构造进程内 file ops，**根本不进 docker 创建分支**（不 import/调 `_create_environment`）：

```python
def _get_file_ops(task_id: str = "default"):
    # 多租户 A 方案：直接进程内，不建 docker 容器（R3#5）
    _mt_root = _multi_tenant_workspace_root()
    if _mt_root is not None:
        from tools.environments.local import LocalEnvironment
        from tools.file_operations import ShellFileOperations   # 类名以源码为准
        return ShellFileOperations(LocalEnvironment(cwd=str(_mt_root)))
    # —— 以下为原有逻辑（单用户/其它平台）：按 env_type 走 docker/local —— 
    from tools.terminal_tool import (_active_environments, _env_lock, _create_environment, ...)
    ...
```

> **类名/构造以源码为准**：照搬现有"local/进程内"路径构造 file_ops 的方式（看 `_get_file_ops` 里 `env_type=="local"` 分支怎么造），只把 cwd 固定成 owner_root。关键是**多租户分支在 `_create_environment` 之前 return**，确保不建 docker。每命令重新 `_get_file_ops` 开销可忽略（进程内、无容器）。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_inprocess_multitenant.py -v`
Expected: PASS

- [ ] **Step 5: 回归：确认 Task 7 测试仍过**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_workspace_alias.py tests/tools/test_file_tools_inprocess_multitenant.py -v`
Expected: PASS

- [ ] **Step 6: 提交（由你执行）**

```bash
git add tools/file_tools.py tests/tools/test_file_tools_inprocess_multitenant.py
git commit -m "feat(file-tools): keep file tools in-process under multi-tenant (option A)"
```

---

## Task 9: wecom_multi_tenant_sandbox 工具集

**对应 spec：** §9.1、审查 R1#3。新增含 terminal 的工具集，纯受限集保持不变。

**Files:**
- Modify: `toolsets.py`（`wecom_multi_tenant` 定义附近）
- Test: `tests/test_toolsets_wecom_sandbox.py`（新建）

**Interfaces:**
- Produces: `TOOLSETS["wecom_multi_tenant_sandbox"]` = `wecom_multi_tenant` 全部工具 + `"terminal"`；`wecom_multi_tenant` 不变（无 terminal/process）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_toolsets_wecom_sandbox.py
from toolsets import resolve_toolset


def test_sandbox_toolset_adds_terminal():
    base = set(resolve_toolset("wecom_multi_tenant"))
    sandbox = set(resolve_toolset("wecom_multi_tenant_sandbox"))
    assert "terminal" not in base                 # 纯受限集不变
    assert "terminal" in sandbox                  # sandbox 集含 terminal
    assert base.issubset(sandbox)                 # sandbox = base + terminal
    assert "process" not in sandbox               # process 一期不开
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/test_toolsets_wecom_sandbox.py -v`
Expected: FAIL（无该 toolset）

- [ ] **Step 3: 最小实现**

在 `toolsets.py` 的 `TOOLSETS` 里，紧跟 `wecom_multi_tenant` 后新增：

```python
"wecom_multi_tenant_sandbox": {
    "description": (
        "WeCom multi-tenant sandbox toolset: 受限集 + terminal（owner 专属 docker 沙箱执行）"
    ),
    "tools": [
        "web_search", "web_extract",
        "read_file", "write_file", "patch", "search_files",
        "skills_list", "skill_view",
        "todo", "memory", "session_search", "clarify",
        "terminal",
    ],
    "includes": [],
},
```

> 若想避免重复列举，可用 `"includes": ["wecom_multi_tenant"], "tools": ["terminal"]`——但需确认 `resolve_toolset` 的合并语义（读 `toolsets.py` 顶部 resolve 实现）。两种皆可，优先与现有写法一致、最小惊讶。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/test_toolsets_wecom_sandbox.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add toolsets.py tests/test_toolsets_wecom_sandbox.py
git commit -m "feat(toolsets): add wecom_multi_tenant_sandbox (restricted + terminal)"
```

---

## Task 10: constrain_toolsets_for_owner 按 sandbox.enabled 选择

**对应 spec：** §9.2、审查 R1#3。开关决定回纯受限集还是 sandbox 集。

**Files:**
- Modify: `gateway/multi_tenant.py`（`constrain_toolsets_for_owner` `:227-247`）
- Test: `tests/gateway/test_constrain_toolsets_sandbox.py`（新建）

**Interfaces:**
- Consumes: `sandbox_enabled()`（T1）、`_WECOM_MULTI_TENANT_TOOLSET`（`:62`）、新增 `_WECOM_MULTI_TENANT_SANDBOX_TOOLSET`。
- Produces: owner 请求时，`sandbox_enabled()` → 返回 `[sandbox toolset]`，否则 `[纯受限 toolset]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/gateway/test_constrain_toolsets_sandbox.py
from unittest.mock import patch
import gateway.multi_tenant as mt


def test_constrain_returns_sandbox_when_enabled():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_enabled", return_value=True):
        enabled, disabled = mt.constrain_toolsets_for_owner(None, None, owner_key="wecom:c:a:u")
    assert enabled == ["wecom_multi_tenant_sandbox"]


def test_constrain_returns_restricted_when_sandbox_off():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_enabled", return_value=False):
        enabled, _ = mt.constrain_toolsets_for_owner(None, None, owner_key="wecom:c:a:u")
    assert enabled == ["wecom_multi_tenant"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/gateway/test_constrain_toolsets_sandbox.py -v`
Expected: FAIL（当前恒返回纯受限集）

- [ ] **Step 3: 最小实现**

```python
# 模块顶部常量区（紧邻 :62）
_WECOM_MULTI_TENANT_SANDBOX_TOOLSET = "wecom_multi_tenant_sandbox"

# constrain_toolsets_for_owner 内
if multi_tenant_enabled() and owner_key:
    if sandbox_enabled():
        return [_WECOM_MULTI_TENANT_SANDBOX_TOOLSET], None
    return [_WECOM_MULTI_TENANT_TOOLSET], None
return enabled_toolsets, disabled_toolsets
```

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/gateway/test_constrain_toolsets_sandbox.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add gateway/multi_tenant.py tests/gateway/test_constrain_toolsets_sandbox.py
git commit -m "feat(multi-tenant): constrain_toolsets selects sandbox toolset by config"
```

---

## Task 11: 请求处注册/清理 owner 沙箱 override（按 session_id 对齐）

**对应 spec：** §5.2、§5.3、§9.8；审查 R3#1。**override 必须注册在与 terminal/file 工具运行时收到的 `task_id` 相同的键下**——主流程传的是 `task_id=session_id`（`run.py:16094`），所以注册键 = **session_id**，不是 `hash_owner_key`。

**Files:**
- Modify: `gateway/run.py`（注册：`run_conversation` 调用处 `:16093` 附近，`task_id=session_id` 在作用域内；清理：该 conversation 调用返回后）
- Test: `tests/gateway/test_owner_override_registration.py`（新建）

**Interfaces:**
- Consumes: `build_owner_sandbox_overrides(owner_key)`（T2）、`sandbox_enabled()`（T1）、`register_task_env_overrides`/`clear_task_env_overrides`（`tools/terminal_tool.py`）。
- Produces: 多租户 sandbox + 有 owner 时，按 **`task_id=session_id`** 注册 override；conversation 结束清理。容器因此 per-session；workspace 仍按 owner 共享（override 里的 `host_cwd` 来自 owner）。

> **为什么是 session_id 不是 hash_owner_key（R3#1）**：`task_id` 在本项目还键控缓存的 AIAgent 实例 / 沙箱 / bg 进程（`run.py:13900/13981`）。改 task_id=owner hash 会让同 owner 多 session 共用一个 agent 实例，违背"task_id=一个对话"假设。注册在 session_id 下既闭环又零额外风险；运营上"一用户一时刻一 session"使其实践等于 per-owner。
> **老容器回收**：`/new` 会硬销毁老 agent（`_cleanup_agent_resources`），老 session 容器随之走清理 / 300s 空闲回收——符合"老的杀掉"预期。

- [ ] **Step 1: 写失败测试**（轻量：验证入口逻辑会注册）

```python
# tests/gateway/test_owner_override_registration.py
from pathlib import Path
from unittest.mock import patch
import gateway.multi_tenant as mt
import tools.terminal_tool as tt


def test_override_registered_under_session_id_resolves(tmp_path):
    """契约：用 session_id 注册 override 后，terminal 用同一 session_id 能 resolve 到。"""
    session_id = "sess-123"
    with patch.object(mt, "sandbox_config", return_value={"image": "img"}), \
         patch.object(mt, "owner_workspace_root", return_value=tmp_path / "ws"):
        ov = mt.build_owner_sandbox_overrides("wecom:c:a:u")
    tt.register_task_env_overrides(session_id, ov)
    try:
        resolved = tt.resolve_task_overrides(session_id)
        assert resolved.get("env_type") == "docker"
        assert resolved.get("network") is False
    finally:
        tt.clear_task_env_overrides(session_id)
```

> `gateway/run.py` 的入口接线集成在大流程里，纯单测难以无副作用驱动；端到端验证放在 T16。此处单测锁定核心契约：**用 session_id 注册 → terminal 用 session_id resolve 得到**（即键对齐，R3#1 的回归保护）。

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `scripts/run_tests.sh tests/gateway/test_owner_override_registration.py -v`
Expected: PASS（依赖 T2 已实现；若 T2 未做会 FAIL）

- [ ] **Step 3: 实现入口接线（注册键 = session_id）**

在 `gateway/run.py:16093` 附近——`_conversation_kwargs = {"conversation_history": ..., "task_id": session_id}` 构建处、**调用 `run_conversation` 之前**——注册 override（键用同一个 `session_id`）：

```python
# 多租户 sandbox：为本次对话注册 owner 专属 docker 沙箱 override
# 注册键 = session_id（与传给 run_conversation 的 task_id 一致，R3#1）
_sandbox_registered = False
try:
    from gateway.multi_tenant import sandbox_enabled, build_owner_sandbox_overrides, get_current_owner_key
    from tools.terminal_tool import register_task_env_overrides
    _ok = get_current_owner_key()  # 当前 owner（ContextVar，已在上游 set）
    if _ok and sandbox_enabled():
        register_task_env_overrides(session_id, build_owner_sandbox_overrides(_ok))
        _sandbox_registered = True
except Exception:
    logger.exception("failed to register owner sandbox overrides")  # 工具侧 fail-closed 会兜
```

在该 conversation 调用返回后（同一作用域的 finally / 收尾处）清理：

```python
if _sandbox_registered:
    try:
        from tools.terminal_tool import clear_task_env_overrides
        clear_task_env_overrides(session_id)
    except Exception:
        logger.exception("failed to clear owner sandbox overrides")
```

> **键必须是 session_id**：terminal/file 工具运行时拿到的就是这个 session_id（`task_id=session_id`），只有同键注册 `resolve_task_overrides` 才读得到（R3#1）。`get_current_owner_key()` 取当前 owner（上游 `set_current_owner_key` 已注入 ContextVar）。
> **实施前确认**：`session_id` 与 `get_current_owner_key()` 在该作用域均可用；若注册点与 `task_id=session_id` 构建点不在同一函数，挪到能同时拿到二者的位置。

- [ ] **Step 4: 跑相关测试**

Run: `scripts/run_tests.sh tests/gateway/ -v`
Expected: PASS（无回归）

- [ ] **Step 5: 提交（由你执行）**

```bash
git add gateway/run.py tests/gateway/test_owner_override_registration.py
git commit -m "feat(gateway): register/clear owner sandbox overrides at request entry"
```

---

## Task 12: 上传文件路径**呈现给模型时**转 /workspace/...（不改内部路径）

**对应 spec：** §5.5.3、§9.8b、已决 #5；审查 R3#3。模型看到的上传文件路径是 `/workspace/uploads/...`，但**网关内部仍用宿主机真实路径**（图片/文档内部处理还要读它）。

**Files:**
- Modify: `gateway/run.py`（DOCUMENT 上下文 note 构建处 `:8734` 附近——已有 `to_agent_visible_cache_path` 这个"宿主机路径→模型可见路径"接缝，多租户分支扩展它）
- **不改** `plugins/platforms/wecom/adapter.py` 的 `_cache_owner_upload` 返回值（内部仍是宿主机真实路径，R3#3）
- Test: `tests/gateway/test_upload_path_presentation.py`（新建）

**Interfaces:**
- Consumes: `owner_workspace_root(owner_key)`、现有 `to_agent_visible_cache_path`。
- Produces: `_to_workspace_view(host_path: str, owner_root: Path) -> str | None`——在 owner_root 内则返回 `/workspace/<rel>`；**越界返回 None（fail-closed，绝不回传宿主机路径，防泄漏）**。仅在"展示给模型"处用它转换，内部路径不变。

- [ ] **Step 1: 写失败测试**

```python
# tests/gateway/test_upload_path_presentation.py
from pathlib import Path
from gateway.run import _to_workspace_view   # Step 3 在 run.py 定义


def test_host_upload_path_becomes_workspace_view():
    owner_root = Path("/data/workspaces/h")
    host = "/data/workspaces/h/uploads/abc/报表.xlsx"
    assert _to_workspace_view(host, owner_root) == "/workspace/uploads/abc/报表.xlsx"


def test_out_of_bounds_path_fails_closed():
    """R3#3：越界路径不得原样回传宿主机路径，必须 None（fail-closed）。"""
    owner_root = Path("/data/workspaces/h")
    assert _to_workspace_view("/etc/passwd", owner_root) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/gateway/test_upload_path_presentation.py -v`
Expected: FAIL（无 `_to_workspace_view`）

- [ ] **Step 3: 最小实现（在 run.py，fail-closed）**

```python
def _to_workspace_view(host_path: str, owner_root) -> "str | None":
    """owner_root 下的宿主机绝对路径 → 模型可见的 /workspace 视图；越界返回 None。"""
    from pathlib import Path as _P
    try:
        rel = _P(host_path).resolve().relative_to(_P(owner_root).resolve())
    except ValueError:
        return None  # 越界：fail-closed，绝不回传宿主机路径（R3#3）
    return "/workspace/" + str(rel)
```

在 `gateway/run.py:8734` 的 DOCUMENT 上下文 note 构建处，多租户分支用 `_to_workspace_view(path, owner_workspace_root(get_current_owner_key()))` 得到呈现给模型的路径。

> **fail-closed（R3#2 强化）**：返回 None 时**直接跳过该条 note**（或写一句不含路径的通用说明，如"用户上传了一个文件，在你的工作区 uploads 目录下"）。**绝不 fallback 到 `to_agent_visible_cache_path`**——它在非 docker / 未命中 cache 挂载时会**原样返回宿主机路径**（`credential_files.py:403` docstring 明示），fallback 即泄漏。

**内部 `event.media_urls` 等不改**——只改"拼给模型看的文本"。仅 `multi_tenant_enabled()` 时启用；单用户不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/gateway/test_upload_path_presentation.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add gateway/run.py tests/gateway/test_upload_path_presentation.py
git commit -m "feat(gateway): present uploaded file paths to model as /workspace view (fail-closed)"
```

---

## Task 13: 并发信号量

**对应 spec：** §6.2、§8（`max_concurrent=24`）、§9.12。限制同时执行的 owner 容器命令数。

**Files:**
- Modify: `tools/terminal_tool.py`（在 docker 容器执行命令处包信号量）
- Test: `tests/tools/test_sandbox_concurrency_semaphore.py`（新建）

**Interfaces:**
- Produces: 模块级 `_sandbox_semaphore`（容量来自 `sandbox_config().get("max_concurrent", 24)`，惰性初始化），多租户 docker 执行命令时 acquire/release。

- [ ] **Step 1: 写失败测试**（验证容量从配置读取、acquire 包裹执行）

```python
# tests/tools/test_sandbox_concurrency_semaphore.py
from unittest.mock import patch
import tools.terminal_tool as tt


def test_semaphore_capacity_from_config():
    with patch("tools.terminal_tool.sandbox_config", return_value={"max_concurrent": 3}):
        sem = tt._get_sandbox_semaphore(force_fresh=True)
    assert sem._value == 3   # threading.Semaphore 初始计数


def test_semaphore_wraps_execution(monkeypatch):
    """R3#6 行为测试：执行命令前 acquire、命令结束后 release（用伪 semaphore 记录）。"""
    events = []
    class FakeSem:
        def __enter__(self): events.append("acquire"); return self
        def __exit__(self, *a): events.append("release")
    monkeypatch.setattr(tt, "_get_sandbox_semaphore", lambda *a, **k: FakeSem())

    # 伪造一个 docker env，execute 时记录顺序
    from unittest.mock import MagicMock, patch
    fake_env = MagicMock()
    def fake_execute(*a, **k):
        events.append("execute")
        return {"output": "ok", "exit_code": 0}
    fake_env.execute.side_effect = fake_execute

    task_id = "sess-sem"
    tt.register_task_env_overrides(task_id, {
        "env_type": "docker", "docker_image": "img", "host_cwd": "/data/workspaces/h",
        "docker_mount_cwd_to_workspace": True, "cwd": "/workspace", "network": False,
        "container_persistent": True, "docker_persist_across_processes": False,
        "mount_credentials": False, "mount_skills": True, "mount_cache": False,
    })
    try:
        with patch.object(tt, "multi_tenant_enabled", return_value=True), \
             patch.object(tt, "sandbox_enabled", return_value=True), \
             patch.object(tt, "_create_environment", return_value=fake_env), \
             patch.object(tt, "_active_environments", {}):
            tt.terminal_tool(command="echo hi", task_id=task_id)
    finally:
        tt.clear_task_env_overrides(task_id)
    # acquire 在 execute 前，release 在 execute 后
    assert events.index("acquire") < events.index("execute") < events.index("release")
```

> 第二个测试依赖 T4/T5 已落地（多租户 docker 执行路径打通）。若 `multi_tenant_enabled`/`sandbox_enabled` 在 terminal_tool 里是函数内 import，patch 目标改成 `gateway.multi_tenant.*`（见 T5/T6 的 R3#7 说明）。

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/tools/test_sandbox_concurrency_semaphore.py -v`
Expected: FAIL

- [ ] **Step 3: 最小实现**

```python
import threading
_sandbox_semaphore = None
_sandbox_semaphore_lock = threading.Lock()

def _get_sandbox_semaphore(force_fresh: bool = False):
    global _sandbox_semaphore
    if force_fresh or _sandbox_semaphore is None:
        with _sandbox_semaphore_lock:
            if force_fresh or _sandbox_semaphore is None:
                from gateway.multi_tenant import sandbox_config
                cap = int(sandbox_config().get("max_concurrent", 24))
                _sandbox_semaphore = threading.Semaphore(max(1, cap))
    return _sandbox_semaphore
```

在多租户 docker 执行命令处（`env.execute(...)` 调用外）：

```python
if multi_tenant_enabled() and sandbox_enabled() and env_type == "docker":
    with _get_sandbox_semaphore():
        result = env.execute(command, cwd=effective_cwd, ...)
else:
    result = env.execute(command, cwd=effective_cwd, ...)
```

> 信号量只包"执行命令"这一段（不包建容器/空闲），以"同时执行数"封顶峰值，符合 §8 推算。

- [ ] **Step 4: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/tools/test_sandbox_concurrency_semaphore.py -v`
Expected: PASS

- [ ] **Step 5: 提交（由你执行）**

```bash
git add tools/terminal_tool.py tests/tools/test_sandbox_concurrency_semaphore.py
git commit -m "feat(terminal): global concurrency semaphore for sandbox exec"
```

---

## Task 14: system prompt 能力清单（治幻觉）

**对应 spec：** §7、§9.10。多租户 sandbox 分支加静态能力清单 + 诚实锚点（cache 友好）。

**Files:**
- Modify: `agent/prompt_builder.py`（放固定文本函数 `build_capability_manifest()`）
- Modify: `agent/system_prompt.py`（**真正的组装入口** `build_system_prompt_parts`，`:113`——在 **stable 段**注入，审查 R3#4）
- Test: `tests/agent/test_capability_manifest.py`（新建）

**Interfaces:**
- Produces: 当启用 `wecom_multi_tenant_sandbox` 工具集时，最终组装出的 system prompt（stable 段）含固定能力清单文本（"shell 在隔离沙箱、只挂你的工作区、默认无网络、schema 没有的工具就是没有"）。stable 段保证会话内静态、cache 友好。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_capability_manifest.py
from agent.prompt_builder import build_capability_manifest  # Step 3 新增


def test_manifest_states_sandbox_boundaries():
    text = build_capability_manifest()
    assert "/workspace" in text or "工作区" in text
    assert "网络" in text or "network" in text.lower()
    # 诚实锚点
    assert "schema" in text.lower() or "不要声称" in text


def test_manifest_is_static():
    assert build_capability_manifest() == build_capability_manifest()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `scripts/run_tests.sh tests/agent/test_capability_manifest.py -v`
Expected: FAIL

- [ ] **Step 3: 最小实现**

在 `agent/prompt_builder.py` 加一个返回固定字符串的函数，并在多租户 sandbox 分支拼进 system prompt（保持会话内静态，不破缓存）：

```python
def build_capability_manifest() -> str:
    return (
        "# 你的执行环境（多租户沙箱）\n"
        "- 你的 shell 运行在一个隔离沙箱里，只挂载了当前用户的工作区 /workspace；"
        "你无法访问宿主机或其他用户的数据。\n"
        "- 沙箱默认无网络。需要联网检索请用 web_search / web_extract 工具。\n"
        "- 用户上传的文件在 /workspace/uploads/ 下。\n"
        "- 你的能力仅限工具 schema 中列出的那些；schema 里没有的能力你就是没有，"
        "不要声称或暗示你拥有它。不确定时先检查你的工具，不要假设。\n"
    )
```

> **注入点（R3#4）**：真正组装在 `agent/system_prompt.py:113 build_system_prompt_parts`，返回含 `stable` 段的 dict。把 manifest 拼进 **stable 段**（识别"当前启用 `wecom_multi_tenant_sandbox` 工具集"的分支——读该函数如何拿到 toolset 信息）。stable 段会话内固定 → 满足 prompt 缓存红线。

- [ ] **Step 4: 补一个"最终 prompt 真含该文本"的集成断言**

```python
# 追加到 tests/agent/test_capability_manifest.py
from unittest.mock import MagicMock
from agent.system_prompt import build_system_prompt_parts

def test_manifest_present_in_assembled_prompt_under_sandbox_toolset():
    agent = MagicMock()
    # 按 build_system_prompt_parts 读取 toolset 的实际方式设置 agent（以源码为准）
    # 目标：让它识别到启用了 wecom_multi_tenant_sandbox
    parts = build_system_prompt_parts(agent)
    assert "/workspace" in parts["stable"] or "工作区" in parts["stable"]
```

> 该集成断言需对齐 `build_system_prompt_parts` 读取 toolset 的真实途径（先读 `:113` 起的实现），把 agent mock 配成"启用 sandbox toolset"。这是 R3#4 要求的"验证最终 prompt 真含文本"。

- [ ] **Step 5: 跑测试确认通过**

Run: `scripts/run_tests.sh tests/agent/test_capability_manifest.py -v`
Expected: PASS

- [ ] **Step 6: 提交（由你执行）**

```bash
git add agent/prompt_builder.py agent/system_prompt.py tests/agent/test_capability_manifest.py
git commit -m "feat(prompt): capability manifest injected in system prompt stable part"
```

---

## Task 15: 沙箱镜像 Dockerfile

**对应 spec：** §8、§13 已决 #2。预装全部运行期依赖（因默认禁网）。

**Files:**
- Create: `docker/sandbox/Dockerfile`（路径与仓库现有 docker 资源约定对齐；若无则新建该目录）
- Create: `docker/sandbox/README.md`（构建与配置说明）

**Interfaces:**
- Produces: 一个可构建的镜像，含 python + pandas/numpy/openpyxl、python-docx/python-pptx/reportlab、libreoffice、matplotlib、pdfplumber/PyPDF2、Pillow；镜像名写入 `security.multi_tenant.sandbox.image`。

- [ ] **Step 1: 写 Dockerfile**

```dockerfile
# docker/sandbox/Dockerfile
FROM nikolaik/python-nodejs:python3.11-nodejs20

# libreoffice 用于文档格式互转（headless）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
    && rm -rf /var/lib/apt/lists/*

# 数据处理 / 文档生成 / PDF / 图像（默认禁网，必须预装齐全）
RUN pip install --no-cache-dir \
        pandas numpy openpyxl \
        python-docx python-pptx reportlab \
        matplotlib \
        pdfplumber PyPDF2 Pillow

WORKDIR /workspace
```

- [ ] **Step 2: 构建验证（由你在服务器执行）**

```bash
docker build -t hermes-sandbox:latest docker/sandbox/
docker run --rm --network=none hermes-sandbox:latest \
  python3 -c "import pandas, docx, pptx, reportlab, matplotlib, pdfplumber, PyPDF2, PIL; print('deps ok')"
```
Expected: 打印 `deps ok`（确认禁网下依赖齐全可用）。

- [ ] **Step 3: 写 README（构建命令 + 把镜像名填进 config.yaml `security.multi_tenant.sandbox.image`）**

- [ ] **Step 4: 提交（由你执行）**

```bash
git add docker/sandbox/Dockerfile docker/sandbox/README.md
git commit -m "feat(sandbox): add sandbox docker image with preinstalled deps"
```

---

## Task 15b: 部署配置 — session 永久打开（非代码，配置项）

**对应 spec：** §8.1。要求 session 不自动重置，只有手动 `/new` 才结束。

**Files:**
- Modify: 部署用 `config.yaml`（网关配置，非源码）

- [ ] **Step 1: 设置重置策略为 none**

```yaml
default_reset_policy:
  mode: none        # 永不自动重置；仅 /new、/reset 触发
```

- [ ] **Step 2: 验证（启动后手动）**

启动网关，开一个会话、闲置超过默认 24h 边界 / 跨过凌晨 4 点后再发消息，确认**上下文未被重置**（沿用同一 session）。或读 `SessionResetPolicy`（`gateway/config.py:275`）确认 `mode="none"` 时 `_is_session_expired` 恒 False。

> 这是 config.yaml 配置项、非代码改动，无单测。注意它**只影响 session 生命周期**，不影响容器 ~300s 回收、不影响 workspace 持久（三时钟独立，见 spec §5.5/§8.1）。副作用：历史会累积到 `/new`，靠运营"做完即 /new + 清理"控制（spec §13 #11/#12）。

---

## Task 16: 端到端隔离 + fail-closed + 互通 验证（集成）

**对应 spec：** §10 全量验证清单。前置任务都完成后做。

**Files:**
- Test: `tests/gateway/test_sandbox_e2e.py`（新建；需要 docker 可用，标记 `@pytest.mark.docker` 或在 CI/服务器跑）

**说明：** 这些是跨模块集成断言，部分需真实 docker。无 docker 的环境跳过（`pytest.importorskip`/skip mark），在服务器手动跑。

- [ ] **Step 1: 隔离断言**（两个 owner 在沙箱内写文件，互相读不到）
- [ ] **Step 2: fail-closed**（多租户 + 制造 owner 缺失/override 缺失 → terminal 拒绝，无宿主机副作用）
- [ ] **Step 3: 路径互通**（terminal 在容器写 `/workspace/out.csv` → `read_file("/workspace/out.csv")` 读到同一文件）
- [ ] **Step 4: 越界拒绝**（`read_file("/workspace/../../etc/passwd")` 被拒）
- [ ] **Step 5: 禁网**（容器内访问外网失败）
- [ ] **Step 6: 自动挂载**（`docker run` 参数无 credential、无全局 cache；有 skills RO）
- [ ] **Step 7: toolset 切换**（`sandbox.enabled` true/false → owner 拿到不同 toolset）
- [ ] **Step 8: 单用户回归**（`enabled=false` 时 terminal 行为与改造前一致）
- [ ] **Step 9: 顺序保证**（同一回复含 terminal + read_file → 串行、先跑后读；见 §5.5.4，确认无需改动即满足）
- [ ] **Step 10: 端到端冒烟**（手动）：企业微信发 Excel → 智能体在沙箱用 pandas + python-docx 生成 Word 报告

- [ ] **Step 11: 提交（由你执行）**

```bash
git add tests/gateway/test_sandbox_e2e.py
git commit -m "test(sandbox): end-to-end isolation/fail-closed/interop checks"
```

---

## 实施顺序建议与检查点

1. **Phase 0（T1–T2）** → 一次提交一组，纯函数易测。
2. **Phase 1（T3–T6）** → T4 完成后**立即跑一次 T4 + T5 的定向测试**确认 override 真正生效（P0 命门）。
3. **Phase 2（T7–T8）** → 两个 file 测试一起回归。
4. **Phase 3（T9–T12）** → T11 入口接线后，关注 task_id 对齐（任务内已标注）。
5. **Phase 4（T13–T15）**。
6. **T16 集成验证** → 在能跑 docker 的环境（服务器）逐条过 §10。

**每个任务的验证只用** `scripts/run_tests.sh <该任务测试路径>`。全部任务做完后，**不**声称"整体验收完成"，而是按 T16 在服务器逐条过端到端，再下结论。

## 自检（spec 覆盖）

- §5.1/§5.2 override 注入 → T2、T4、T11 ✓
- §5.3 容器 per-session（override 注册在 session_id）、workspace per-owner、跨进程复用关 → T2、T11 ✓
- §5.4 生命周期（复用现有，无需改） → 无代码任务，T16 Step9 验证 ✓
- §5.5.2 A 方案 file 进程内 → T8 ✓
- §5.5.3 路径别名 → T7、T12 ✓
- §5.5.4 顺序保证（无需改） → T16 Step9 ✓
- §6.1 fail-closed → T5（terminal）、T7/T8（file 经 validate） ✓
- §6.2 加固：禁网 T4、挂载开关 T3、禁后台 T6、并发 T13 ✓
- §7 能力清单 → T14 ✓
- §8 配置 → T1、T2、T13、T15 ✓
- §9 改动清单 12+2 项 → T3–T15 全覆盖 ✓
- §10 测试 → 各任务定向测试 + T16 集成 ✓
