# tests/tools/test_sandbox_concurrency_semaphore.py
"""
Task 13: 并发信号量测试
验证 _get_sandbox_semaphore 容量来自配置，且信号量包裹 docker 执行路径。
"""
from unittest.mock import patch
import tools.terminal_tool as tt


def test_semaphore_capacity_from_config():
    with patch("tools.terminal_tool.sandbox_config", return_value={"max_concurrent": 3}):
        sem = tt._get_sandbox_semaphore(force_fresh=True)
    assert sem._value == 3   # threading.Semaphore 初始计数


def test_semaphore_wraps_execution(monkeypatch):
    """R3#6 行为测试：执行命令前 acquire、命令结束后 release（用伪 semaphore 记录）。"""
    events = []

    class FakeSem:
        def __enter__(self): events.append("acquire"); return self
        def __exit__(self, *a): events.append("release")

    monkeypatch.setattr(tt, "_get_sandbox_semaphore", lambda *a, **k: FakeSem())

    # 伪造一个 docker env，execute 时记录顺序
    from unittest.mock import MagicMock, patch
    fake_env = MagicMock()

    def fake_execute(*a, **k):
        events.append("execute")
        return {"output": "ok", "exit_code": 0}

    fake_env.execute.side_effect = fake_execute

    task_id = "sess-sem"
    tt.register_task_env_overrides(task_id, {
        "env_type": "docker", "docker_image": "img", "host_cwd": "/data/workspaces/h",
        "docker_mount_cwd_to_workspace": True, "cwd": "/workspace", "network": False,
        "container_persistent": True, "docker_persist_across_processes": False,
        "mount_credentials": False, "mount_skills": True, "mount_cache": False,
    })
    try:
        with patch.object(tt, "multi_tenant_enabled", return_value=True), \
             patch.object(tt, "sandbox_enabled", return_value=True), \
             patch.object(tt, "_create_environment", return_value=fake_env), \
             patch.object(tt, "_active_environments", {}):
            tt.terminal_tool(command="echo hi", task_id=task_id)
    finally:
        tt.clear_task_env_overrides(task_id)

    # acquire 在 execute 前，release 在 execute 后
    assert events.index("acquire") < events.index("execute") < events.index("release")
