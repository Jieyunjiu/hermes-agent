"""T2：多租户 sandbox 下 file 工具下沉 owner docker 容器 + fail-closed。

覆盖：
- 无 owner docker override 时，四个 file handler 必须拒绝（不落回主机/无 owner 容器）。
- 有 owner docker override 时，`_get_file_ops` 建容器的参数必须来自 override
  （host_cwd/network/mount_* 等），而不是全局 TERMINAL_ENV 配置。
- `_file_ops_path_for_call` 在多租户下必须把宿主机绝对路径转成容器内 /workspace
  路径（file 工具现在经 docker exec 在 owner 容器里执行，容器看不到宿主机路径）。
- `write_file_tool` / `patch_tool`（replace 模式）真正调用 `file_ops.write_file`
  / `file_ops.patch_replace` 时，传的必须是换算后的容器路径，而不是宿主机绝对
  路径——之前只测了 `_file_ops_path_for_call` 这个 helper 本身，没覆盖真实调用
  点，漏掉了这两处未接线的 bug（T2 第二次收尾修复）。

对应 `.superpowers/sdd/task-2-brief.md`。
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_no_owner_override_refuses_write_patch_search(monkeypatch):
    # 其余三个 handler 同样必须拒绝，不能只护住 read_file。
    _enable_mt(monkeypatch, override={})

    out_write = json.loads(ft._handle_write_file({"path": "/workspace/x.txt", "content": "hi"}, task_id="sess-1"))
    assert out_write["status"] == "error"
    assert "refused" in out_write["error"]

    out_patch = json.loads(ft._handle_patch({"mode": "replace", "path": "/workspace/x.txt", "old_string": "a", "new_string": "b"}, task_id="sess-1"))
    assert out_patch["status"] == "error"
    assert "refused" in out_patch["error"]

    out_search = json.loads(ft._handle_search_files({"pattern": "x"}, task_id="sess-1"))
    assert out_search["status"] == "error"
    assert "refused" in out_search["error"]


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


def test_file_ops_path_for_call_maps_to_container_workspace(monkeypatch):
    # file 工具现在经 docker exec 在 owner 容器里执行（见 `_get_file_ops`），
    # 容器里看不到宿主机文件系统，只能看到 bind mount 到 /workspace 的 owner_root。
    # 所以传给容器执行器的路径必须是 /workspace/<相对路径>，而不是宿主机绝对路径。
    owner_root = Path("/data/workspaces/hashA")
    monkeypatch.setattr(ft, "_multi_tenant_workspace_root", lambda: owner_root)

    resolved_file = owner_root / "x.txt"
    assert ft._file_ops_path_for_call("/workspace/x.txt", resolved_file) == "/workspace/x.txt"

    resolved_nested = owner_root / "sub" / "y.txt"
    assert ft._file_ops_path_for_call("/workspace/sub/y.txt", resolved_nested) == "/workspace/sub/y.txt"

    # resolved 恰好等于 owner_root 本身（例如列目录时传根目录）-> 容器内就是 /workspace
    assert ft._file_ops_path_for_call("/workspace", owner_root) == "/workspace"


def test_file_ops_path_for_call_returns_original_when_not_multi_tenant(monkeypatch):
    # 非多租户模式（legacy 单用户）行为不变：直接用调用方原始传入的路径。
    monkeypatch.setattr(ft, "_multi_tenant_workspace_root", lambda: None)
    resolved = Path("/home/user/project/x.txt")
    assert ft._file_ops_path_for_call("x.txt", resolved) == "x.txt"


def test_write_file_tool_passes_container_path_to_file_ops(monkeypatch):
    # 回归：write_file_tool 之前把宿主机绝对路径（_resolved）直接传给
    # file_ops.write_file，绕过了 _file_ops_path_for_call 的换算。多租户 docker
    # 下容器看不到宿主机路径，这会在容器可写层 mkdir -p 出孤儿文件——宿主机看
    # 不到、容器销毁即丢、之后用 /workspace/x 也读不到，属于静默数据丢失。
    owner_root = Path("/data/workspaces/hashA")
    monkeypatch.setattr(ft, "_multi_tenant_workspace_root", lambda: owner_root)

    captured = {}

    def _write_file(path, content):
        captured["path"] = path
        return SimpleNamespace(to_dict=lambda: {"bytes_written": len(content)})

    fake_ops = MagicMock()
    fake_ops.write_file = _write_file
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fake_ops)

    out = ft.write_file_tool(path="/workspace/sub/x.txt", content="hi", task_id="sess-write-1")
    result = json.loads(out)
    assert not result.get("error"), result
    # 关键断言：交给执行器的必须是容器内 /workspace 路径，不是宿主机绝对路径。
    assert captured["path"] == "/workspace/sub/x.txt"


def test_patch_tool_replace_passes_container_path_to_file_ops(monkeypatch):
    # 回归：patch_tool 的 replace 分支之前把 _path_to_resolved（宿主机绝对路径）
    # 直接传给 file_ops.patch_replace。多租户 docker 下容器内 cat 不到该路径，
    # replace 会直接失败（"Could not find" 之类），而不是在正确文件上生效。
    owner_root = Path("/data/workspaces/hashA")
    monkeypatch.setattr(ft, "_multi_tenant_workspace_root", lambda: owner_root)

    captured = {}

    def _patch_replace(path, old_string, new_string, replace_all=False):
        captured["path"] = path
        return SimpleNamespace(to_dict=lambda: {"success": True, "diff": "--- a\n+++ b\n"})

    fake_ops = MagicMock()
    fake_ops.patch_replace = _patch_replace
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fake_ops)

    out = ft.patch_tool(
        mode="replace", path="/workspace/sub/y.txt",
        old_string="a", new_string="b", task_id="sess-patch-1",
    )
    result = json.loads(out)
    assert not result.get("error"), result
    # 关键断言：交给执行器的必须是容器内 /workspace 路径，不是宿主机绝对路径。
    assert captured["path"] == "/workspace/sub/y.txt"
