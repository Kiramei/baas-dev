import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from service.remote.scrcpy import ScrcpyClient


def test_deploy_waits_for_server_readiness(monkeypatch):
    client = ScrcpyClient.__new__(ScrcpyClient)
    client.device = SimpleNamespace(sync=SimpleNamespace(push=lambda *_args: None))
    client._ScrcpyClient__server_pid = None
    monkeypatch.setattr(client, "_ScrcpyClient__find_expected_server_pids", lambda: [])
    monkeypatch.setattr(client, "_ScrcpyClient__build_server_command", lambda: "start")

    def shell(command, timeout=None):
        return "started" if command == "start" else ""

    ready = False

    def wait_ready():
        nonlocal ready
        time.sleep(0.08)
        ready = True
        return 123

    monkeypatch.setattr(client, "_ScrcpyClient__shell", shell)
    monkeypatch.setattr(client, "_ScrcpyClient__wait_server_ready", wait_ready)
    server_jar = Path(__file__).parents[2] / "service" / "remote" / "scrcpy-server.jar"
    assert server_jar.exists()

    asyncio.run(client._ScrcpyClient__deploy_server())
    assert ready is True


def test_server_connection_retries_invalid_handshake(monkeypatch):
    client = ScrcpyClient.__new__(ScrcpyClient)
    client.connection_timeout = 500
    client.device = SimpleNamespace(
        serial="127.0.0.1:5557",
        forward_list=lambda: [
            SimpleNamespace(serial="emulator-5554", remote="tcp:8886", local="tcp:10461"),
            SimpleNamespace(serial="127.0.0.1:5557", remote="tcp:8886", local="tcp:18886"),
        ],
        forward=lambda *_args: None,
    )
    client._ScrcpyClient__remote_socket = None
    attempts = 0
    urls = []
    expected_socket = object()

    async def connect(url, **_kwargs):
        nonlocal attempts
        attempts += 1
        urls.append(url)
        if attempts == 1:
            raise RuntimeError("did not receive a valid HTTP response")
        return expected_socket

    monkeypatch.setattr("service.remote.scrcpy.websockets.connect", connect)
    asyncio.run(client._ScrcpyClient__init_server_connection())

    assert attempts == 2
    assert urls == ["ws://127.0.0.1:18886", "ws://127.0.0.1:18886"]
    assert client.control_socket is expected_socket
