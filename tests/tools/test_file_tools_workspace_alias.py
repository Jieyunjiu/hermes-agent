# tests/tools/test_file_tools_workspace_alias.py
from pathlib import Path
from unittest.mock import patch
import tools.file_tools as ft


def test_workspace_alias_maps_to_owner_root(tmp_path):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        p = ft._resolve_path_for_task("/workspace/uploads/x.xlsx")
    assert p == (owner_root / "uploads/x.xlsx").resolve()


def test_relative_still_maps_to_owner_root(tmp_path):
    owner_root = tmp_path / "owner"; owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        p = ft._resolve_path_for_task("report.csv")
    assert p == (owner_root / "report.csv").resolve()


def test_workspace_alias_traversal_rejected(tmp_path):
    """/workspace/../../etc/passwd 归一化后越界，validate 必须判错。"""
    owner_root = tmp_path / "owner"; owner_root.mkdir()
    with patch.object(ft, "_multi_tenant_workspace_root", return_value=owner_root):
        target = ft._resolve_path_for_task("/workspace/../../etc/passwd")
        err = ft._validate_multi_tenant_workspace_path(target)
    assert err is not None  # 越界被拒
