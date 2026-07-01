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
