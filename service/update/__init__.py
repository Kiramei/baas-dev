from __future__ import annotations

from importlib import import_module

from .setup_io import read_setup_toml, write_setup_toml

_CHECK_EXPORTS = {
    "GitOperationHandler",
    "VersionInfo",
    "check_for_update",
    "get_local_version",
    "repo_sha_test_configs",
    "test_all_repo_sha",
    "test_repo_sha",
    "update_to_latest",
    "update_to_latest_with_progress",
    "validate_cdk",
    "_setup_channel",
}

__all__ = sorted(_CHECK_EXPORTS | {"checks", "read_setup_toml", "write_setup_toml"})


def __getattr__(name: str):
    if name == "checks":
        return import_module(".checks", __name__)
    if name in _CHECK_EXPORTS:
        checks = import_module(".checks", __name__)
        return getattr(checks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
