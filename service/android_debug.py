from __future__ import annotations

import os
from pathlib import Path
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def _setup_debug_enabled() -> bool:
    path = Path.cwd() / "setup.toml"
    if not path.exists():
        return False
    in_general = False
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                in_general = line.strip("[]").strip().lower() == "general"
                continue
            if in_general and line.lower().startswith("debug"):
                _, _, value = line.partition("=")
                return value.strip().strip('"').strip("'").lower() in _TRUE_VALUES
    except OSError:
        return False
    return False


def android_debug_enabled() -> bool:
    if _env_enabled("BAAS_ANDROID_DEBUG"):
        return True
    if not _env_enabled("BAAS_ANDROID"):
        return False
    return _setup_debug_enabled()


def android_debug_log(logger: Any, marker: str, **fields: Any) -> None:
    if not android_debug_enabled():
        return
    details = " ".join(f"{key}={value!r}" for key, value in fields.items())
    message = f"[ANDROID-DEBUG] {marker}"
    if details:
        message = f"{message} {details}"
    if logger is not None:
        try:
            logger.info(message)
        except Exception:
            pass
    print(message, flush=True)
