"""T3：多租户 sandbox 下 execute_code 按 session override 落 owner 容器 + fail-closed。

覆盖：
- 多租户 sandbox 但无 owner docker override 时，execute_code 必须拒绝
  （不落回主机/全局 docker，避免隔离泄漏）。
- `generate_hermes_tools_module` 只按传入的 enabled_tools 生成子工具 stub——
  调用方传入不含 web 的 owner 13 工具 allowlist 时，web_search/web_extract
  不应出现在生成的沙箱模块源码里。

对应 `.superpowers/sdd/task-3-brief.md`。
"""
import json

import tools.code_execution_tool as ce


def test_no_owner_override_refuses(monkeypatch):
    # 真实入口是 ce.execute_code（注册 handler 是 lambda args,**kw: execute_code(...)）
    # P4：patch 原始模块属性
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.sandbox_enabled", lambda: True)
    monkeypatch.setattr("tools.terminal_tool.resolve_task_overrides", lambda tid: {})
    out = ce.execute_code(code="print(1)", task_id="sess-1")
    assert json.loads(out)["status"] == "error"


def test_subtools_exclude_web():
    # 真实签名：generate_hermes_tools_module(enabled_tools, transport="uds")
    from tools.code_execution_tool import generate_hermes_tools_module
    owner_allow = ["read_file", "write_file", "terminal"]  # 13 集的子集，无 web
    module = generate_hermes_tools_module(owner_allow)  # transport 默认 "uds"
    assert "web_search" not in module
    assert "web_extract" not in module


def test_owner_docker_override_dispatches_remote_even_if_global_env_type_is_local(monkeypatch):
    """Critical 逃逸回归测试。

    修复前：execute_code 的 fail-closed 守卫只看 overrides 里的 env_type
    （owner override == "docker" 才放行），但放行之后紧跟着的本地/远程分发
    判断读的是另一个变量——全局 `_get_env_config()["env_type"]`（默认
    "local"）。运营方只开 `security.multi_tenant.sandbox.enabled=true`、
    全局 TERMINAL_ENV 维持默认 "local"（最常见配置）时：守卫看到 owner
    override=docker 放行，但分发判断读到全局 local，会跳过
    _execute_remote/_get_or_create_env（owner 容器绑定逻辑），直接用
    subprocess.Popen 在宿主机上跑用户代码——这是隔离逃逸。

    本测试断言：owner override 给出 env_type=docker 时，即使全局
    _get_env_config() 仍是 "local"，execute_code 也必须走 _execute_remote
    （即将被覆盖后的 env_type 驱动到 owner 容器路径），而不是本地分支。
    """
    import tools.code_execution_tool as ce

    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.sandbox_enabled", lambda: True)
    monkeypatch.setattr(
        "tools.terminal_tool.resolve_task_overrides",
        lambda tid: {"env_type": "docker", "docker_image": "owner-image:latest"},
    )
    # 全局配置维持默认 TERMINAL_ENV=local（最常见的运营配置——运营方通常
    # 不会为了开多租户 sandbox 而去动全局 TERMINAL_ENV）。
    monkeypatch.setattr(
        "tools.terminal_tool._get_env_config",
        lambda: {"env_type": "local"},
    )

    called = {}

    def _fake_execute_remote(code, task_id, enabled_tools):
        called["hit"] = True
        return json.dumps({
            "status": "success", "output": "", "tool_calls_made": 0,
            "duration_seconds": 0,
        })

    monkeypatch.setattr(ce, "_execute_remote", _fake_execute_remote)

    out = ce.execute_code(code="print(1)", task_id="sess-2")

    assert called.get("hit") is True, (
        "execute_code 没有走 _execute_remote —— 多租户 sandbox 下 owner "
        "docker override 未生效，用户代码可能在宿主机 subprocess 分支执行"
    )
    assert json.loads(out)["status"] == "success"
