"""
Task 5：多租户 sandbox fail-closed 守卫

多租户 sandbox 开启时，env_type 必须来自 owner override；
即使全局 TERMINAL_ENV=docker，若无 owner override 注册，也必须拒绝执行，绝不退化为全局容器。
"""
import json
from unittest.mock import patch, MagicMock
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


def test_multi_tenant_refuses_global_docker_without_owner_override():
    """回归测试：全局 TERMINAL_ENV=docker + 无 owner override → 必须拒绝（隔离泄漏漏洞修复）。

    漏洞场景：操作员设置 TERMINAL_ENV=docker，但某 task_id 的 owner override 注册失败
    （overrides == {}），旧守卫仅检查合并后的 env_type，仍为 "docker" → 守卫放行，
    terminal 在全局、非 owner 绑定的容器中执行 → 多租户隔离泄漏。

    修复后，守卫检查 overrides.get("env_type") == "docker"，override 缺失时直接拒绝。
    """
    task_id = "no-override-task"
    # 确保此 task_id 没有注册任何 override
    tt.clear_task_env_overrides(task_id)

    # 检测创建容器的函数是否被调用（被调用即属泄漏）
    create_env_called = []

    def fake_create_environment(**kwargs):
        create_env_called.append(kwargs)
        env = MagicMock()
        env.execute.return_value = {"output": "pwned", "exit_code": 0}
        return env

    with patch("tools.terminal_tool.multi_tenant_enabled", return_value=True), \
         patch("tools.terminal_tool.sandbox_enabled", return_value=True), \
         patch("tools.terminal_tool.get_current_owner_key", return_value="wecom:c:a:u"), \
         patch.dict("os.environ", {"TERMINAL_ENV": "docker"}), \
         patch.object(tt, "_create_environment", side_effect=fake_create_environment), \
         patch.object(tt, "_active_environments", {}):
        out = json.loads(tt.terminal_tool(command="echo hi", task_id=task_id))

    # 必须拒绝，不得创建/执行任何容器
    assert not create_env_called, (
        "守卫漏洞：全局 docker + 无 owner override 时，_create_environment 不应被调用"
    )
    assert out.get("status") == "error" or out.get("error"), (
        f"必须返回拒绝响应，实际: {out}"
    )
    assert "owner" in out.get("error", "").lower() or "override" in out.get("error", "").lower() \
        or "refus" in out.get("error", "").lower(), (
        f"拒绝消息应提及 owner override，实际: {out.get('error', '')}"
    )
