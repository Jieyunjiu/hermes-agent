from agent.prompt_builder import build_capability_manifest


def test_manifest_minimal_english_facts():
    m = build_capability_manifest()
    assert "isolated" in m and "/workspace" in m
    assert "no network" in m.lower()
    assert "/workspace/uploads/" in m
    assert build_capability_manifest() == m  # 纯函数，稳定


def test_manifest_preinstalled_libraries():
    """验证能力清单列出关键预装库，防止模型尝试安装。"""
    m = build_capability_manifest()
    # 关键库必须列出
    assert "pdfplumber" in m
    assert "python-docx" in m
    assert "pandas" in m
    # 禁止列出未装库
    assert "pymupdf" not in m
    assert "fitz" not in m
