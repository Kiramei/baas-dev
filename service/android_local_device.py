from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import time
from urllib import request

import cv2
import numpy as np
from PIL import Image
from PIL import UnidentifiedImageError

from service.android_modes import ANDROID_LOCAL_METHOD
from service.android_debug import android_debug_log
from core.device.uiautomator2_client import U2Client

_ANDROID_LAUNCHER_ACTIVITIES = {
    "com.RoamingStar.BlueArchive": "com.yostar.supersdk.activity.YoStarSplashActivity",
    "com.RoamingStar.BlueArchive.bilibili": "com.yostar.supersdk.activity.YoStarSplashActivity",
}
_ANDROID_RAW_SCREEN_SIZE: tuple[int, int] | None = None


def _agent_url() -> str:
    """Return the loopback URL used by the embedded atx-agent."""
    return "http://127.0.0.1:7912/version"


def _uiautomator_fallback_enabled() -> bool:
    """Return whether Android embedded mode may use the atx/uiautomator fallback."""
    return os.getenv("BAAS_ANDROID_ENABLE_UIAUTOMATOR_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _agent_is_alive(timeout: float = 1.0) -> bool:
    """Return whether the embedded atx-agent is accepting requests."""
    try:
        with request.urlopen(_agent_url(), timeout=timeout) as response:
            return bool(response.read().strip())
    except Exception:
        return False


def _jsonrpc_is_alive(timeout: float = 1.0) -> bool:
    """Return whether the local UIAutomator JSON-RPC endpoint can handle device calls."""
    body = b'{"jsonrpc":"2.0","id":"health","method":"deviceInfo","params":[]}'
    req = request.Request(
        "http://127.0.0.1:7912/jsonrpc/0",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status == 200 and b'"result"' in response.read()
    except Exception:
        return False


def _accessibility_bridge():
    """Return the Android accessibility bridge class when running inside Chaquopy."""
    try:
        from java import jclass
    except Exception:
        return None
    try:
        return jclass("io.github.kiramei.baas_tauri.BaasAccessibilityBridge")
    except Exception:
        return None


def android_accessibility_ready() -> bool:
    """Return whether the BAAS Android accessibility service is enabled and connected."""
    bridge = _accessibility_bridge()
    if bridge is None:
        return False
    try:
        return bool(bridge.isReady())
    except Exception:
        return False


def android_current_package() -> str:
    """Return the current foreground package reported by the native accessibility bridge."""
    bridge = _accessibility_bridge()
    if bridge is None:
        return ""
    try:
        return str(bridge.currentPackageName() or "")
    except Exception:
        return ""


def android_active_window_bounds() -> tuple[int, int, int, int] | None:
    """Return the active Android window bounds reported by AccessibilityService."""
    bridge = _accessibility_bridge()
    if bridge is None:
        return None
    try:
        payload = str(bridge.activeWindowBounds() or "").strip()
    except Exception:
        return None
    if not payload:
        return None
    try:
        left, top, right, bottom = [int(part.strip()) for part in payload.split(",", 3)]
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def android_safe_display_bounds() -> tuple[int, int, int, int] | None:
    """Return display bounds after Android cutout and navigation insets are removed."""
    bridge = _accessibility_bridge()
    if bridge is None:
        return None
    try:
        payload = str(bridge.safeDisplayBounds() or "").strip()
    except Exception:
        return None
    if not payload:
        return None
    try:
        left, top, right, bottom = [int(part.strip()) for part in payload.split(",", 3)]
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _remember_raw_screen_size(width: int, height: int) -> None:
    """Store the unrotated accessibility screenshot size for gesture coordinates."""
    global _ANDROID_RAW_SCREEN_SIZE
    _ANDROID_RAW_SCREEN_SIZE = (width, height)


def _to_accessibility_point(x: int, y: int) -> tuple[int, int]:
    """Convert BAAS landscape coordinates to AccessibilityService raw display coordinates."""
    if _ANDROID_RAW_SCREEN_SIZE is None:
        return int(x), int(y)
    raw_width, raw_height = _ANDROID_RAW_SCREEN_SIZE
    if raw_height <= raw_width:
        return (
            int(max(0, min(raw_width - 1, x))),
            int(max(0, min(raw_height - 1, y))),
        )
    # takeScreenshot returns the natural portrait buffer while BAAS works on
    # the clockwise-rotated landscape image.
    return (
        int(max(0, min(raw_width - 1, y))),
        int(max(0, min(raw_height - 1, raw_height - 1 - x))),
    )


def _internal_files_dir() -> str:
    """Return the Android app internal files directory used for executable helpers."""
    return os.getenv("BAAS_ANDROID_INTERNAL_FILES_DIR", "").strip() or "/data/data/io.github.kiramei.baas_tauri/files"


def _internal_agent_path() -> str:
    """Return the executable atx-agent path inside the app sandbox."""
    return os.path.join(_internal_files_dir(), "atx-agent")


def _prepare_internal_agent(logger=None) -> str:
    """Copy atx-agent into app-owned storage so Android allows the app UID to execute it."""
    target = _internal_agent_path()
    source = os.getenv("BAAS_ANDROID_ATX_AGENT_PATH", "").strip() or "/data/local/tmp/atx-agent"
    if os.path.exists(target):
        os.chmod(target, 0o700)
        return target
    if not os.path.exists(source):
        raise RuntimeError(f"Android atx-agent binary not found: {source}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target, 0o700)
    if logger is not None:
        logger.info(f"Prepared Android atx-agent at {target}.")
    return target


def ensure_android_local_agent(logger=None, timeout: float = 8.0) -> None:
    """Ensure the Android-local atx-agent is running before UIAutomator calls are made."""
    if _agent_is_alive():
        android_debug_log(logger, "android.agent.already_alive")
        return
    agent = _prepare_internal_agent(logger)
    if logger is not None:
        logger.info("Starting Android local atx-agent.")
    android_debug_log(logger, "android.agent.start.begin", agent=agent)
    subprocess.Popen(
        [agent, "server", "--nouia", "-d"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _agent_is_alive():
            if logger is not None:
                logger.info("Android local atx-agent is ready.")
            android_debug_log(logger, "android.agent.start.ready")
            return
        time.sleep(0.2)
    android_debug_log(logger, "android.agent.start.timeout", timeout=timeout)
    raise RuntimeError("Android local atx-agent did not start on 127.0.0.1:7912")


def android_control_backend(logger=None) -> str:
    """Return the available Android local control backend or raise an actionable error."""
    android_debug_log(logger, "android.control_backend.probe")
    if android_accessibility_ready():
        if logger is not None:
            logger.info("Android accessibility control backend is ready.")
        android_debug_log(logger, "android.control_backend.accessibility")
        return "accessibility"
    if not _uiautomator_fallback_enabled():
        android_debug_log(logger, "android.control_backend.not_ready", fallback_enabled=False)
        raise RuntimeError(
            "Android local control is not ready. Enable the BAAS accessibility service in Android settings."
        )
    ensure_android_local_agent(logger)
    if _jsonrpc_is_alive(timeout=1.5):
        if logger is not None:
            logger.info("Android UIAutomator control backend is ready.")
        android_debug_log(logger, "android.control_backend.uiautomator")
        return "uiautomator"
    android_debug_log(logger, "android.control_backend.not_ready", fallback_enabled=True, jsonrpc=False)
    raise RuntimeError(
        "Android local control is not ready. Enable the BAAS accessibility service in Android settings, "
        "or start uiautomator instrumentation from adb for development."
    )


def start_android_activity(package_name: str, activity_name: str | None = None, logger=None) -> None:
    """Start an Android application through the embedded app context instead of shell am."""
    try:
        from java import jclass
    except Exception as exc:
        raise RuntimeError("Android native activity launch requires Chaquopy Java bindings") from exc

    Python = jclass("com.chaquo.python.Python")
    Intent = jclass("android.content.Intent")

    context = Python.getPlatform().getApplication()
    package_manager = context.getPackageManager()
    launcher_activity = _ANDROID_LAUNCHER_ACTIVITIES.get(package_name)
    activity = launcher_activity or activity_name

    intent = None
    if activity:
        intent = Intent(Intent.ACTION_MAIN)
        intent.addCategory(Intent.CATEGORY_LAUNCHER)
        intent.setClassName(package_name, activity)
    if intent is None:
        intent = package_manager.getLaunchIntentForPackage(package_name)
    if intent is None:
        raise RuntimeError(f"Unable to build Android launch intent for {package_name}")

    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    intent.addFlags(Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
    if logger is not None:
        logger.info(f"Starting Android activity via native intent: {package_name}/{activity or '<launcher>'}")
    android_debug_log(
        logger,
        "android.activity.start",
        package=package_name,
        activity=activity or "<launcher>",
    )
    try:
        context.startActivity(intent)
        return
    except Exception as exc:
        if activity is None:
            raise
        if logger is not None:
            logger.warning(
                "Explicit Android activity launch failed; falling back to package launch intent: "
                + str(exc)
            )
        android_debug_log(
            logger,
            "android.activity.start.explicit_failed",
            package=package_name,
            activity=activity,
            error=repr(exc),
        )

    fallback_intent = package_manager.getLaunchIntentForPackage(package_name)
    if fallback_intent is None:
        raise RuntimeError(f"Unable to build Android launch intent for {package_name}")
    fallback_intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    fallback_intent.addFlags(Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
    android_debug_log(logger, "android.activity.start.fallback", package=package_name)
    context.startActivity(fallback_intent)


def _with_uiautomator_retry(u2_client, operation):
    """Runs an Android-local UIAutomator operation with one reconnect attempt."""
    try:
        return operation()
    except Exception:
        ensure_android_local_agent()
        connection = u2_client.get_connection()
        uiautomator = getattr(connection, "uiautomator", None)
        if uiautomator is not None:
            uiautomator.start()
        return operation()


class AndroidLocalControl:
    """Android-embedded control adapter backed by the local device agent."""

    def __init__(self, conn):
        self.serial = conn.serial
        self.backend = android_control_backend(getattr(conn, "logger", None))
        android_debug_log(getattr(conn, "logger", None), "android.control.init", backend=self.backend)
        self.bridge = _accessibility_bridge() if self.backend == "accessibility" else None
        self.u2 = U2Client.get_instance(self.serial) if self.backend == "uiautomator" else None

    def click(self, x, y):
        """Taps the Android screen and retries once if UIAutomator detached."""
        if self.bridge is not None:
            point_x, point_y = _to_accessibility_point(int(x), int(y))
            return self.bridge.click(point_x, point_y)
        return _with_uiautomator_retry(self.u2, lambda: self.u2.click(x, y))

    def swipe(self, x1, y1, x2, y2, duration):
        """Swipes the Android screen and retries once if UIAutomator detached."""
        if self.bridge is not None:
            from_x, from_y = _to_accessibility_point(int(x1), int(y1))
            to_x, to_y = _to_accessibility_point(int(x2), int(y2))
            return self.bridge.swipe(from_x, from_y, to_x, to_y, int(max(1, duration * 1000)))
        return _with_uiautomator_retry(self.u2, lambda: self.u2.swipe(x1, y1, x2, y2, duration))

    def long_click(self, x, y, duration):
        """Presses one Android screen point for the requested duration."""
        if self.bridge is not None:
            point_x, point_y = _to_accessibility_point(int(x), int(y))
            return self.bridge.swipe(point_x, point_y, point_x, point_y, int(max(1, duration * 1000)))
        return _with_uiautomator_retry(self.u2, lambda: self.u2.swipe(x, y, x, y, duration))

    def scroll(self, x, y, clicks):
        """Converts wheel-style scroll clicks into Android swipe gestures."""
        direction = -1 if clicks > 0 else 1
        distance = 240 * abs(clicks)
        self.swipe(x, y, x, y + direction * distance, 0.2)


class AndroidLocalScreenshot:
    """Android-embedded screenshot adapter backed by the local device agent."""

    def __init__(self, conn):
        self.serial = conn.serial
        self.backend = android_control_backend(getattr(conn, "logger", None))
        self.bridge = _accessibility_bridge() if self.backend == "accessibility" else None
        self.u2 = U2Client.get_instance(self.serial) if self.backend == "uiautomator" else None

    def screenshot(self):
        """Captures the Android screen and retries once if UIAutomator detached."""
        if self.bridge is not None:
            last_error = None
            for _ in range(3):
                try:
                    payload = self.bridge.screenshotPngBase64()
                    image = Image.open(io.BytesIO(base64.b64decode(str(payload)))).convert("RGB")
                    _remember_raw_screen_size(image.width, image.height)
                    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                except (ValueError, UnidentifiedImageError, OSError) as exc:
                    last_error = exc
                    time.sleep(0.15)
            raise RuntimeError("Android accessibility screenshot returned invalid image data") from last_error
        return _with_uiautomator_retry(self.u2, lambda: self.u2.screenshot())
