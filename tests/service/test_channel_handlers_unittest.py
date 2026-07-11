from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pydantic  # noqa: F401
except ModuleNotFoundError:
    fake_types = types.ModuleType("service.types")

    class _Message:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    fake_types.SyncPullMessage = _Message
    fake_types.SyncPatchMessage = _Message
    fake_types.ProviderRequest = _Message
    fake_types.CommandMessage = _Message
    sys.modules["service.types"] = fake_types

from service.channels.provider import ProviderChannelHandler
from service.channels.remote import RemoteChannelHandler
from service.channels.sync import SyncChannelHandler
from service.channels.trigger import TriggerChannelHandler
from service.transport import ChannelClosed
from service.transport.in_memory_endpoint import InMemoryChannelEndpoint


class FakeConfigManager:
    def __init__(self) -> None:
        self.unsubscribe_called = False
        self.applied = []

    async def subscribe_updates(self):
        return asyncio.Queue()

    def unsubscribe_updates(self, queue) -> None:
        self.unsubscribe_called = True

    async def get_snapshot(self, resource, resource_id):
        return SimpleNamespace(timestamp=11, data={"resource": resource, "id": resource_id})

    async def get_config_list(self):
        return SimpleNamespace(timestamp=12, data=["default_config"])

    async def apply_patch(self, resource, resource_id, ops, timestamp, origin):
        self.applied.append((resource, resource_id, ops, timestamp, origin))

    async def get_static_snapshot(self):
        return SimpleNamespace(timestamp=13, data={"static": True})


class FakeLogManager:
    def __init__(self) -> None:
        self.unsubscribe_called = False

    def get_history(self):
        return [{"scope": "global", "time": "t", "level": "info", "message": "hello"}]

    def get_scopes(self):
        return ["global"]

    async def subscribe(self):
        return asyncio.Queue()

    def unsubscribe(self, queue) -> None:
        self.unsubscribe_called = True


class FakeRuntime:
    is_all_data_initialized = False

    def __init__(self) -> None:
        self.unsubscribe_called = False

    def current_status(self):
        return {"running": False}

    async def subscribe_status(self):
        return asyncio.Queue()

    def unsubscribe_status(self, queue) -> None:
        self.unsubscribe_called = True


class FakeRemoteRuntime(FakeRuntime):
    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.requested_config_id = None

    async def require_remote_(self, config_id):
        self.requested_config_id = config_id
        return self.client


class FakeScrcpyClient:
    def __init__(self) -> None:
        self.alive = False
        self.callbacks = []
        self.initialized = False
        self.stopped = False

    def set_proxy_callbacks(self, ws_to_adb=None, adb_to_ws=None):
        self.callbacks.append((ws_to_adb, adb_to_ws))

    async def init(self):
        self.initialized = True
        self.alive = True

    async def proxy_endpoint(self, endpoint):
        payload = await endpoint.recv_bytes()
        await endpoint.send_json({"type": "remote_ack", "size": len(payload)})
        await endpoint.send_bytes(b"video")

    async def stop(self):
        self.stopped = True
        self.alive = False


class FakeScrcpyProxySession:
    def __init__(self, client, stream, *, encrypt_adb_to_ws: bool) -> None:
        self.client = client
        self.stream = stream
        self.encrypt_adb_to_ws = encrypt_adb_to_ws

    async def run_endpoint(self, endpoint):
        self.client.set_proxy_callbacks(None, None)
        if not self.client.alive:
            await self.client.init()
        await self.client.proxy_endpoint(endpoint)

    async def close(self):
        self.client.set_proxy_callbacks(None, None)
        if self.client.alive:
            await self.client.stop()


class ChannelHandlerTests(unittest.TestCase):
    def test_sync_handler_uses_transport_neutral_endpoint(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            config_manager = FakeConfigManager()
            context = SimpleNamespace(config_manager=config_manager)
            await endpoint.queue_json({"type": "list"})
            await endpoint.queue_json({"type": "pull", "resource": "config", "resource_id": "default_config"})
            await endpoint.queue_json(
                {
                    "type": "patch",
                    "resource": "config",
                    "resource_id": "default_config",
                    "timestamp": 22,
                    "ops": [{"op": "replace", "path": "/enabled", "value": True}],
                }
            )
            await endpoint.queue_close()

            try:
                await SyncChannelHandler(context).handle(endpoint)
            except ChannelClosed:
                pass

            return endpoint.sent, config_manager

        sent, config_manager = asyncio.run(scenario())

        self.assertEqual(sent[0], ("json", {"type": "config_list", "timestamp": 12, "data": ["default_config"]}))
        self.assertEqual(sent[1][1]["type"], "snapshot")
        self.assertEqual(sent[2][1]["type"], "patch_ack")
        self.assertEqual(config_manager.applied[0][4], "frontend")
        self.assertTrue(config_manager.unsubscribe_called)

    def test_sync_handler_test_resource_does_not_touch_config_manager(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            config_manager = FakeConfigManager()
            context = SimpleNamespace(config_manager=config_manager)
            await endpoint.queue_json({"type": "pull", "resource": "transport_test", "resource_id": "case"})
            await endpoint.queue_json(
                {
                    "type": "patch",
                    "resource": "transport_test",
                    "resource_id": "case",
                    "timestamp": 33,
                    "ops": [{"op": "replace", "path": "/enabled", "value": True}],
                }
            )
            await endpoint.queue_close()

            with patch.dict("os.environ", {"BAAS_SHM_TEST_COMMANDS": "1"}):
                try:
                    await SyncChannelHandler(context).handle(endpoint)
                except ChannelClosed:
                    pass
            return endpoint.sent, config_manager

        sent, config_manager = asyncio.run(scenario())

        self.assertEqual(sent[0][1]["type"], "snapshot")
        self.assertEqual(sent[0][1]["data"], {"transport": "shared-memory", "resource_id": "case"})
        self.assertEqual(sent[1][1]["type"], "patch_ack")
        self.assertEqual(sent[1][1]["timestamp"], 33)
        self.assertEqual(config_manager.applied, [])
        self.assertTrue(config_manager.unsubscribe_called)

    def test_provider_handler_sends_initial_state_and_requests_without_websocket(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            context = SimpleNamespace(
                config_manager=FakeConfigManager(),
                log_manager=FakeLogManager(),
                runtime=FakeRuntime(),
            )
            await endpoint.queue_json({"type": "static_request"})
            await endpoint.queue_json({"type": "status_request"})
            await endpoint.queue_close()

            try:
                await ProviderChannelHandler(context).handle(endpoint)
            except ChannelClosed:
                pass

            return endpoint.sent, context

        sent, context = asyncio.run(scenario())

        self.assertEqual(sent[0][1]["type"], "logs_full")
        self.assertEqual(sent[1], ("json", {"type": "status", "status": {"running": False}}))
        self.assertEqual(sent[2], ("json", {"type": "static_snapshot", "timestamp": 13, "data": {"static": True}}))
        self.assertEqual(sent[3], ("json", {"type": "status", "status": {"running": False}}))
        self.assertTrue(context.log_manager.unsubscribe_called)
        self.assertTrue(context.runtime.unsubscribe_called)

    def test_trigger_handler_preserves_binary_request_and_response_semantics(self) -> None:
        async def fake_execute_command(cmd, binary_payload=None):
            if cmd.command == "import_config":
                self.assertEqual(binary_payload, b"archive")
                return {"status": "ok", "data": {"imported": True}}
            if cmd.command == "export_config":
                return {"status": "ok", "data": {"name": "Export"}, "_binary": b"zip"}
            raise AssertionError(cmd.command)

        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            await endpoint.queue_json(
                {"type": "command", "command": "import_config", "timestamp": 31, "payload": {"binary": True}}
            )
            await endpoint.queue_bytes(b"archive")
            await endpoint.queue_json({"type": "command", "command": "export_config", "timestamp": 32, "payload": {}})
            await endpoint.queue_close()

            with patch("service.channels.trigger.execute_command", fake_execute_command):
                try:
                    await TriggerChannelHandler(SimpleNamespace()).handle(endpoint)
                except ChannelClosed:
                    pass
            return endpoint.sent

        sent = asyncio.run(scenario())

        self.assertEqual(sent[0][1]["timestamp"], 31)
        self.assertEqual(sent[0][1]["data"], {"imported": True})
        self.assertEqual(sent[1][1]["data"]["binary"], {"size": 3})
        self.assertEqual(sent[2], ("bytes", b"zip"))

    def test_trigger_handler_test_stream_preserves_done_and_error_semantics(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            await endpoint.queue_json(
                {"type": "command", "command": "transport_stream_test", "timestamp": 51, "payload": {}}
            )
            await endpoint.queue_json(
                {"type": "command", "command": "transport_stream_error", "timestamp": 52, "payload": {}}
            )
            await endpoint.queue_close()

            with patch.dict("os.environ", {"BAAS_SHM_TEST_COMMANDS": "1"}):
                try:
                    await TriggerChannelHandler(SimpleNamespace()).handle(endpoint)
                except ChannelClosed:
                    pass
            return endpoint.sent

        sent = asyncio.run(scenario())

        self.assertEqual(
            [entry[1] for entry in sent[:3]],
            [
                {
                    "type": "command_response",
                    "command": "transport_stream_test",
                    "status": "ok",
                    "data": {"chunk": 0},
                    "timestamp": 51,
                },
                {
                    "type": "command_response",
                    "command": "transport_stream_test",
                    "status": "ok",
                    "data": {"chunk": 1},
                    "timestamp": 51,
                },
                {
                    "type": "command_response",
                    "command": "transport_stream_test",
                    "status": "ok",
                    "data": {"done": True},
                    "timestamp": 51,
                },
            ],
        )
        self.assertEqual(sent[3][1]["status"], "error")
        self.assertEqual(sent[3][1]["data"], {"done": True})
        self.assertEqual(sent[3][1]["timestamp"], 52)

    def test_remote_handler_uses_transport_neutral_endpoint(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            client = FakeScrcpyClient()
            runtime = FakeRemoteRuntime(client)
            await endpoint.queue_json({"config_id": "default_config"})
            await endpoint.queue_bytes(b"ctl")

            fake_remote = SimpleNamespace(ScrcpyProxySession=FakeScrcpyProxySession)
            with patch.dict(sys.modules, {"service.remote": fake_remote}):
                await RemoteChannelHandler(SimpleNamespace(runtime=runtime)).handle(endpoint)
            return endpoint.sent, runtime, client

        sent, runtime, client = asyncio.run(scenario())

        self.assertEqual(runtime.requested_config_id, "default_config")
        self.assertTrue(client.initialized)
        self.assertTrue(client.stopped)
        self.assertEqual(client.callbacks[-1], (None, None))
        self.assertEqual(sent[0], ("json", {"type": "remote_ack", "size": 3}))
        self.assertEqual(sent[1], ("bytes", b"video"))

    def test_remote_transport_test_supports_configurable_frame_size(self) -> None:
        async def scenario():
            endpoint = InMemoryChannelEndpoint()
            await endpoint.queue_json(
                {
                    "config_id": "transport_remote_test",
                    "frame_count": 3,
                    "frame_size": 128,
                }
            )
            await endpoint.queue_bytes(b"ctl")

            with patch.dict(os.environ, {"BAAS_SHM_TEST_COMMANDS": "1"}):
                await RemoteChannelHandler(SimpleNamespace(runtime=SimpleNamespace())).handle(endpoint)
            closed = False
            try:
                await endpoint.recv_json()
            except ChannelClosed:
                closed = True
            return endpoint.sent, closed

        sent, closed = asyncio.run(scenario())

        self.assertEqual(sent[0], ("json", {"type": "remote_ack", "size": 3}))
        byte_payloads = [payload for kind, payload in sent if kind == "bytes"]
        self.assertEqual(len(byte_payloads), 3)
        self.assertTrue(all(len(payload) == 128 for payload in byte_payloads))
        self.assertTrue(byte_payloads[0].startswith(b"remote-frame-00000000:"))
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
