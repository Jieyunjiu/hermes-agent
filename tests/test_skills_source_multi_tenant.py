"""多租户下 skills 来源治理测试。

任务背景：企业微信多租户改造要求「用户只能用公司统一开发的 skills、
不能自建/自加」，因此多租户模式下需要：
1. `get_external_skills_dirs()` 不采纳 `skills.external_dirs` 等用户可写来源；
2. `skill_view` 对 `plugin:skill` 限定名直接拒绝加载，不 discover/serve。

单用户模式（multi_tenant_enabled() 返回 False）行为不受影响，不在本文件覆盖，
因为改动只在多租户分支内短路返回。
"""

import json

import agent.skill_utils as su
import tools.skills_tool as st


def test_external_dirs_empty_under_multi_tenant(monkeypatch, tmp_path):
    # `_read_external_dirs_from_config` 这个私有函数在当前实现中并不存在
    # （real 读取点是 get_config_path() + _load_raw_config()），按 brief 注释
    # 对齐到实际读取点：构造一份真实的 config.yaml，指向一个确实存在的
    # external_dirs 目录，验证即使配置了它，多租户下也不采纳。
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)

    ext_dir = tmp_path / "external_skills"
    ext_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"skills:\n  external_dirs:\n    - {ext_dir}\n")

    monkeypatch.setattr(su, "get_config_path", lambda: config_path)
    su._external_dirs_cache_clear()

    assert su.get_external_skills_dirs() == []


def test_plugin_skill_load_rejected_under_multi_tenant(monkeypatch):
    # 多租户下 skill_view 一个 plugin:skill 限定名 -> 拒绝，不 discover/serve
    # 真实注册 handler 是 _skill_view_with_bump(args, **kw)（skills_tool.py:1606/:1635）
    monkeypatch.setattr("gateway.multi_tenant.multi_tenant_enabled", lambda: True)
    out = st._skill_view_with_bump({"name": "superpowers:writing-plans"})
    data = json.loads(out)
    assert data.get("status") == "error" or "not available" in json.dumps(data).lower()
