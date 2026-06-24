from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import sys

import pytest

import run_agent
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _redirect_hermes_home(tmp_path, monkeypatch):
    """这些用例会构造 AIAgent，日志必须写到 pytest 临时目录。"""
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(run_agent, "_hermes_home", hermes_home)


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=f"{platform}-session",
            platform=platform,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "telegram-session"


def test_run_conversation_persists_tokens_for_cron_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="cron")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "cron-session"


def test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one(monkeypatch):
    sentinel_db = object()
    captured = {}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(None, platform="acp")
    result = json.loads(agent._invoke_tool("session_search", {"query": "Hermes"}, "task-id"))

    assert result["success"] is True
    assert captured["db"] is sentinel_db
    assert captured["query"] == "Hermes"
    assert agent._session_db is sentinel_db


def test_runtime_session_search_honors_owner_context(tmp_path, monkeypatch):
    """运行时特殊分支也必须继承 ContextVar owner，不能退回全局历史。"""
    from gateway.multi_tenant import clear_current_owner_key, set_current_owner_key
    from hermes_state import SessionDB

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"security": {"multi_tenant": {"enabled": True}}},
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("alice-session", "wecom", owner_key="wecom:corp:app:alice")
    db.create_session("bob-session", "wecom", owner_key="wecom:corp:app:bob")
    db.append_message("bob-session", "user", "bobsecretneedle belongs to bob")

    agent = _make_agent(db, platform="wecom")
    agent.session_id = "alice-session"

    try:
        set_current_owner_key("wecom:corp:app:alice")
        raw = agent._invoke_tool(
            "session_search",
            {"query": "bobsecretneedle", "limit": 5},
            "task-id",
        )
    finally:
        clear_current_owner_key()
        db.close()

    result = json.loads(raw)
    assert result["success"] is True
    assert result["results"] == []
