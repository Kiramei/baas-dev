from __future__ import annotations

import pytest

from service import android_local_device


def test_android_control_backend_requires_accessibility_by_default(monkeypatch):
    monkeypatch.delenv("BAAS_ANDROID_ENABLE_UIAUTOMATOR_FALLBACK", raising=False)
    monkeypatch.setattr(android_local_device, "android_accessibility_ready", lambda: False)

    def fail_agent(_logger=None):
        raise AssertionError("uiautomator fallback should not start by default")

    monkeypatch.setattr(android_local_device, "ensure_android_local_agent", fail_agent)

    with pytest.raises(RuntimeError, match="Enable the BAAS accessibility service"):
        android_local_device.android_control_backend()


def test_android_control_backend_allows_explicit_uiautomator_fallback(monkeypatch):
    calls = []
    monkeypatch.setenv("BAAS_ANDROID_ENABLE_UIAUTOMATOR_FALLBACK", "1")
    monkeypatch.setattr(android_local_device, "android_accessibility_ready", lambda: False)
    monkeypatch.setattr(android_local_device, "ensure_android_local_agent", lambda _logger=None: calls.append("agent"))
    monkeypatch.setattr(android_local_device, "_jsonrpc_is_alive", lambda timeout=1.0: True)

    assert android_local_device.android_control_backend() == "uiautomator"
    assert calls == ["agent"]
