"""Task 11 契约测试：owner sandbox override 用 session_id 注册后，terminal 工具能 resolve 到。

测试目标（R3#1）：注册键 = session_id，与 run_conversation 的 task_id 一致，
保证 resolve_task_overrides(session_id) 能拿到 override。
"""
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
