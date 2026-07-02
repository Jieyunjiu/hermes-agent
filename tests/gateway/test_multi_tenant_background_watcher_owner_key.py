"""回归测试：多租户下 owner_key ContextVar 曾在两条链路上丢失（已修复）。

背景：企业微信多租户场景下，用户让 Hermes 用 terminal/沙箱生成 Word 报告时，
gateway 报 ``OwnerKeyMissing``（memory_tool 内部）以及 "Skipping unsafe MEDIA
directive path" 警告（出站投递）。定位到两个具体、可定位的根因，不是同一处
代码，但都指向"owner_key ContextVar 的有效范围与实际使用点不匹配"：

1.（根因1，已修复）``_build_process_event_source``（gateway/run.py）在
   session_store / 内存缓存都命中不到时，走手写 fallback 分支构造
   ``SessionSource``——这个分支原来没有传 ``owner_key``。后台进程完成通知
   （``notify_on_complete`` / 崩溃恢复重新挂 watcher）走的正是这条函数。
   修复：``tools/terminal_tool.py`` spawn 时采集 owner_key 存进
   ``ProcessSession.watcher_owner_key``（``tools/process_registry.py`` 新增
   字段，checkpoint 向后兼容），``pending_watchers`` / completion_queue 事件
   都带上它；``_build_process_event_source`` 读到后正确传给
   ``SessionSource(owner_key=...)``；多租户模式下仍然取不到（旧 checkpoint、
   CLI 来源等）时优雅 fail-closed 返回 ``None``（调用方本来就会丢弃该通知），
   不会用空 owner 放行，也不会让 ``OwnerKeyMissing`` 冒泡崩掉 agent loop。

2.（根因2，已修复）``_set_session_env`` / ``_clear_session_env``
   （gateway/run.py）只包住了 ``_handle_message_with_agent`` 这一段；但真正
   需要 owner_key 的 MEDIA 路径换算
   （``gateway/platforms/base.py:_map_workspace_delivery_path_to_owner_root``）
   发生在调用方 ``_process_message_background``（同一个 asyncio 任务，但在
   ``_handle_message_with_agent`` 已经返回、owner_key 已被清空之后）。
   修复：``_process_message_background`` 在做路径换算前用
   ``scoped_owner_key(event.source.owner_key or "")`` 从事件本身重新短暂
   绑定一次，退出时自动恢复，不影响后续逻辑。
"""

import pytest

from gateway.config import Platform


def _enable_multi_tenant(monkeypatch, workspace_root=None):
    mt = {"enabled": True}
    if workspace_root is not None:
        mt["workspace_root"] = str(workspace_root)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"security": {"multi_tenant": mt}},
    )


def test_build_process_event_source_fallback_propagates_owner_key(monkeypatch):
    """session_store / 内存缓存都未命中时，只要 evt 带 owner_key，fallback 分支
    重建的 source 也要带上它（对应 gateway/run.py `_build_process_event_source`
    手写 fallback 分支）。

    这个 evt 形状对应修复后的 tools/terminal_tool.py：spawn 时采集
    current_owner_key_or_none()，塞进 pending_watchers.append({...}) 的
    "owner_key" 字段。
    """
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    class _EmptySessionStore:
        _entries = {}

        def _ensure_loaded(self):
            pass

    runner.session_store = _EmptySessionStore()
    runner._get_cached_session_source = lambda session_key: None

    evt = {
        "session_id": "proc-1",
        "session_key": "agent:main:wecom:dm:zhangsan",
        "platform": "wecom",
        "chat_type": "dm",
        "chat_id": "zhangsan",
        "user_id": "zhangsan",
        "user_name": "zhangsan",
        "thread_id": "",
        "notify_on_complete": True,
        "owner_key": "wecom:corp:app:zhangsan",
    }

    source = runner._build_process_event_source(evt)

    assert source is not None
    assert source.owner_key == "wecom:corp:app:zhangsan", (
        "_build_process_event_source fallback 分支没有把 evt 里的 owner_key "
        "透传到重建出来的 SessionSource —— 下游 _set_session_env 会把 owner_key "
        "绑定为空字符串，multi-tenant 模式下这个新 turn 里任何调用 "
        "get_current_owner_key() 的工具都会 fail-closed 报 OwnerKeyMissing"
    )


def test_build_process_event_source_fallback_fails_closed_when_owner_key_missing(
    monkeypatch, caplog
):
    """向后兼容场景：旧 checkpoint / 无法采集到 owner_key 的合成事件，
    多租户模式下必须优雅拒绝（返回 None），绝不能用空 owner_key 放行去跑一整个
    新 turn（那会导致 turn 内 memory_tool 等工具 fail-closed 报未捕获的
    OwnerKeyMissing，崩掉 agent loop）。
    """
    _enable_multi_tenant(monkeypatch)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    class _EmptySessionStore:
        _entries = {}

        def _ensure_loaded(self):
            pass

    runner.session_store = _EmptySessionStore()
    runner._get_cached_session_source = lambda session_key: None

    # 模拟升级前写入的旧 checkpoint：没有 watcher_owner_key 字段，
    # ProcessSession.recover_from_checkpoint 里 entry.get("watcher_owner_key", "")
    # 会读到 ""，pending_watchers 里也就是 "" / 不存在这个 key。
    evt = {
        "session_id": "proc-1",
        "session_key": "agent:main:wecom:dm:zhangsan",
        "platform": "wecom",
        "chat_type": "dm",
        "chat_id": "zhangsan",
        "user_id": "zhangsan",
        "user_name": "zhangsan",
        "thread_id": "",
        "notify_on_complete": True,
    }

    with caplog.at_level("WARNING"):
        source = runner._build_process_event_source(evt)

    assert source is None, (
        "多租户模式下 owner_key 缺失时 _build_process_event_source 必须 "
        "fail-closed 返回 None，不能用空 owner 拼一个可用的 SessionSource"
    )
    assert "owner_key" in caplog.text


@pytest.mark.asyncio
async def test_media_delivery_path_mapping_after_handler_returns_loses_owner_key(
    tmp_path, monkeypatch
):
    """owner_key ContextVar 的绑定范围只覆盖 _handle_message_with_agent，
    但 MEDIA 路径换算发生在调用方 _process_message_background 里，
    这里驱动真实的 _process_message_background 端到端跑一遍，验证修复后
    （根因2：filter_media_delivery_paths 调用点用 scoped_owner_key 从
    event.source.owner_key 重新绑定）附件确实能投递出去，而不是被
    "Skipping unsafe MEDIA directive path" 静默丢弃。
    """
    from unittest.mock import AsyncMock

    from gateway.config import PlatformConfig
    from gateway.multi_tenant import owner_workspace_root
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource, build_session_key

    owner_key = "wecom:corp:app:zhangsan"
    _enable_multi_tenant(monkeypatch, workspace_root=tmp_path / "workspaces")

    # 模拟 agent 在容器里把报告写到了 owner 专属的宿主机目录下（对应容器视角的
    # /workspace/report.docx，_map_workspace_delivery_path_to_owner_root 负责换算）。
    owner_root = owner_workspace_root(owner_key)
    owner_root.mkdir(parents=True, exist_ok=True)
    (owner_root / "report.docx").write_bytes(b"docx-bytes")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (owner_root,),
    )

    class _WecomLikeAdapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="test"), Platform.WECOM)

        async def connect(self):
            return True

        async def disconnect(self):
            pass

        async def send(self, chat_id, content=None, **kwargs):
            return SendResult(success=True, message_id="text")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id, "type": "dm"}

    adapter = _WecomLikeAdapter()
    source = SessionSource(
        platform=Platform.WECOM,
        chat_id="zhangsan",
        chat_type="dm",
        user_id="zhangsan",
        owner_key=owner_key,
    )
    event = MessageEvent(
        text="帮我把 PDF 转成 Word 报告",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )
    # _message_handler 相当于跑完的 _handle_message_with_agent：返回时
    # owner_key ContextVar 已经在它自己的 finally 里被清空了，
    # _process_message_background 拿到 response 后才做 MEDIA 提取/换算。
    adapter._message_handler = AsyncMock(return_value="MEDIA:/workspace/report.docx")
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))

    await adapter._process_message_background(event, build_session_key(source))

    # 复现的 bug：如果 filter_media_delivery_paths 换算路径时 owner_key 已丢失，
    # _map_workspace_delivery_path_to_owner_root 返回 None，附件被判定"不安全"
    # 直接丢弃，send_document 永远不会被调用。
    adapter.send_document.assert_awaited_once()
    delivered_path = adapter.send_document.await_args.kwargs["file_path"]
    assert delivered_path == str((owner_root / "report.docx").resolve()), (
        "MEDIA 附件投递路径没有正确换算到 zhangsan 的 owner_root——说明 "
        "_process_message_background 里做路径换算时 owner_key 已经丢失"
    )
