"""Task 8：多租户模式下 file 工具不创建 docker 容器（option A：进程内）。

覆盖 spec §5.5.2 / §9.8c(a)，审查 R3#5 / R4#3。
"""
from unittest.mock import patch
import tools.file_tools as ft


def test_read_file_does_not_create_docker_even_when_env_type_docker(tmp_path):
    """R3#5 / R4#3：即使 TERMINAL_ENV=docker，多租户下 read_file_tool 也不应创建 docker 容器。"""
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "a.txt").write_text("hello", encoding="utf-8")

    def fake_create_environment(*a, **k):
        raise AssertionError("should not create docker container in multi-tenant A-mode")

    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root), \
         patch.dict("os.environ", {"TERMINAL_ENV": "docker"}), \
         patch("tools.terminal_tool._create_environment", side_effect=fake_create_environment):
        result = ft.read_file_tool("/workspace/a.txt")

    # 进程内读到内容，且 fake_create_environment 从未被调用（否则会抛 AssertionError）
    assert "hello" in str(result)
