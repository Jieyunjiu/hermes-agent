"""Test wecom_multi_tenant_sandbox toolset configuration."""

from toolsets import resolve_toolset


def test_sandbox_toolset_adds_terminal():
    """Verify sandbox toolset = base + terminal, process not included."""
    base = set(resolve_toolset("wecom_multi_tenant"))
    sandbox = set(resolve_toolset("wecom_multi_tenant_sandbox"))

    assert "terminal" not in base, "Base wecom_multi_tenant should not contain terminal"
    assert "terminal" in sandbox, "Sandbox should contain terminal"
    assert base.issubset(sandbox), "Sandbox should be superset of base"
    assert "process" not in sandbox, "Process should not be in sandbox toolset"
