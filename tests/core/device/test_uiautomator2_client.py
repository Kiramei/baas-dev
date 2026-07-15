from types import SimpleNamespace

import pytest

from core.device import uiautomator2_client


def make_initer():
    initer = uiautomator2_client.BAAS_U2_Initer.__new__(
        uiautomator2_client.BAAS_U2_Initer
    )
    initer._device = SimpleNamespace(serial="emulator-5556")
    initer.logger = SimpleNamespace(info=lambda _message: None)
    initer.shell = lambda *_args: None
    return initer


def test_install_uiautomator_apks_uses_streaming_adb(monkeypatch, tmp_path):
    apk = tmp_path / "app-uiautomator.apk"
    apk.write_bytes(b"apk")
    calls = []

    monkeypatch.setattr(uiautomator2_client, "adb_path", lambda: "adb")
    monkeypatch.setattr(
        uiautomator2_client,
        "app_uiautomator_apk_local_path",
        lambda: [(apk.name, str(apk))],
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="Performing Streamed Install\nSuccess\n")

    monkeypatch.setattr(uiautomator2_client.subprocess, "run", run)

    make_initer()._install_uiautomator_apks()

    assert calls[0][0] == [
        "adb",
        "-s",
        "emulator-5556",
        "install",
        "-r",
        "-t",
        str(apk),
    ]


def test_install_uiautomator_apks_reports_adb_failure(monkeypatch, tmp_path):
    apk = tmp_path / "app-uiautomator.apk"
    apk.write_bytes(b"apk")

    monkeypatch.setattr(uiautomator2_client, "adb_path", lambda: "adb")
    monkeypatch.setattr(
        uiautomator2_client,
        "app_uiautomator_apk_local_path",
        lambda: [(apk.name, str(apk))],
    )
    monkeypatch.setattr(
        uiautomator2_client.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="Failure [INSTALL_FAILED_MEDIA_UNAVAILABLE]",
        ),
    )

    with pytest.raises(RuntimeError, match="INSTALL_FAILED_MEDIA_UNAVAILABLE"):
        make_initer()._install_uiautomator_apks()
