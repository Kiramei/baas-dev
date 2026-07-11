from __future__ import annotations

__all__ = [
    "ProviderChannelHandler",
    "RemoteChannelHandler",
    "SyncChannelHandler",
    "TriggerChannelHandler",
]


def __getattr__(name: str):
    if name == "ProviderChannelHandler":
        from .provider import ProviderChannelHandler

        return ProviderChannelHandler
    if name == "RemoteChannelHandler":
        from .remote import RemoteChannelHandler

        return RemoteChannelHandler
    if name == "SyncChannelHandler":
        from .sync import SyncChannelHandler

        return SyncChannelHandler
    if name == "TriggerChannelHandler":
        from .trigger import TriggerChannelHandler

        return TriggerChannelHandler
    raise AttributeError(name)
