"""T2：多租户 sandbox 下 file 工具下沉 owner docker 容器 + fail-closed。

覆盖：
- 无 owner docker override 时，四个 file handler 必须拒绝（不落回主机/无 owner 容器）。
- 有 owner docker override 时，`_get_file_ops` 建容器的参数必须来自 override
  （host_cwd/network/mount_* 等），而不是全局 TERMINAL_ENV 配置。

对应 `.superpowers/sdd/task-2-brief.md`。
"""
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
