from pathlib import Path
from unittest.mock import patch
import gateway.multi_tenant as mt


def test_build_owner_sandbox_overrides_shape(tmp_path):
    owner_root = tmp_path / "data" / "workspaces" / "abc123"
    with patch.object(mt, "sandbox_config", return_value={
        "image": "hermes-sandbox:latest", "cpus": "2", "memory_mb": 4096,
    }), patch.object(mt, "owner_workspace_root", return_value=owner_root):
        ov = mt.build_owner_sandbox_overrides("wecom:c:a:u")

    assert ov["env_type"] == "docker"
    assert ov["docker_image"] == "hermes-sandbox:latest"
    assert ov["host_cwd"] == str(owner_root)
    assert ov["docker_mount_cwd_to_workspace"] is True
    assert ov["cwd"] == "/workspace"
    assert ov["network"] is False
    assert ov["docker_volumes"] == []
    assert ov["container_persistent"] is True
    assert ov["docker_persist_across_processes"] is False
    assert ov["mount_credentials"] is False
    assert ov["mount_skills"] is True
    assert ov["mount_cache"] is False
    assert ov["container_cpu"] == 2
    assert ov["container_memory"] == 4096


def test_build_owner_sandbox_overrides_creates_workspace_dir(tmp_path):
    """审查 R3#2：owner workspace 目录不存在时应被创建（否则 docker 不挂）。"""
    owner_root = tmp_path / "data" / "workspaces" / "abc123"
    assert not owner_root.exists()
    with patch.object(mt, "sandbox_config", return_value={"image": "img"}), \
         patch.object(mt, "owner_workspace_root", return_value=owner_root):
        ov = mt.build_owner_sandbox_overrides("wecom:c:a:u")
    assert owner_root.is_dir()           # 已创建
    assert ov["host_cwd"] == str(owner_root)
