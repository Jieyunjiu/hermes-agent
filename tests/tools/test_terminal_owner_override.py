"""Task 4: 验证 owner override 真正驱动 env_type / host_cwd / network / mount_*。

测试策略：注册带 env_type 的 override（隔离键），让全局 TERMINAL_ENV=local，
         期望 _create_environment 被以 env_type="docker"、host_cwd、
         以及含 network/mount_* 的 container_config 调用。
"""
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
        "mount_credentials": False,
        "mount_skills": True,
        "mount_cache": False,
        "container_cpu": 2,
        "container_memory": 4096,
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

        assert captured["env_type"] == "docker", (
            f"env_type 应来自 override，实际为 {captured.get('env_type')!r}"
        )
        assert captured["host_cwd"] == "/data/workspaces/abc", (
            f"host_cwd 应来自 override，实际为 {captured.get('host_cwd')!r}"
        )
        cc = captured["container_config"]
        assert cc["network"] is False, f"network 应为 False，实际为 {cc.get('network')!r}"
        assert cc["docker_mount_cwd_to_workspace"] is True, (
            f"docker_mount_cwd_to_workspace 应为 True，实际为 {cc.get('docker_mount_cwd_to_workspace')!r}"
        )
        assert cc["mount_credentials"] is False, (
            f"mount_credentials 应为 False，实际为 {cc.get('mount_credentials')!r}"
        )
        assert cc["mount_cache"] is False, (
            f"mount_cache 应为 False，实际为 {cc.get('mount_cache')!r}"
        )
        assert cc["mount_skills"] is True, (
            f"mount_skills 应为 True，实际为 {cc.get('mount_skills')!r}"
        )
    finally:
        tt.clear_task_env_overrides(task_id)
