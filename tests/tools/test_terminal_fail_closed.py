"""
Task 5：多租户 sandbox fail-closed 守卫

多租户 sandbox 开启时，env_type 必须是 docker；
若最终解析出的 env_type 仍是 local，必须拒绝执行，绝不退化。
"""
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
