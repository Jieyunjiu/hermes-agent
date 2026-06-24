"""多租户模式下 session_search 的 owner 边界测试。"""

import json

from hermes_state import SessionDB
from tools.session_search_tool import session_search


def _enable_multi_tenant(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"security": {"multi_tenant": {"enabled": True}}},
    )


def test_session_search_fails_closed_when_owner_key_missing(tmp_path, monkeypatch):
    """多租户开启后，没有绑定 owner_key 时不能退回全局历史。"""
    _enable_multi_tenant(monkeypatch)

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("victim", "wecom", owner_key="wecom:corp:app:alice")
    try:
        raw = session_search(db=db)
    finally:
        db.close()

    result = json.loads(raw)
    assert result["success"] is False
    assert "owner" in result["error"].lower()


def test_session_search_rejects_explicit_profile_read_under_owner_scope(
    tmp_path, monkeypatch
):
    """显式 profile 读取也是跨隔离岛访问，多租户模式下应直接拒绝。"""
    _enable_multi_tenant(monkeypatch)

    from gateway.multi_tenant import clear_current_owner_key, set_current_owner_key

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("current", "wecom", owner_key="wecom:corp:app:alice")
    try:
        set_current_owner_key("wecom:corp:app:alice")
        raw = session_search(db=db, session_id="current", profile="other-profile")
    finally:
        clear_current_owner_key()
        db.close()

    result = json.loads(raw)
    assert result["success"] is False
    assert "profile" in result["error"].lower()
