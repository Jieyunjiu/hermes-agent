"""企业微信多租户阶段 1 剩余安全边界测试。"""

import base64
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _enable_multi_tenant(monkeypatch, workspace_root=None):
    mt = {"enabled": True}
    if workspace_root is not None:
        mt["workspace_root"] = str(workspace_root)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"security": {"multi_tenant": mt}},
    )


def _owner_source():
    return SessionSource(
        platform=Platform.WECOM,
        chat_id="zhangsan",
        user_id="zhangsan",
        owner_key="wecom:corp:app:zhangsan",
    )


@pytest.mark.asyncio
async def test_reload_mcp_is_blocked_in_multi_tenant_gateway(monkeypatch):
    """`/reload-mcp` 不经过模型工具面，必须在 slash command 层单独拦截。"""
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:wecom:dm:zhangsan"
    runner._read_user_config = lambda: {}
    runner._request_slash_confirm = AsyncMock(return_value="should not prompt")
    event = MessageEvent(text="/reload-mcp", source=_owner_source())

    result = await runner._handle_reload_mcp_command(event)

    assert "multi-tenant" in result.lower()
    runner._request_slash_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_reload_skills_is_blocked_in_multi_tenant_gateway(monkeypatch):
    """`/reload-skills` 也是动态注入入口，allowlist 管不到，必须单独拦截。"""
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(text="/reload-skills", source=_owner_source())

    result = await runner._handle_reload_skills_command(event)

    assert "multi-tenant" in result.lower()


def test_multi_tenant_tool_policy_forces_curated_toolset(monkeypatch):
    """多租户 gateway 必须运行时收敛工具面，不能继承平台/MCP/plugin 工具集。"""
    _enable_multi_tenant(monkeypatch)

    from gateway.multi_tenant import constrain_toolsets_for_owner

    enabled, disabled = constrain_toolsets_for_owner(
        ["hermes-wecom", "terminal", "browser"],
        ["some-disabled"],
        owner_key="wecom:corp:app:zhangsan",
    )

    assert enabled == ["wecom_multi_tenant"]
    assert disabled is None


def test_multi_tenant_default_config_is_explicitly_disabled():
    """默认配置要有多租户结构，但不能默认打开隔离模式。"""
    from hermes_cli.config import DEFAULT_CONFIG

    multi_tenant = DEFAULT_CONFIG["security"]["multi_tenant"]

    assert multi_tenant["enabled"] is False
    assert multi_tenant["workspace_root"] == "/data/workspaces"


def test_wecom_multi_tenant_toolset_excludes_stage1_blocked_tools():
    """实际解析后的 toolset 不应包含阶段 1 明确禁用的高风险工具。"""
    from toolsets import resolve_toolset

    tools = set(resolve_toolset("wecom_multi_tenant"))

    assert {
        "memory",
        "session_search",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "skills_list",
        "skill_view",
        "todo",
        "clarify",
    }.issubset(tools)
    assert tools.isdisjoint(
        {
            "terminal",
            "process",
            "execute_code",
            "delegate_task",
            "skill_manage",
            "cronjob",
            "browser_cdp",
            "computer_use",
        }
    )


@pytest.mark.asyncio
async def test_wecom_inbound_file_upload_lands_inside_owner_workspace(
    tmp_path, monkeypatch
):
    """WeCom 入站附件应落到 owner workspace/uploads，而不是全局 cache。"""
    _enable_multi_tenant(monkeypatch, workspace_root=tmp_path / "workspaces")

    from gateway.multi_tenant import hash_owner_key
    from plugins.platforms.wecom.adapter import WeComAdapter

    adapter = WeComAdapter(
        PlatformConfig(
            enabled=True,
            extra={"bot_id": "app", "secret": "secret", "corp_id": "corp"},
        )
    )
    owner_key = "wecom:corp:app:zhangsan"
    payload = base64.b64encode(b"hello").decode("ascii")

    path, content_type = await adapter._cache_media(
        "file",
        {"base64": payload, "filename": "../report.txt"},
        owner_key=owner_key,
        upload_id="msg-1",
    )

    expected_root = tmp_path / "workspaces" / hash_owner_key(owner_key)
    assert str(path).startswith(str(expected_root / "uploads" / "msg-1"))
    assert path.endswith("report.txt")
    assert content_type == "text/plain"
    assert (expected_root / "uploads" / "msg-1" / "report.txt").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_memory_pending_command_uses_event_owner_context(tmp_path, monkeypatch):
    """冷路径 `/memory pending` 在 _set_session_env 之前执行，也必须绑定 owner。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner
    from gateway.multi_tenant import clear_current_owner_key, set_current_owner_key
    from tools import write_approval as wa

    owner = "wecom:corp:app:zhangsan"
    try:
        set_current_owner_key(owner)
        wa.stage_write(
            wa.MEMORY,
            {"action": "add", "target": "memory", "content": "owner note"},
            summary="owner note",
            origin="foreground",
        )
    finally:
        clear_current_owner_key()

    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:wecom:dm:zhangsan"
    runner._evict_cached_agent = lambda session_key: None
    event = MessageEvent(text="/memory pending", source=_owner_source())

    result = await runner._handle_memory_command(event)

    assert "owner note" in result


@pytest.mark.asyncio
async def test_skills_pending_command_uses_event_owner_context(tmp_path, monkeypatch):
    """`/skills pending` 同样是冷路径命令，不能因为缺 ContextVar 退回全局。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner
    from gateway.multi_tenant import clear_current_owner_key, set_current_owner_key
    from tools import write_approval as wa

    owner = "wecom:corp:app:zhangsan"
    try:
        set_current_owner_key(owner)
        wa.stage_write(
            wa.SKILLS,
            {"action": "create", "name": "owner-skill", "content": "body"},
            summary="create owner-skill",
            origin="foreground",
        )
    finally:
        clear_current_owner_key()

    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:wecom:dm:zhangsan"
    runner._evict_cached_agent = lambda session_key: None
    event = MessageEvent(text="/skills pending", source=_owner_source())

    result = await runner._handle_skills_command(event)

    assert "owner-skill" in result
