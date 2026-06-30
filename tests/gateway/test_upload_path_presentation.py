"""Task 12: 上传文件路径呈现给模型时转 /workspace/...（不改内部路径）

测试 _to_workspace_view 的 fail-closed 行为（R3#3）：
- owner_root 内的路径 → /workspace/<rel>
- 越界路径（如 /etc/passwd）→ None，绝不回传宿主机路径
"""
from pathlib import Path
from gateway.run import _to_workspace_view


def test_host_upload_path_becomes_workspace_view():
    owner_root = Path("/data/workspaces/h")
    host = "/data/workspaces/h/uploads/abc/报表.xlsx"
    assert _to_workspace_view(host, owner_root) == "/workspace/uploads/abc/报表.xlsx"


def test_out_of_bounds_path_fails_closed():
    """R3#3：越界路径不得原样回传宿主机路径，必须 None（fail-closed）。"""
    owner_root = Path("/data/workspaces/h")
    assert _to_workspace_view("/etc/passwd", owner_root) is None
