from __future__ import annotations

import sys
from types import SimpleNamespace

from service.runtime import _AndroidDisplayResizeGuard


class _FakeDevice:
    def __init__(self, calls, *, cutout=False, unknown_cutout_command=False):
        self.calls = calls
        self.cutout = cutout
        self.unknown_cutout_command = unknown_cutout_command

    def shell(self, command: str):
        self.calls.append(command)
        if command == "cmd window get-display-cutout":
            if self.unknown_cutout_command:
                return "Unknown command: get-display-cutout"
            return "DisplayCutout{boundingRect=Rect(0, 0 - 120, 40)}" if self.cutout else "NO-CUTOUT"
        if command == "dumpsys window displays":
            return "mDisplayCutout=DisplayCutout{boundingRect={Bounds=[Rect(844, 0 - 1080, 95)]}}" if self.cutout else ""
        if command == "wm size":
            return "Physical size: 1080x2400"
        return ""


class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


def test_android_display_guard_is_noop_outside_android(monkeypatch):
    monkeypatch.delenv("BAAS_ANDROID", raising=False)
    guard = _AndroidDisplayResizeGuard()

    guard.activate()
    guard.release()


def test_android_display_guard_does_not_resize_without_explicit_target(monkeypatch):
    calls = []

    def connect(target):
        assert target == "http://127.0.0.1:7912"
        return _FakeDevice(calls)

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.delenv("BAAS_ANDROID_WM_SIZE", raising=False)
    monkeypatch.delenv("BAAS_ANDROID_U2_SERIAL", raising=False)
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))

    guard = _AndroidDisplayResizeGuard()
    guard.activate()
    guard.release()

    assert calls == []


def test_android_display_guard_sets_and_resets_explicit_size(monkeypatch):
    calls = []

    def connect(target):
        assert target == "http://127.0.0.1:7912"
        return _FakeDevice(calls)

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "720x1280")
    monkeypatch.delenv("BAAS_ANDROID_U2_SERIAL", raising=False)
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))

    guard = _AndroidDisplayResizeGuard()
    guard.activate()
    guard.release()

    assert calls == ["cmd window get-display-cutout", "wm size", "wm size 720x1280"]


def test_android_display_guard_resets_only_when_enabled(monkeypatch):
    calls = []

    def connect(target):
        assert target == "http://127.0.0.1:7912"
        return _FakeDevice(calls)

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "720x1280")
    monkeypatch.setenv("BAAS_ANDROID_WM_RESET_ON_RELEASE", "1")
    monkeypatch.delenv("BAAS_ANDROID_U2_SERIAL", raising=False)
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))

    guard = _AndroidDisplayResizeGuard()
    guard.activate()
    guard.release()

    assert calls == ["cmd window get-display-cutout", "wm size", "wm size 720x1280", "wm size reset"]


def test_android_display_guard_uses_reference_count(monkeypatch):
    calls = []

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "800x1280")
    monkeypatch.setenv("BAAS_ANDROID_U2_SERIAL", "http://localhost:7912")
    monkeypatch.setitem(
        sys.modules,
        "uiautomator2",
        SimpleNamespace(connect=lambda _target: _FakeDevice(calls)),
    )

    guard = _AndroidDisplayResizeGuard()
    guard.activate()
    guard.activate()
    guard.release()
    guard.release()

    assert calls == ["cmd window get-display-cutout", "wm size", "wm size 800x1280"]


def test_android_display_guard_force_restore_ignores_reference_count(monkeypatch):
    calls = []

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setitem(
        sys.modules,
        "uiautomator2",
        SimpleNamespace(connect=lambda _target: _FakeDevice(calls)),
    )

    guard = _AndroidDisplayResizeGuard()
    guard.activate()
    guard.activate()
    guard.force_restore()

    assert guard._active_count == 0
    assert calls == []


def test_android_display_guard_skips_cutout_device(monkeypatch):
    calls = []

    def connect(_target):
        return _FakeDevice(calls, cutout=True)

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "720x1280")
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))

    logger = _FakeLogger()
    guard = _AndroidDisplayResizeGuard()
    guard.activate(logger)
    guard.release(logger)

    assert calls == ["cmd window get-display-cutout"]
    assert "cutout/notch" in logger.warnings[0]


def test_android_display_guard_uses_dumpsys_cutout_fallback(monkeypatch):
    calls = []

    def connect(_target):
        return _FakeDevice(calls, cutout=True, unknown_cutout_command=True)

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "720x1280")
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))

    logger = _FakeLogger()
    guard = _AndroidDisplayResizeGuard()
    guard.activate(logger)

    assert calls == ["cmd window get-display-cutout", "dumpsys window displays"]
    assert "cutout/notch" in logger.warnings[0]


def test_android_display_guard_does_not_block_when_resize_fails(monkeypatch):
    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setenv("BAAS_ANDROID_WM_SIZE", "720x1280")

    def fail_shell(_command):
        raise RuntimeError("No adb exe could be found. Install adb on your system")

    monkeypatch.setattr(_AndroidDisplayResizeGuard, "_shell", staticmethod(fail_shell))

    logger = _FakeLogger()
    guard = _AndroidDisplayResizeGuard()

    guard.activate(logger)
    guard.release(logger)

    assert guard._active_count == 0
    assert "continue without resizing" in logger.warnings[0]
