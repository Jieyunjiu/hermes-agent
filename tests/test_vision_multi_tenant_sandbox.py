"""T4：多租户下 vision_analyze owner 路径收敛 + 禁远程 URL。

覆盖：
- `http(s)://` 远程 URL 一律拒绝（防 SSRF / 跨域外联）。
- 越界本地路径（不在 owner workspace 内）拒绝。
- 两个前置入口（注册 handler `_handle_vision_analyze` 与直调函数
  `vision_analyze_tool`）都要挂上收敛逻辑，任一条路径都不能绕过。

对应 `.superpowers/sdd/task-4-brief.md`。
"""
import asyncio
import json
import tools.vision_tools as vt


def _enable_mt(monkeypatch, owner_root):
    # P4：patch 原始模块属性（实现用函数内 import）
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr("gateway.multi_tenant.owner_workspace_root", lambda k: owner_root)
    monkeypatch.setattr("gateway.multi_tenant.get_current_owner_key", lambda: "wecom:c:a:u")


def test_http_url_rejected(monkeypatch, tmp_path):
    # _handle_vision_analyze 是 async（返回 Awaitable[str]）→ 用 asyncio.run 驱动
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt._handle_vision_analyze({"image_url": "https://evil/x.png", "prompt": "?"}))
    assert json.loads(out).get("status") == "error"


def test_out_of_workspace_path_rejected(monkeypatch, tmp_path):
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt._handle_vision_analyze({"image_url": "/etc/passwd", "prompt": "?"}))
    assert json.loads(out).get("status") == "error"


def test_vision_analyze_tool_direct_call_rejects_url(monkeypatch, tmp_path):
    # run_agent 会直接调用 vision_analyze_tool，绕过 _handle_vision_analyze，
    # 必须单独覆盖这条直调路径，确认它也挂了收敛逻辑。
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt.vision_analyze_tool("https://evil/x.png", "describe"))
    assert json.loads(out).get("status") == "error"


def test_vision_analyze_tool_direct_call_rejects_out_of_workspace_path(monkeypatch, tmp_path):
    _enable_mt(monkeypatch, tmp_path)
    out = asyncio.run(vt.vision_analyze_tool("/etc/passwd", "describe"))
    assert json.loads(out).get("status") == "error"
