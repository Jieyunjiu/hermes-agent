from unittest.mock import patch
import gateway.multi_tenant as mt


def test_constrain_returns_sandbox_when_enabled():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_enabled", return_value=True):
        enabled, disabled = mt.constrain_toolsets_for_owner(None, None, owner_key="wecom:c:a:u")
    assert enabled == ["wecom_multi_tenant_sandbox"]


def test_constrain_returns_restricted_when_sandbox_off():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_enabled", return_value=False):
        enabled, _ = mt.constrain_toolsets_for_owner(None, None, owner_key="wecom:c:a:u")
    assert enabled == ["wecom_multi_tenant"]
