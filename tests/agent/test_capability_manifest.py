"""Tests for build_capability_manifest() — sandbox 能力清单（防幻觉锚点）。

两类断言：
  1. 单元：build_capability_manifest() 包含边界声明，且幂等。
  2. 集成：build_system_prompt_parts 在 wecom_multi_tenant_sandbox
     工具集激活时，把清单注入 stable 段；未激活时不注入。
"""
from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_builder import build_capability_manifest


# ── 单元测试 ────────────────────────────────────────────────────────────────


def test_manifest_states_sandbox_boundaries():
    text = build_capability_manifest()
    # 工作区边界
    assert "/workspace" in text or "工作区" in text
    # 网络限制
    assert "网络" in text or "network" in text.lower()
    # 诚实锚点：schema 之外的能力不存在
    assert "schema" in text.lower() or "不要声称" in text


def test_manifest_is_static():
    """纯函数，两次调用返回完全相同的字符串。"""
    assert build_capability_manifest() == build_capability_manifest()


# ── 集成测试辅助 ─────────────────────────────────────────────────────────────


def _make_agent(**overrides):
    """返回能让 build_system_prompt_parts 正常运行的最小 agent stub。"""
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        enabled_toolsets=None,
        context_compressor=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_PATCHES = dict(
    **{"run_agent.load_soul_md": ("", None)},
)

_COMMON_PATCHES = {
    "run_agent.load_soul_md": "",
    "run_agent.build_nous_subscription_prompt": "",
    "run_agent.build_environment_hints": "",
    "run_agent.build_context_files_prompt": "",
}


def _build_stable(agent):
    from agent.system_prompt import build_system_prompt_parts

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


# ── 集成测试 ─────────────────────────────────────────────────────────────────


def test_manifest_present_in_assembled_prompt_under_sandbox_toolset():
    """激活 wecom_multi_tenant_sandbox → stable 段含清单文本。"""
    agent = _make_agent(enabled_toolsets=["wecom_multi_tenant_sandbox"])
    stable = _build_stable(agent)
    # 检查清单的专属标题，避免误判其他内容
    assert "你的执行环境（多租户沙箱）" in stable


def test_manifest_absent_without_sandbox_toolset():
    """未激活 sandbox 工具集 → stable 段不含清单标题。"""
    agent = _make_agent(enabled_toolsets=None)
    stable = _build_stable(agent)
    assert "你的执行环境（多租户沙箱）" not in stable
