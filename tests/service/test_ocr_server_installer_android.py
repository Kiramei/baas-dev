import platform
import importlib
import sys
import types

from core.ocr.baas_ocr_client import server_installer


def test_android_ocr_branch_accepts_armv8_alias(monkeypatch):
    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setattr(platform, "machine", lambda: "armv8")

    assert server_installer._android_ocr_branch() == "android-arm64-v8a"


def test_android_ocr_client_accepts_armv8_alias(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace())
    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setattr(platform, "machine", lambda: "armv8l")
    android_ocr_client = importlib.import_module("service.android_ocr_client")

    assert android_ocr_client._android_ocr_branch() == "android-arm64-v8a"


def test_android_ocr_client_uses_core_prebuild_dir(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace())
    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    android_ocr_client = importlib.reload(importlib.import_module("service.android_ocr_client"))

    normalized = android_ocr_client._server_folder_path().replace("\\", "/")

    assert normalized.endswith("core/ocr/baas_ocr_client/bin-android/android-arm64-v8a")
