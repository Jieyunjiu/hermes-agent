import json
from unittest.mock import patch
import tools.terminal_tool as tt


def test_background_rejected_in_multi_tenant():
    with patch("tools.terminal_tool.multi_tenant_enabled", return_value=True), \
         patch("tools.terminal_tool.sandbox_enabled", return_value=True):
        out = json.loads(tt.terminal_tool(command="sleep 1", background=True, task_id="x"))
    assert out.get("error")
    assert "background" in out["error"].lower() or "前台" in out["error"]
