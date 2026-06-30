"""测试 DockerEnvironment 三个挂载开关（mount_credentials / mount_skills / mount_cache）。

必须用 tmp_path 真实文件/目录，因为 docker.py 会用 is_file()/is_dir() 检验路径，
假路径会被直接跳过，开关就测不出来。
"""

from unittest.mock import patch
from tools.environments.docker import DockerEnvironment


def _real_mounts(tmp_path):
    """创建真实的凭证文件 + skills/cache 目录供挂载测试使用。"""
    creds = tmp_path / "creds.json"
    creds.write_text("{}", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    return creds, skills, cache


def _run_args_for(tmp_path, **kwargs):
    """构造 DockerEnvironment 但不真正起容器，截获最后一次 docker run 参数。"""
    creds, skills, cache = _real_mounts(tmp_path)
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = list(cmd)

        class R:
            returncode = 0
            stdout = "containerid\n"
            stderr = ""

        return R()

    with (
        patch("tools.environments.docker.subprocess.run", side_effect=fake_run),
        patch("tools.environments.docker.find_docker", return_value="/usr/bin/docker"),
        patch("tools.environments.docker._ensure_docker_available", return_value=None),
        patch(
            "tools.credential_files.get_credential_file_mounts",
            return_value=[{"host_path": str(creds), "container_path": "/root/.creds.json"}],
        ),
        patch(
            "tools.credential_files.get_skills_directory_mount",
            return_value=[{"host_path": str(skills), "container_path": "/root/.hermes/skills"}],
        ),
        patch(
            "tools.credential_files.get_cache_directory_mounts",
            return_value=[{"host_path": str(cache), "container_path": "/root/.cache"}],
        ),
    ):
        DockerEnvironment(image="img", cwd="/workspace", timeout=10, **kwargs)

    return " ".join(captured.get("cmd", [])), str(creds), str(skills), str(cache)


def test_mount_switches_off_excludes_creds_and_cache_keeps_skills(tmp_path):
    """关掉 credentials 和 cache 开关后，docker run 参数里不含对应路径；skills 仍存在。"""
    args, creds, skills, cache = _run_args_for(
        tmp_path,
        mount_credentials=False,
        mount_cache=False,
        mount_skills=True,
    )
    assert creds not in args, f"creds 路径应被排除，但出现在 args 中: {args}"
    assert cache not in args, f"cache 路径应被排除，但出现在 args 中: {args}"
    assert skills in args, f"skills 路径应出现在 args 中，但未找到: {args}"


def test_all_switches_on_includes_all_mounts(tmp_path):
    """默认全开时，三类路径都应出现在 docker run 参数里（保持旧行为不变）。"""
    args, creds, skills, cache = _run_args_for(tmp_path)
    assert creds in args, f"creds 路径应出现在 args 中: {args}"
    assert skills in args, f"skills 路径应出现在 args 中: {args}"
    assert cache in args, f"cache 路径应出现在 args 中: {args}"


def test_all_switches_off_excludes_all_mounts(tmp_path):
    """全关时，三类路径都不应出现在 docker run 参数里。"""
    args, creds, skills, cache = _run_args_for(
        tmp_path,
        mount_credentials=False,
        mount_skills=False,
        mount_cache=False,
    )
    assert creds not in args, f"creds 路径应被排除: {args}"
    assert skills not in args, f"skills 路径应被排除: {args}"
    assert cache not in args, f"cache 路径应被排除: {args}"
