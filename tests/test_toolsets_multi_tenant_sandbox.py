from toolsets import TOOLSETS

EXPECTED = {
    "read_file", "write_file", "patch", "search_files",
    "terminal", "execute_code",
    "skills_list", "skill_view",
    "vision_analyze", "memory", "session_search", "todo", "clarify",
}
FORBIDDEN = {"process", "read_terminal", "web_search", "web_extract", "skill_manage"}

def test_sandbox_toolset_is_exactly_13():
    tools = set(TOOLSETS["wecom_multi_tenant_sandbox"]["tools"])
    assert tools == EXPECTED, tools ^ EXPECTED

def test_sandbox_toolset_excludes_forbidden():
    tools = set(TOOLSETS["wecom_multi_tenant_sandbox"]["tools"])
    assert tools & FORBIDDEN == set()
