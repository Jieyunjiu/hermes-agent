from agent.prompt_builder import build_capability_manifest


def test_manifest_minimal_english_facts():
    m = build_capability_manifest()
    assert "isolated" in m and "/workspace" in m
    assert "no network" in m.lower()
    assert "/workspace/uploads/" in m
    assert build_capability_manifest() == m  # 纯函数，稳定
