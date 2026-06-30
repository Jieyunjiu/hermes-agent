from unittest.mock import patch
import gateway.multi_tenant as mt


def test_sandbox_config_reads_block():
    # _config_multi_tenant() 返回 security.multi_tenant 块；sandbox_config 取其 .sandbox
    with patch.object(mt, "_config_multi_tenant",
                      return_value={"enabled": True, "sandbox": {"enabled": True, "memory_mb": 4096}}):
        cfg = mt.sandbox_config()
    assert cfg.get("enabled") is True
    assert cfg.get("memory_mb") == 4096


def test_sandbox_config_missing_returns_empty():
    with patch.object(mt, "_config_multi_tenant", return_value={}):
        assert mt.sandbox_config() == {}


def test_sandbox_enabled_requires_both_flags():
    with patch.object(mt, "multi_tenant_enabled", return_value=True), \
         patch.object(mt, "sandbox_config", return_value={"enabled": True}):
        assert mt.sandbox_enabled() is True
    with patch.object(mt, "multi_tenant_enabled", return_value=False), \
         patch.object(mt, "sandbox_config", return_value={"enabled": True}):
        assert mt.sandbox_enabled() is False
