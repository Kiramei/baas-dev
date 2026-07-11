from __future__ import annotations

import os
import queue
import sys
import time
import traceback
import types
from datetime import datetime
from functools import wraps
from inspect import signature
from typing import Any


_APPLIED = False


def _supports_parameter(callable_obj: Any, name: str) -> bool:
    try:
        return name in signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False


def _ensure_logger_extensions(logger: Any, jsonify: bool = False) -> None:
    if not hasattr(logger, "log_collector"):
        logger.log_collector = queue.Queue()
    if not hasattr(logger, "jsonify"):
        logger.jsonify = False
    if jsonify:
        logger.jsonify = True


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _shared_memory_transport_requested() -> bool:
    if os.getenv("BAAS_SERVICE_TRANSPORT", "").strip().lower() == "shm":
        return True
    argv = list(sys.argv[1:])
    for index, value in enumerate(argv):
        if value == "--transport" and index + 1 < len(argv) and argv[index + 1] == "shm":
            return True
        if value == "--transport=shm":
            return True
    return False


def _android_logcat_mirror_enabled() -> bool:
    return os.getenv("BAAS_ANDROID", "").strip() == "1" and os.getenv(
        "BAAS_ANDROID_LOGCAT_MIRROR", "1"
    ).strip().lower() not in {"0", "false", "no", "off"}


def _mirror_android_log(level: Any, message: Any) -> None:
    if _android_logcat_mirror_enabled():
        print(f"[BAAS logger] [{level}] {message}", flush=True)


def _android_debug_log(logger: Any, marker: str, **fields: Any) -> None:
    try:
        from service.android_debug import android_debug_log
    except Exception:
        return
    android_debug_log(logger, marker, **fields)


def _install_gui_stubs() -> None:
    if "gui.util.translator" not in sys.modules:
        translator = types.ModuleType("gui.util.translator")

        class _Translator:
            @staticmethod
            def tr(_domain, value):
                return value

            @staticmethod
            def undo(value):
                return value

        translator.baasTranslator = _Translator()
        sys.modules["gui.util.translator"] = translator

    if "gui.util.customized_ui" not in sys.modules:
        customized_ui = types.ModuleType("gui.util.customized_ui")

        class BoundComponent:
            def __init__(self, component, string_rule, config_set, attribute="setText"):
                self.component = component
                self.string_rule = string_rule
                self.config_set = config_set
                self.attribute = attribute

            def config_updated(self, _key):
                return None

        customized_ui.BoundComponent = BoundComponent
        sys.modules["gui.util.customized_ui"] = customized_ui


def _install_android_ocr_modules() -> None:
    if not _env_enabled("BAAS_ANDROID"):
        return
    from service import android_ocr_client

    sys.modules["core.ocr.baas_ocr_client.Client"] = android_ocr_client


def prepare_service_imports() -> None:
    _install_gui_stubs()
    if not _shared_memory_transport_requested():
        _install_android_ocr_modules()


def _patch_logger() -> None:
    _install_gui_stubs()
    _install_android_ocr_modules()
    from core import utils

    logger_cls = utils.Logger
    if getattr(logger_cls, "_baas_service_injected", False):
        return

    original_init = logger_cls.__init__
    original_log = getattr(logger_cls, "log", None)
    original_out = getattr(logger_cls, "__out__", None)

    @wraps(original_init)
    def init(self, logger_signal, jsonify=False):
        if _supports_parameter(original_init, "jsonify"):
            original_init(self, logger_signal, jsonify=jsonify)
        else:
            original_init(self, logger_signal)
        _ensure_logger_extensions(self, jsonify=jsonify)

    logger_cls.__init__ = init
    if original_out is not None:
        @wraps(original_out)
        def out(self, message, level=1, raw_print=False):
            _ensure_logger_extensions(self)
            if getattr(self, "jsonify", False):
                self.log_collector.put({
                    "time": datetime.now(),
                    "level": level,
                    "message": str(message),
                })
                _mirror_android_log(level, message)
                return
            return original_out(self, message, level=level, raw_print=raw_print)

        logger_cls.__out__ = out
    if original_log is not None:
        @wraps(original_log)
        def log(self, level, message):
            _ensure_logger_extensions(self)
            if getattr(self, "jsonify", False):
                self.log_collector.put({
                    "time": datetime.now(),
                    "level": level,
                    "message": message,
                })
                _mirror_android_log(level, message)
                return
            return original_log(self, level, message)

        logger_cls.log = log
    logger_cls._baas_service_injected = True


def _patch_main() -> None:
    _install_gui_stubs()
    _install_android_ocr_modules()
    import main as main_module
    from core.ocr import ocr
    from core.ocr.baas_ocr_client.server_installer import check_git
    from core.utils import Logger
    from core.config.config_set import ConfigSet

    main_cls = main_module.Main
    if getattr(main_cls, "_baas_service_injected", False):
        return

    def init(self, logger_signal=None, ocr_needed=None, **kwargs):
        self.ocr_needed = ocr_needed
        self.ocr = None
        self.logger = Logger(logger_signal, jsonify=kwargs.get("jsonify", False))
        self.project_dir = os.path.abspath(os.path.dirname(main_module.__file__))
        self.logger.info(self.project_dir)
        if not kwargs.get("lazy_data", False):
            self.init_all_data()
        self.threads = {}

    def init_all_data(self, need_ocr_update_check=True):
        if not self.init_ocr(need_ocr_update_check=need_ocr_update_check):
            if os.getenv("BAAS_ALLOW_MISSING_OCR", "").strip().lower() in {"1", "true", "yes", "on"}:
                self.logger.warning("Ocr Init Incomplete. Continuing because missing OCR is allowed.")
            else:
                self.logger.error("Ocr Init Incomplete Please restart .")
                return False
        self.init_static_config()
        self.logger.info("-- All Data Initialization Complete Script ready--")
        return True

    def init_ocr(self, need_ocr_update_check=True):
        if need_ocr_update_check:
            try:
                check_git(self.logger)
            except Exception:
                self.logger.error("OCR Update Failed.")
                self.logger.error(traceback.format_exc())
                self.logger.info("Try to Start OCR Server Without Update.")
        try:
            self.ocr = ocr.Baas_ocr(logger=self.logger, ocr_needed=self.ocr_needed)
            return True
        except Exception:
            self.logger.error("OCR initialization failed")
            self.logger.error(traceback.format_exc())
            return False

    def init_static_config(self):
        try:
            if ConfigSet.static_config is None:
                ConfigSet._init_static_config()
            return True
        except Exception:
            self.logger.error("Static Config initialization failed")
            self.logger.error(traceback.format_exc())
            return False

    main_cls.__init__ = init
    main_cls.init_all_data = init_all_data
    main_cls.init_ocr = init_ocr
    if not hasattr(main_cls, "init_static_config"):
        main_cls.init_static_config = init_static_config
    main_cls._baas_service_injected = True


def _patch_baas_thread() -> None:
    _install_gui_stubs()
    _install_android_ocr_modules()
    from core.Baas_thread import Baas_thread

    if getattr(Baas_thread, "_baas_service_injected", False):
        return

    original_init = Baas_thread.__init__
    original_click_thread = Baas_thread.click_thread
    original_set_ocr = Baas_thread.set_ocr
    original_start_emulator = Baas_thread.start_emulator
    original_android_language = Baas_thread._get_android_device_ocr_language
    original_check_atx = Baas_thread.check_atx
    original_wait_uiautomator_start = Baas_thread.wait_uiautomator_start
    original_check_resolution = Baas_thread.check_resolution
    original_normalize_screenshot = Baas_thread.normalize_screenshot
    original_swipe = Baas_thread.swipe
    original_u2_swipe = Baas_thread.u2_swipe
    original_to_main_page = Baas_thread.to_main_page

    @wraps(original_init)
    def init(self, config, logger_signal=None, button_signal=None, update_signal=None, exit_signal=None, **kwargs):
        original_init(self, config, logger_signal, button_signal, update_signal, exit_signal)
        _ensure_logger_extensions(self.logger, jsonify=kwargs.get("jsonify", False))

    @wraps(original_set_ocr)
    def set_ocr(self, ocr):
        self.ocr = ocr
        ocr_client = getattr(ocr, "client", None)
        ocr_config = getattr(ocr_client, "config", None)
        if _env_enabled("BAAS_ANDROID"):
            self.ocr_img_pass_method = 1
        elif ocr_config is not None and getattr(ocr_config, "server_is_remote", False):
            self.ocr_img_pass_method = 1
        elif ocr is None:
            self.ocr_img_pass_method = 1
        else:
            self.ocr_img_pass_method = 0
            self.shared_memory_name = os.path.basename(self.config_set.config_dir)

    def _android_scale_point(self, x, y):
        """Map 1280x720 script coordinates to the current Android landscape surface."""
        viewport = getattr(self, "_android_viewport", None)
        if viewport is not None:
            source_width = float(viewport.get("source_width") or 1280)
            source_height = float(viewport.get("source_height") or 720)
            crop_x = float(viewport.get("crop_x") or 0)
            crop_y = float(viewport.get("crop_y") or 0)
            crop_width = float(viewport.get("crop_width") or source_width)
            crop_height = float(viewport.get("crop_height") or source_height)
            return (
                int(max(0, min(source_width - 1, crop_x + min(1280, x) * crop_width / 1280.0))),
                int(max(0, min(source_height - 1, crop_y + min(720, y) * crop_height / 720.0))),
            )

        width, height = self.resolution if getattr(self, "resolution", None) else (1280, 720)
        scale_x = float(width) / 1280.0
        scale_y = float(height) / 720.0
        return (
            int(max(0, min(float(width) - 1, min(1280, x) * scale_x))),
            int(max(0, min(float(height) - 1, min(720, y) * scale_y))),
        )

    def _android_landscape_bounds(self, width, height):
        """Return the active app bounds in the same coordinate space as the landscape screenshot."""
        try:
            from service.android_local_device import (
                android_active_window_bounds,
                android_current_package,
                android_safe_display_bounds,
            )
        except Exception:
            return None

        package_name = getattr(getattr(self, "config", None), "package_name", None) or getattr(self, "package_name", None)
        try:
            current_package = android_current_package()
        except Exception:
            current_package = ""
        if package_name and current_package and current_package != package_name:
            return None

        def valid(candidate):
            x1, y1, x2, y2 = candidate
            return (
                0 <= x1 < x2 <= width
                and 0 <= y1 < y2 <= height
                and (x2 - x1) >= width * 0.5
                and (y2 - y1) >= height * 0.5
            )

        bounds = android_safe_display_bounds() or android_active_window_bounds()
        if bounds is None:
            return None
        left, top, right, bottom = bounds

        direct = (left, top, right, bottom)
        if valid(direct):
            return direct

        # Some Android APIs report bounds in the unrotated portrait display
        # coordinates while Accessibility screenshots arrive as portrait buffers.
        rotated = (height - bottom, left, height - top, right)
        if valid(rotated):
            return rotated

        return None

    @wraps(original_normalize_screenshot)
    def normalize_screenshot(self, img):
        if not _env_enabled("BAAS_ANDROID"):
            return original_normalize_screenshot(self, img)
        import cv2

        if img is None or getattr(img, "ndim", 0) < 2:
            return img

        height, width = img.shape[:2]
        if height > width:
            self.logger.warning(
                f"Portrait screenshot detected ({width}x{height}), rotating to landscape."
            )
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            height, width = img.shape[:2]

        bounds = self._android_landscape_bounds(width, height)
        if bounds is None:
            crop_x, crop_y, crop_right, crop_bottom = 0, 0, width, height
        else:
            crop_x, crop_y, crop_right, crop_bottom = bounds
            img = img[crop_y:crop_bottom, crop_x:crop_right]

        crop_width = crop_right - crop_x
        crop_height = crop_bottom - crop_y
        viewport = {
            "source_width": width,
            "source_height": height,
            "crop_x": crop_x,
            "crop_y": crop_y,
            "crop_width": crop_width,
            "crop_height": crop_height,
        }
        previous = getattr(self, "_android_viewport", None)
        self._android_viewport = viewport
        if previous != viewport:
            self.logger.info(
                "Android viewport normalized: "
                + f"{width}x{height} crop=({crop_x},{crop_y},{crop_width},{crop_height}) -> 1280x720"
            )

        if img.shape[1] != 1280 or img.shape[0] != 720:
            img = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        return img

    @wraps(original_click_thread)
    def click_thread(self, x, y, count=1, rate=0, duration=0):
        if not _env_enabled("BAAS_ANDROID"):
            return original_click_thread(self, x, y, count, rate, duration)
        import random
        import time

        self.logger.info(f"Click @ ({x},{y})" if count == 1 else f"Click {count} times @ ({x},{y})")
        for _ in range(count):
            if not self.flag_run:
                break
            if rate > 0:
                time.sleep(rate)
            click_x, click_y = self._android_scale_point(
                max(0, x + random.uniform(-5, 5)),
                max(0, y + random.uniform(-5, 5)),
            )
            self.control.click(click_x, click_y)
            if duration > 0:
                time.sleep(duration)

    @wraps(original_start_emulator)
    def start_emulator(self):
        if _env_enabled("BAAS_ANDROID"):
            self.logger.info("Android embedded mode detected; skip desktop emulator startup.")
            return
        return original_start_emulator(self)

    @wraps(original_android_language)
    def _get_android_device_ocr_language(self):
        if _env_enabled("BAAS_ANDROID") and self.server == "Global":
            self.ocr_language = os.getenv("BAAS_ANDROID_GLOBAL_OCR_LANGUAGE", "en-us")
            self.logger.warning(
                "Android embedded mode cannot pull DeviceOption through adb; use " + self.ocr_language
            )
            return
        return original_android_language(self)

    @wraps(original_check_atx)
    def check_atx(self):
        if not _env_enabled("BAAS_ANDROID"):
            return original_check_atx(self)
        import requests
        from service.android_local_device import android_control_backend

        _android_debug_log(self.logger, "injection.check_atx.enter")
        self.logger.info("--------------Check Android local control ----------------")
        backend = android_control_backend(self.logger)
        _android_debug_log(self.logger, "injection.check_atx.backend", backend=backend)
        if backend == "accessibility":
            _android_debug_log(self.logger, "injection.check_atx.accessibility_wait.begin")
            self.wait_uiautomator_start()
            _android_debug_log(self.logger, "injection.check_atx.accessibility_wait.done")
            self.logger.info("Android accessibility control started.")
            return
        try:
            version = requests.get("http://127.0.0.1:7912/version", timeout=3).text
        except requests.RequestException as exc:
            _android_debug_log(self.logger, "injection.check_atx.version.exception", error=repr(exc))
            raise RuntimeError(
                "Android embedded mode requires local uiautomator2 agent on 127.0.0.1:7912"
            ) from exc
        self.logger.info("ATX agent version: [ " + version + " ].")
        _android_debug_log(self.logger, "injection.check_atx.version", version=version)
        _android_debug_log(self.logger, "injection.check_atx.uiautomator_wait.begin")
        self.wait_uiautomator_start()
        _android_debug_log(self.logger, "injection.check_atx.uiautomator_wait.done")
        self.logger.info("Uiautomator2 service started.")

    @wraps(original_wait_uiautomator_start)
    def wait_uiautomator_start(self):
        if not _env_enabled("BAAS_ANDROID"):
            return original_wait_uiautomator_start(self)
        import time
        import cv2
        import numpy as np
        from service.android_local_device import android_accessibility_ready, AndroidLocalScreenshot

        if android_accessibility_ready():
            _android_debug_log(self.logger, "injection.wait_uiautomator.accessibility_ready")
            self.latest_img_array = self.normalize_screenshot(AndroidLocalScreenshot(self.connection).screenshot())
            return

        for attempt in range(1, 11):
            try:
                _android_debug_log(self.logger, "injection.wait_uiautomator.attempt", attempt=attempt)
                self.u2.uiautomator.start()
                while not self.u2.uiautomator.running():
                    time.sleep(0.1)
                self.latest_img_array = self.normalize_screenshot(
                    cv2.cvtColor(np.array(self.u2.screenshot()), cv2.COLOR_RGB2BGR)
                )
                _android_debug_log(self.logger, "injection.wait_uiautomator.ready", attempt=attempt)
                return
            except Exception as exc:  # noqa: BLE001 - retry uiautomator startup
                _android_debug_log(
                    self.logger,
                    "injection.wait_uiautomator.exception",
                    attempt=attempt,
                    error=repr(exc),
                )
                print(exc)
                time.sleep(0.3)
        _android_debug_log(self.logger, "injection.wait_uiautomator.failed")
        raise RuntimeError("Android embedded uiautomator2 agent is not responding")

    @wraps(original_check_resolution)
    def check_resolution(self):
        if not _env_enabled("BAAS_ANDROID"):
            return original_check_resolution(self)

        latest = getattr(self, "latest_img_array", None)
        if latest is None:
            try:
                from service.android_local_device import AndroidLocalScreenshot

                self.latest_img_array = self.normalize_screenshot(
                    AndroidLocalScreenshot(self.connection).screenshot()
                )
                latest = self.latest_img_array
            except Exception as exc:  # noqa: BLE001 - fallback to script-space size below
                self.logger.warning("Android screenshot resolution probe failed: " + str(exc))

        if latest is not None and getattr(latest, "ndim", 0) >= 2:
            height, width = latest.shape[:2]
        else:
            width, height = 1280, 720
        if width < height:
            width, height = height, width

        self.resolution = (width, height)
        self.logger.info("Screen Size  " + str(self.resolution))
        if self.ocr_img_pass_method == 0:
            self.ocr.create_shared_memory(self, width * height * 3)
        self.ratio = width / 1280
        self.logger.info("Screen Size Ratio: " + str(self.ratio))
        return None

    @wraps(original_swipe)
    def swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        if not _env_enabled("BAAS_ANDROID"):
            return original_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)
        import time

        self.logger.info(f"swipe from ( {fx} , {fy} ) --> ( {tx} , {ty} )")
        from_x, from_y = self._android_scale_point(fx, fy)
        to_x, to_y = self._android_scale_point(tx, ty)
        self.control.swipe(from_x, from_y, to_x, to_y, duration)
        if post_sleep_time > 0:
            time.sleep(post_sleep_time)

    @wraps(original_u2_swipe)
    def u2_swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        if not _env_enabled("BAAS_ANDROID"):
            return original_u2_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)
        return self.swipe(fx, fy, tx, ty, duration, post_sleep_time)

    def _android_get_pixel(self, x, y):
        img = getattr(self, "latest_img_array", None)
        if img is None or getattr(img, "ndim", 0) < 3:
            return None
        height, width = img.shape[:2]
        if not (0 <= x < width and 0 <= y < height):
            return None
        return img[int(y), int(x)]

    def _android_pixel_in(self, x, y, rgb_range):
        pixel = _android_get_pixel(self, int(x), int(y))
        if pixel is None:
            return False
        r_min, r_max, g_min, g_max, b_min, b_max = rgb_range
        return r_min <= int(pixel[0]) <= r_max and g_min <= int(pixel[1]) <= g_max and b_min <= int(pixel[2]) <= b_max

    def _android_match_options_modal(self):
        light = (190, 255, 215, 255, 225, 255)
        dark = (0, 70, 20, 95, 45, 120)
        panel_hits = sum(
            1
            for x, y in ((300, 145), (1040, 145), (300, 570), (640, 570), (1040, 570))
            if _android_pixel_in(self, x, y, light)
        )
        close_hits = sum(1 for x, y in ((1058, 146), (1048, 136), (1068, 156)) if _android_pixel_in(self, x, y, dark))
        return panel_hits >= 4 and close_hits >= 1

    def _android_match_mail_menu(self):
        try:
            from core import picture

            return picture.match_img_feature(self, "mail_menu")
        except Exception:
            return False

    def _android_match_task_info_modal(self):
        try:
            from core import picture

            for feature in (
                "rewarded_task_task-info",
                "normal_task_task-info",
                "special_task_task-info",
                "scrimmage_task-info",
            ):
                if picture.match_img_feature(self, feature):
                    return True
        except Exception:
            return False
        return False

    def _android_prepare_for_main_page(self):
        import time

        for _ in range(3):
            try:
                self.latest_img_array = self.get_screenshot_array()
            except Exception as exc:  # noqa: BLE001 - best-effort navigation recovery
                self.logger.warning("Android main-page recovery screenshot failed: " + str(exc))
                return
            if _android_match_options_modal(self):
                self.logger.info("Android main-page recovery closes game options modal.")
                self.click(1058, 146, wait_over=True)
                time.sleep(0.8)
                continue
            if _android_match_mail_menu(self):
                self.logger.info("Android main-page recovery leaves mail menu through quick home.")
                self.click(1236, 31, wait_over=True)
                time.sleep(1.2)
                continue
            if _android_match_task_info_modal(self):
                self.logger.info("Android main-page recovery closes task info modal.")
                self.click(1129, 141, wait_over=True)
                time.sleep(1.0)
                continue
            return

    @wraps(original_to_main_page)
    def to_main_page(self, skip_first_screenshot=False):
        if not _env_enabled("BAAS_ANDROID"):
            return original_to_main_page(self, skip_first_screenshot)
        import time
        from service.android_local_device import start_android_activity

        self.logger.info("Android embedded mode foregrounds game before task navigation.")
        start_android_activity(self.package_name, self.activity_name, self.logger)
        time.sleep(6)
        _android_prepare_for_main_page(self)
        self.logger.info("Android embedded mode delegates to standard main-page detector.")
        return original_to_main_page(self, skip_first_screenshot)

    Baas_thread.__init__ = init
    Baas_thread.click_thread = click_thread
    Baas_thread.set_ocr = set_ocr
    Baas_thread.start_emulator = start_emulator
    Baas_thread._get_android_device_ocr_language = _get_android_device_ocr_language
    Baas_thread.check_atx = check_atx
    Baas_thread.wait_uiautomator_start = wait_uiautomator_start
    Baas_thread.check_resolution = check_resolution
    Baas_thread.normalize_screenshot = normalize_screenshot
    Baas_thread.swipe = swipe
    Baas_thread.u2_swipe = u2_swipe
    Baas_thread.to_main_page = to_main_page
    Baas_thread._android_scale_point = _android_scale_point
    Baas_thread._android_landscape_bounds = _android_landscape_bounds
    Baas_thread._baas_service_injected = True


def _patch_device_modules() -> None:
    _install_gui_stubs()
    from service.android_local_device import (
        ANDROID_LOCAL_METHOD,
        AndroidLocalControl,
        AndroidLocalScreenshot,
        ensure_android_local_agent,
    )
    from core.device.connection import Connection
    from core.device.Control import Control
    from core.device.Screenshot import Screenshot
    from core.device.uiautomator2_client import U2Client
    from core.exception import RequestHumanTakeOver

    if not getattr(Connection, "_baas_service_injected", False):
        original_connection_init = Connection.__init__

        @staticmethod
        def _split_serial(serial):
            serial = Connection.revise_serial(serial)
            try:
                ip, port = serial.rsplit(":", 1)
            except ValueError:
                return serial, ""
            return ip, port

        def _resolve_configured_package(self):
            server = self.config.server
            if server == "auto":
                raise RequestHumanTakeOver("Android embedded mode requires an explicit game server.")
            if server == "官服" or server == "B服":
                self.server = "CN"
            elif server == "国际服" or server == "国际服青少年" or server == "韩国ONE":
                self.server = "Global"
            elif server == "日服":
                self.server = "JP"
            else:
                raise RequestHumanTakeOver("Unsupported Android game server: " + str(server))
            try:
                self.package = self.static_config.package_name[server]
                self.activity = self.static_config.activity_name[server]
            except KeyError as exc:
                raise RequestHumanTakeOver("Game package is not configured: " + str(server)) from exc
            self.logger.info("Package : " + self.package)
            self.logger.info("Server : " + self.server)

        @wraps(original_connection_init)
        def connection_init(self, Baas_instance, skip_package_detection=False):
            if _env_enabled("BAAS_ANDROID"):
                self.Baas_thread = Baas_instance
                self.logger = Baas_instance.get_logger()
                self.config_set = Baas_instance.get_config()
                self.config = self.config_set.config
                self.static_config = self.config_set.static_config
                self.skip_package_detection = skip_package_detection
                self.server = None
                self.activity = None
                self.package = None
                self.serial = os.getenv("BAAS_ANDROID_U2_SERIAL", "127.0.0.1:7912").strip() or "127.0.0.1:7912"
                self.adbIP, self.adbPort = Connection._split_serial(self.serial)
                self._is_android_device = True
                self.logger.info("Android embedded mode detected; use local Android control.")
                self.logger.info(f"Serial : {self.serial}")
                self._resolve_configured_package()
                return
            if skip_package_detection:
                self.Baas_thread = Baas_instance
                self.logger = Baas_instance.get_logger()
                self.config_set = Baas_instance.get_config()
                self.config = self.config_set.config
                self.static_config = self.config_set.static_config
                self.server = None
                original_detect_package = self.detect_package
                self.detect_package = lambda: None
                try:
                    self._init_android_device()
                finally:
                    self.detect_package = original_detect_package
                self._is_android_device = True
                return
            return original_connection_init(self, Baas_instance)

        original_get_current_package = Connection.get_current_package

        @wraps(original_get_current_package)
        def get_current_package(self):
            if _env_enabled("BAAS_ANDROID"):
                from service.android_local_device import android_current_package, _uiautomator_fallback_enabled

                package = android_current_package()
                if package:
                    return package
                if not _uiautomator_fallback_enabled():
                    return ""
                ensure_android_local_agent(getattr(self, "logger", None))
                u2 = getattr(self.Baas_thread, "u2", None)
                if u2 is None:
                    return ""
                current = u2.app_current()
                if isinstance(current, dict):
                    return current.get("package", "")
                return getattr(current, "package", "") or ""
            return original_get_current_package(self)

        Connection._split_serial = _split_serial
        Connection._resolve_configured_package = _resolve_configured_package
        Connection.__init__ = connection_init
        Connection.get_current_package = get_current_package
        Connection._baas_service_injected = True

    if not getattr(Control, "_baas_service_injected", False):
        original_control_init = Control.init_control_instance

        @wraps(original_control_init)
        def init_control_instance(self):
            if _env_enabled("BAAS_ANDROID") and self.Baas_instance.is_android_device:
                self.method = ANDROID_LOCAL_METHOD
                self.config.control_method = ANDROID_LOCAL_METHOD
                self.logger.info("Control method : " + self.method)
                self.control_instance = AndroidLocalControl(self.connection)
                return
            return original_control_init(self)

        Control.init_control_instance = init_control_instance
        Control._baas_service_injected = True

    if not getattr(Screenshot, "_baas_service_injected", False):
        original_screenshot_init = Screenshot.init_screenshot_instance

        @wraps(original_screenshot_init)
        def init_screenshot_instance(self):
            if _env_enabled("BAAS_ANDROID") and self.Baas_instance.is_android_device:
                self.method = ANDROID_LOCAL_METHOD
                self.config.screenshot_method = ANDROID_LOCAL_METHOD
                self.logger.info("Screenshot method : " + self.method)
                self.screenshot_instance = AndroidLocalScreenshot(self.connection)
                return
            return original_screenshot_init(self)

        Screenshot.init_screenshot_instance = init_screenshot_instance
        Screenshot._baas_service_injected = True

    if not getattr(U2Client, "_baas_service_injected", False):
        original_u2_init = U2Client.__init__

        @wraps(original_u2_init)
        def u2_init(self, serial):
            if _env_enabled("BAAS_ANDROID") and not serial.startswith(("http://", "https://")):
                import uiautomator2 as u2

                self.serial = serial
                self.connection = u2.connect("http://" + serial)
                return
            return original_u2_init(self, serial)

        U2Client.__init__ = u2_init
        U2Client._baas_service_injected = True


def _patch_android_coordinate_helpers() -> None:
    from core import color, image, picture

    if (
        getattr(image, "_baas_service_injected", False)
        and getattr(color, "_baas_service_injected", False)
        and getattr(picture, "_baas_service_injected", False)
    ):
        return

    original_co_detect = picture.co_detect
    original_screenshot_cut = image.screenshot_cut
    original_resize_ss_image = image.resize_ss_image
    original_search_image_in_area = image.search_image_in_area
    original_rgb_in_range = color.rgb_in_range
    original_match_rgb_feature = color.match_rgb_feature
    original_deal_with_pop_ups = picture.deal_with_pop_ups

    def _axis_scales(baas):
        """Return Android-aware x/y scales from 1280x720 script space to screenshot space."""
        if not _env_enabled("BAAS_ANDROID"):
            ratio = getattr(baas, "ratio", 1.0) or 1.0
            return ratio, ratio
        width, height = getattr(baas, "resolution", None) or (1280, 720)
        if not width or not height:
            img = getattr(baas, "latest_img_array", None)
            if img is not None and getattr(img, "ndim", 0) >= 2:
                height, width = img.shape[:2]
            else:
                width, height = 1280, 720
        return float(width) / 1280.0, float(height) / 720.0

    def _scale_area(baas, area):
        scale_x, scale_y = _axis_scales(baas)
        img = getattr(baas, "latest_img_array", None)
        height, width = img.shape[:2] if img is not None and getattr(img, "ndim", 0) >= 2 else (720, 1280)
        x0 = int(max(0, min(width, area[0] * scale_x)))
        y0 = int(max(0, min(height, area[1] * scale_y)))
        x1 = int(max(0, min(width, area[2] * scale_x)))
        y1 = int(max(0, min(height, area[3] * scale_y)))
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

    @wraps(original_screenshot_cut)
    def screenshot_cut(baas, area):
        if not _env_enabled("BAAS_ANDROID"):
            return original_screenshot_cut(baas, area)
        x0, y0, x1, y1 = _scale_area(baas, area)
        return baas.latest_img_array[y0:y1, x0:x1, :]

    @wraps(original_resize_ss_image)
    def resize_ss_image(baas, area, interpolation=image.cv2.INTER_AREA):
        if not _env_enabled("BAAS_ANDROID"):
            return original_resize_ss_image(baas, area, interpolation)
        ss_img = screenshot_cut(baas, area)
        target_size = (max(1, int(area[2] - area[0])), max(1, int(area[3] - area[1])))
        return image.cv2.resize(ss_img, target_size, interpolation=interpolation)

    @wraps(original_search_image_in_area)
    def search_image_in_area(baas, template, area=(0, 0, 1280, 720), threshold=0.8, rgb_diff=20):
        if not _env_enabled("BAAS_ANDROID"):
            return original_search_image_in_area(baas, template, area, threshold, rgb_diff)
        template_img = template
        ss_img = resize_ss_image(baas, area)
        similarity = image.cv2.matchTemplate(ss_img, template_img, image.cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = image.cv2.minMaxLoc(similarity)
        if max_val < threshold:
            return False
        ss_img = image.img_cut(
            ss_img,
            (max_loc[0], max_loc[1], max_loc[0] + template_img.shape[1], max_loc[1] + template_img.shape[0]),
        )
        if not image.compare_image_rgb(template_img, ss_img, rgb_diff=rgb_diff):
            return False
        return max_loc[0] + area[0], max_loc[1] + area[1]

    @wraps(original_rgb_in_range)
    def rgb_in_range(baas, x, y, r_min, r_max, g_min, g_max, b_min, b_max, check_nearby=False, nearby_range=1):
        if not _env_enabled("BAAS_ANDROID"):
            return original_rgb_in_range(
                baas, x, y, r_min, r_max, g_min, g_max, b_min, b_max, check_nearby, nearby_range
            )
        scale_x, scale_y = _axis_scales(baas)
        row = int(y * scale_y)
        col = int(x * scale_x)
        pixel = color._get_rgb_at_index(baas, row, col)
        if pixel is None:
            return False
        if color._pixel_in_rgb_range(pixel, r_min, r_max, g_min, g_max, b_min, b_max):
            return True
        if check_nearby:
            for i in range(nearby_range * -1, nearby_range + 1):
                for j in range(nearby_range * -1, nearby_range + 1):
                    pixel = color._get_rgb_at_index(baas, row + i, col + j)
                    if pixel is not None and color._pixel_in_rgb_range(pixel, r_min, r_max, g_min, g_max, b_min, b_max):
                        return True
        return False

    def _android_pixel_in_range(baas, x, y, rgb_range):
        pixel = color._get_rgb_at_index(baas, int(y), int(x))
        if pixel is None:
            return False
        return color._pixel_in_rgb_range(pixel, *rgb_range)

    def _android_match_main_page(baas):
        img = getattr(baas, "latest_img_array", None)
        if img is None or getattr(img, "ndim", 0) < 3:
            return False
        height, width = img.shape[:2]
        if width != 1280 or height != 720:
            return False

        left_profile = (0, 45, 45, 105, 95, 170)
        left_profile_points = (
            (40, 45),
            (60, 45),
            (120, 45),
            (60, 60),
            (40, 80),
        )
        left_profile_hits = sum(1 for x, y in left_profile_points if _android_pixel_in_range(baas, x, y, left_profile))

        top_light = (210, 255, 210, 255, 220, 255)
        top_points = (
            (450, 60),
            (580, 60),
            (700, 60),
            (820, 60),
            (960, 60),
            (1080, 60),
            (1200, 60),
        )
        top_hits = sum(1 for x, y in top_points if _android_pixel_in_range(baas, x, y, top_light))

        bottom_light = (200, 255, 200, 255, 200, 255)
        bottom_points = (
            (60, 675),
            (200, 675),
            (320, 675),
            (450, 675),
            (580, 675),
            (700, 675),
            (1080, 675),
            (1200, 675),
        )
        bottom_hits = sum(1 for x, y in bottom_points if _android_pixel_in_range(baas, x, y, bottom_light))
        return left_profile_hits >= 3 and bottom_hits >= 6

    def _android_match_news_close_button(baas):
        img = getattr(baas, "latest_img_array", None)
        if img is None or getattr(img, "ndim", 0) < 3:
            return False
        height, width = img.shape[:2]
        if width != 1280 or height != 720:
            return False

        white = (230, 255, 230, 255, 230, 255)
        blue = (20, 90, 120, 190, 220, 255)
        modal_gray = (80, 140, 95, 145, 110, 165)
        close_white_points = ((1132, 94), (1142, 104), (1152, 114))
        close_blue_points = ((1100, 100), (1120, 104), (1160, 104), (1140, 80), (1140, 130))
        header_points = ((130, 170), (450, 170), (900, 170))
        white_hits = sum(1 for x, y in close_white_points if _android_pixel_in_range(baas, x, y, white))
        blue_hits = sum(1 for x, y in close_blue_points if _android_pixel_in_range(baas, x, y, blue))
        header_hits = sum(1 for x, y in header_points if _android_pixel_in_range(baas, x, y, modal_gray))
        return white_hits >= 2 and blue_hits >= 4 and header_hits >= 2

    def _android_match_help_modal(baas):
        img = getattr(baas, "latest_img_array", None)
        if img is None or getattr(img, "ndim", 0) < 3:
            return False
        height, width = img.shape[:2]
        if width != 1280 or height != 720:
            return False

        panel = (190, 255, 215, 255, 225, 255)
        close_dark = (0, 75, 20, 100, 45, 125)
        panel_points = ((260, 130), (640, 130), (1010, 130), (260, 600), (640, 600), (1010, 600))
        close_points = ((1008, 122), (1018, 132), (1028, 142))
        panel_hits = sum(1 for x, y in panel_points if _android_pixel_in_range(baas, x, y, panel))
        close_hits = sum(1 for x, y in close_points if _android_pixel_in_range(baas, x, y, close_dark))
        return panel_hits >= 5 and close_hits >= 2

    @wraps(original_match_rgb_feature)
    def match_rgb_feature(baas, feature_name):
        if _env_enabled("BAAS_ANDROID") and feature_name == "main_page":
            return _android_match_main_page(baas)
        return original_match_rgb_feature(baas, feature_name)

    @wraps(original_co_detect)
    def co_detect(baas, *args, **kwargs):
        if _env_enabled("BAAS_ANDROID"):
            kwargs["check_pkg_interval"] = max(float(kwargs.get("check_pkg_interval", 20)), 1_000_000.0)
            rgb_ends = args[0] if args else kwargs.get("rgb_ends")
            if rgb_ends == "main_page" or (isinstance(rgb_ends, list) and "main_page" in rgb_ends):
                kwargs["tentative_click"] = True
                kwargs.setdefault("tentative_x", 640)
                kwargs.setdefault("tentative_y", 360)
        return original_co_detect(baas, *args, **kwargs)

    @wraps(original_deal_with_pop_ups)
    def deal_with_pop_ups(baas, pop_ups_rgb_reactions=None, pop_ups_img_reactions=None):
        if _env_enabled("BAAS_ANDROID") and _android_match_news_close_button(baas):
            baas.logger.info("Found Android main page news modal close button")
            baas.click(1142, 104)
            baas.last_click_time = time.time()
            baas.last_click_position = (1142, 104)
            baas.last_click_name = "android_main_page_news"
            return True, "android_main_page_news"
        if _env_enabled("BAAS_ANDROID") and _android_match_help_modal(baas):
            baas.logger.info("Found Android help modal close button")
            baas.click(1018, 132)
            baas.last_click_time = time.time()
            baas.last_click_position = (1018, 132)
            baas.last_click_name = "android_help_modal"
            return True, "android_help_modal"
        return original_deal_with_pop_ups(baas, pop_ups_rgb_reactions, pop_ups_img_reactions)

    picture.co_detect = co_detect
    picture.deal_with_pop_ups = deal_with_pop_ups
    picture.match_rgb_feature = match_rgb_feature
    picture._baas_service_injected = True
    image.screenshot_cut = screenshot_cut
    image.resize_ss_image = resize_ss_image
    image.search_image_in_area = search_image_in_area
    image._baas_service_injected = True
    color.rgb_in_range = rgb_in_range
    color.match_rgb_feature = match_rgb_feature
    color._baas_service_injected = True


def _patch_cafe_reward() -> None:
    import cv2
    import numpy as np
    import threading
    import time
    from module import cafe_reward

    if getattr(cafe_reward, "_baas_service_injected", False):
        return

    original_to_cafe = cafe_reward.to_cafe
    original_to_no2_cafe = cafe_reward.to_no2_cafe
    original_cafe_to_gift = cafe_reward.cafe_to_gift
    original_gift_to_cafe = cafe_reward.gift_to_cafe
    original_swipe_gift_and_screenshot = cafe_reward.swipe_gift_and_screenshot
    original_screenshot_thread = cafe_reward.screenshot_thread
    original_zoom_out = cafe_reward.zoom_out
    original_to_invitation_ticket = cafe_reward.to_invitation_ticket
    original_get_invitation_ticket_next_time = cafe_reward.get_invitation_ticket_next_time
    original_to_cafe_earning_status = cafe_reward.to_cafe_earning_status
    original_collect = cafe_reward.collect

    cafe_reward._happy_face_templates = None
    cafe_reward._happy_face_match_scale = 0.75
    cafe_reward._happy_face_match_roi = (0, 45, 1280, 555)

    def _resize_for_happy_face_match(img):
        """Resize cafe interaction images before matching to keep template search bounded."""
        height, width = img.shape[:2]
        size = (
            max(1, int(round(width * cafe_reward._happy_face_match_scale))),
            max(1, int(round(height * cafe_reward._happy_face_match_scale))),
        )
        return cv2.resize(img, size, interpolation=cv2.INTER_AREA)

    def _get_happy_face_templates():
        """Load and cache grayscale cafe interaction templates used by the injected matcher."""
        if cafe_reward._happy_face_templates is None:
            templates = []
            for i in range(1, 5):
                template = cv2.imread("src/images/CN/cafe/happy_face" + str(i) + ".png")
                if template is None:
                    templates.append(None)
                    continue
                template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
                template = _resize_for_happy_face_match(template)
                templates.append(template)
            cafe_reward._happy_face_templates = templates
        return cafe_reward._happy_face_templates

    def _dedupe_happy_face_points(points):
        """Merge nearby interaction candidates so one student head is clicked only once."""
        deduped = []
        for x, y in sorted(points, key=lambda item: (item[1], item[0])):
            if any(abs(x - px) <= 24 and abs(y - py) <= 24 for px, py in deduped):
                continue
            deduped.append([x, y])
            if len(deduped) >= 32:
                break
        return deduped

    def _match_android_yellow_interactions(img):
        """Find Android cafe interaction marks by clustering yellow marker rays."""
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([12, 80, 120]), np.array([45, 255, 255]))
        mask[:45, :] = 0
        mask[570:, :] = 0
        mask[:, :260] = 0
        if not hasattr(cv2, "connectedComponentsWithStats"):
            return _match_android_yellow_interactions_by_grid(mask)
        count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        rays = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            if not (15 <= area <= 650):
                continue
            if not (5 <= width <= 75 and 5 <= height <= 55):
                continue
            center_x, center_y = centroids[index]
            if center_y < 70:
                continue
            rays.append((float(center_x), float(center_y), int(area)))
        return _cluster_android_interaction_rays(rays)

    def _cluster_android_interaction_rays(rays):
        """Group nearby yellow rays into one candidate per Android interaction mark."""
        clusters = []
        for ray_x, ray_y, area in sorted(rays, key=lambda item: (item[1], item[0])):
            target = None
            for cluster in clusters:
                center_x = cluster["sum_x"] / cluster["count"]
                center_y = cluster["sum_y"] / cluster["count"]
                if abs(ray_x - center_x) <= 75 and abs(ray_y - center_y) <= 55:
                    target = cluster
                    break
            if target is None:
                target = {
                    "sum_x": 0.0,
                    "sum_y": 0.0,
                    "count": 0,
                    "area": 0,
                    "min_x": ray_x,
                    "max_x": ray_x,
                    "min_y": ray_y,
                    "max_y": ray_y,
                }
                clusters.append(target)
            target["sum_x"] += ray_x
            target["sum_y"] += ray_y
            target["count"] += 1
            target["area"] += area
            target["min_x"] = min(target["min_x"], ray_x)
            target["max_x"] = max(target["max_x"], ray_x)
            target["min_y"] = min(target["min_y"], ray_y)
            target["max_y"] = max(target["max_y"], ray_y)

        points = []
        for cluster in clusters:
            spread_x = cluster["max_x"] - cluster["min_x"]
            spread_y = cluster["max_y"] - cluster["min_y"]
            if not (2 <= cluster["count"] <= 8):
                continue
            if spread_x < 14 and spread_y < 14:
                continue
            if not (40 <= cluster["area"] <= 2400):
                continue
            center_x = cluster["sum_x"] / cluster["count"]
            center_y = cluster["sum_y"] / cluster["count"]
            points.append([int(center_x), int(min(center_y + 62, 591))])
        points = _dedupe_happy_face_points(points)
        return sorted(points, key=lambda point: (point[1], point[0]))[:6]

    def _match_android_yellow_interactions_by_grid(mask):
        """Group yellow marker pixels with a numpy grid when OpenCV components are unavailable."""
        ys, xs = np.where(mask > 0)
        if len(xs) == 0:
            return []
        cell_size = 48
        keys = (ys // cell_size) * 64 + (xs // cell_size)
        unique_keys, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
        points = []
        for index, _key in enumerate(unique_keys):
            count = counts[index]
            if not (20 <= count <= 900):
                continue
            selection = inverse == index
            group_xs = xs[selection]
            group_ys = ys[selection]
            width = int(np.ptp(group_xs)) + 1
            height = int(np.ptp(group_ys)) + 1
            if not (8 <= width <= 150 and 8 <= height <= 130):
                continue
            center_x = int(np.mean(group_xs))
            center_y = int(np.mean(group_ys))
            if center_y < 70 and count > 900:
                continue
            points.append([center_x, int(min(center_y + 62, 591))])
        return sorted(_dedupe_happy_face_points(points), key=lambda point: (point[1], point[0]))[:6]

    def _android_wait_for_cafe(self, timeout=45.0):
        """Wait until Android reaches a stable cafe screen after navigation or loading."""
        from core import color, image

        deadline = time.time() + timeout
        while self.flag_run and time.time() < deadline:
            self.update_screenshot_array()
            if color.match_rgb_feature(self, "invitation_ticket_available_to_use"):
                return True
            if image.compare_image(self, "cafe_menu", threshold=0.6, rgb_diff=80):
                return True
            if image.compare_image(self, "cafe_0.0", threshold=0.6, rgb_diff=80):
                return True
            time.sleep(0.75)
        self.logger.warning("Android cafe screen was not confirmed before timeout.")
        return False

    def _android_is_main_page(self):
        """Return whether the Android screenshot is on the home page."""
        from core import color

        try:
            return color.match_rgb_feature(self, "main_page")
        except Exception:
            return False

    def _android_is_cafe_screen(self):
        """Return whether the Android screenshot is already in cafe."""
        from core import color, image

        try:
            return (
                color.match_rgb_feature(self, "cafe")
                or color.match_rgb_feature(self, "gift")
                or color.match_rgb_feature(self, "invitation_ticket_available_to_use")
                or image.compare_image(self, "cafe_menu", threshold=0.6, rgb_diff=80)
                or image.compare_image(self, "cafe_0.0", threshold=0.6, rgb_diff=80)
            )
        except Exception:
            return False

    def _android_return_to_main_or_cafe(self):
        """Leave Android subpages where the home footer is hidden before cafe navigation."""
        self.update_screenshot_array()
        if _android_is_main_page(self) or _android_is_cafe_screen(self):
            return
        self.click(1236, 31, wait_over=True)
        time.sleep(1.5)
        self.update_screenshot_array()
        if _android_is_main_page(self) or _android_is_cafe_screen(self):
            return
        self.click(58, 36, wait_over=True)
        time.sleep(1.5)
        self.update_screenshot_array()

    def _android_close_cafe_overlay(self):
        """Close Android cafe overlays such as the gift tray before base-screen actions."""
        self.update_screenshot_array()
        if not _android_is_cafe_screen(self):
            return
        self.click(1206, 550, wait_over=True)
        time.sleep(0.75)
        self.update_screenshot_array()

    def match(img):
        """Find cafe interaction icons with template matching on every platform."""
        if _env_enabled("BAAS_ANDROID"):
            return _match_android_yellow_interactions(img)
        res = []
        roi_x0, roi_y0, roi_x1, roi_y1 = cafe_reward._happy_face_match_roi
        search_img = img[roi_y0:roi_y1, roi_x0:roi_x1]
        search_img = cv2.cvtColor(search_img, cv2.COLOR_BGR2GRAY)
        search_img = _resize_for_happy_face_match(search_img)
        for template in _get_happy_face_templates():
            if template is None:
                continue
            result = cv2.matchTemplate(search_img, template, cv2.TM_CCOEFF_NORMED)
            threshold = 0.75
            suppress_x = max(20, template.shape[1])
            suppress_y = max(20, template.shape[0])
            for _ in range(16):
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val < threshold:
                    break
                pt_x, pt_y = max_loc
                res.append([
                    int(roi_x0 + (pt_x + template.shape[1] / 2) / cafe_reward._happy_face_match_scale),
                    int(roi_y0 + (pt_y + template.shape[0] / 2) / cafe_reward._happy_face_match_scale + 58),
                ])
                left = max(0, pt_x - suppress_x)
                right = min(result.shape[1], pt_x + suppress_x + 1)
                top = max(0, pt_y - suppress_y)
                bottom = min(result.shape[0], pt_y + suppress_y + 1)
                result[top:bottom, left:right] = -1
        return _dedupe_happy_face_points(res)

    @wraps(original_to_cafe)
    def to_cafe(self, skip_first_screenshot=False):
        """Enter cafe directly on Android because 20:9 home RGB anchors are unstable."""
        if not getattr(self, "is_android_device", False):
            return original_to_cafe(self, skip_first_screenshot)
        _android_return_to_main_or_cafe(self)
        _android_close_cafe_overlay(self)
        if _android_wait_for_cafe(self, timeout=3.0):
            return None
        self.logger.info("Android direct cafe navigation.")
        self.click(95, 675, wait_over=True)
        _android_wait_for_cafe(self, timeout=20.0)
        return None

    @wraps(original_to_no2_cafe)
    def to_no2_cafe(self):
        """Switch to the second cafe through stable Android wide-screen coordinates."""
        if not getattr(self, "is_android_device", False):
            return original_to_no2_cafe(self)
        to_cafe(self)
        self.click(144, 100, wait_over=True)
        time.sleep(2)
        _android_wait_for_cafe(self, timeout=10.0)

    @wraps(original_cafe_to_gift)
    def cafe_to_gift(self):
        """Open the Android wide-screen gift tray from the bottom cafe toolbar."""
        if not getattr(self, "is_android_device", False):
            return original_cafe_to_gift(self)
        to_cafe(self)
        self.click(192, 640, wait_over=True)
        time.sleep(1.0)
        self.update_screenshot_array()

    @wraps(original_gift_to_cafe)
    def gift_to_cafe(self):
        """Return from the gift screen quickly on Android without slow image co-detection."""
        if getattr(self, "is_android_device", False):
            _android_close_cafe_overlay(self)
            _android_wait_for_cafe(self, timeout=8.0)
            return
        return original_gift_to_cafe(self)

    @wraps(original_zoom_out)
    def zoom_out(self):
        """Avoid UIAutomator pinch calls on Android accessibility-only control."""
        if not getattr(self, "is_android_device", False):
            return original_zoom_out(self)
        to_cafe(self)
        self.swipe(709, 558, 709, 309, duration=0.2)
        time.sleep(0.5)

    @wraps(original_to_invitation_ticket)
    def to_invitation_ticket(self, skip_first_screenshot=False):
        """Open the Android wide-screen invitation ticket panel from the cafe footer."""
        if not getattr(self, "is_android_device", False):
            return original_to_invitation_ticket(self, skip_first_screenshot)
        from core import image

        if image.compare_image(self, "cafe_invitation-ticket", threshold=0.65, rgb_diff=80):
            return "cafe_invitation-ticket"
        to_cafe(self)
        self.click(884, 647, wait_over=True)
        time.sleep(1.0)
        self.update_screenshot_array()
        if image.compare_image(self, "cafe_invitation-ticket-invalid", threshold=0.65, rgb_diff=80):
            return "cafe_invitation-ticket-invalid"
        return "cafe_invitation-ticket"

    @wraps(original_get_invitation_ticket_next_time)
    def get_invitation_ticket_next_time(self):
        """Skip Android invite cooldown OCR when the ticket is unavailable."""
        if not getattr(self, "is_android_device", False):
            return original_get_invitation_ticket_next_time(self)
        self.logger.info("Android invitation ticket cooldown OCR skipped.")
        return None

    @wraps(original_to_cafe_earning_status)
    def to_cafe_earning_status(self):
        """Open the Android cafe earnings panel with a direct wide-screen tap."""
        if not getattr(self, "is_android_device", False):
            return original_to_cafe_earning_status(self)
        to_cafe(self)
        self.click(1172, 640, wait_over=True)
        time.sleep(1.0)
        self.update_screenshot_array()

    @wraps(original_collect)
    def collect(self):
        """Collect Android cafe earnings without relying on 16:9 image anchors."""
        if not getattr(self, "is_android_device", False):
            return original_collect(self)
        to_cafe_earning_status(self)
        self.click(643, 521, wait_over=True)
        time.sleep(1.0)
        self.click(640, 100, wait_over=True)
        time.sleep(0.5)
        to_cafe(self)

    @wraps(original_swipe_gift_and_screenshot)
    def swipe_gift_and_screenshot(self):
        """Capture the cafe interaction frame while dragging gifts across the screen."""
        if not getattr(self, "is_android_device", False):
            return original_swipe_gift_and_screenshot(self)
        shot_delay = self.config.cafe_reward_interaction_shot_delay
        thread = threading.Thread(target=cafe_reward.screenshot_thread, args=(self, shot_delay))
        thread.start()
        start_t = time.time()
        self.u2_swipe(131, 660, 1280, 660, duration=0.3)
        thread.join(timeout=max(1.0, shot_delay + 1.0))
        swipe_t = round(time.time() - start_t, 3)
        self.logger.info("Gift swipe duration : [ " + str(swipe_t) + " ]")
        return swipe_t

    @wraps(original_screenshot_thread)
    def screenshot_thread(self, delay):
        """Capture cafe screenshots through the configured Android screenshot backend."""
        if not getattr(self, "is_android_device", False):
            return original_screenshot_thread(self, delay)
        time.sleep(delay)
        self.latest_img_array = self.get_screenshot_array()

    original_find_student_position = cafe_reward.find_student_position

    @wraps(original_find_student_position)
    def find_student_position(self):
        """Log cafe interaction matching duration and candidate count for diagnostics."""
        match_start_t = time.time()
        res = original_find_student_position(self)
        self.logger.info(
            "Cafe interaction total duration : [ "
            + str(round(time.time() - match_start_t, 3))
            + " ], candidates : [ "
            + str(len(res))
            + " ]"
        )
        return res

    cafe_reward.match = match
    cafe_reward.to_cafe = to_cafe
    cafe_reward.to_no2_cafe = to_no2_cafe
    cafe_reward.cafe_to_gift = cafe_to_gift
    cafe_reward.gift_to_cafe = gift_to_cafe
    cafe_reward.zoom_out = zoom_out
    cafe_reward.to_invitation_ticket = to_invitation_ticket
    cafe_reward.get_invitation_ticket_next_time = get_invitation_ticket_next_time
    cafe_reward.to_cafe_earning_status = to_cafe_earning_status
    cafe_reward.collect = collect
    cafe_reward.swipe_gift_and_screenshot = swipe_gift_and_screenshot
    cafe_reward.screenshot_thread = screenshot_thread
    cafe_reward.find_student_position = find_student_position
    cafe_reward._baas_service_injected = True


def _patch_android_restart() -> None:
    from module import restart
    from service.android_local_device import start_android_activity

    if getattr(restart, "_baas_service_injected", False):
        return

    original_implement = restart.implement
    original_start = restart.start

    @wraps(original_implement)
    def implement(self):
        """Check Android game foreground state without UIAutomator."""
        if not _env_enabled("BAAS_ANDROID"):
            return original_implement(self)
        current_package = self.connection.get_current_package()
        if current_package != self.package_name:
            if current_package:
                self.logger.warning("APP NOT RUNNING current package: " + current_package)
            start(self)
            return True
        self.logger.info("CHECK RESTART")
        if restart.check_need_restart(self):
            self.logger.info("current package: " + current_package)
            self.logger.info("Android embedded mode restarts game via native launch.")
            start(self)
        return True

    @wraps(original_start)
    def start(self):
        """Start the Android game with the configured activity in embedded service mode."""
        if not _env_enabled("BAAS_ANDROID"):
            return original_start(self)
        self.logger.info("-- START BLUE ARCHIVE --")
        start_android_activity(self.package_name, self.activity_name, self.logger)
        self.to_main_page()

    restart.implement = implement
    restart.start = start
    try:
        from core import Baas_thread as baas_thread_module

        baas_thread_module.func_dict["restart"] = implement
    except Exception:
        pass
    restart._baas_service_injected = True


def _patch_android_uiautomator_refresh() -> None:
    from module import refresh_uiautomator2

    if getattr(refresh_uiautomator2, "_baas_service_injected", False):
        return

    original_implement = refresh_uiautomator2.implement

    @wraps(original_implement)
    def implement(self):
        """Refresh Android local control without requiring a UIAutomator instance."""
        if not _env_enabled("BAAS_ANDROID"):
            return original_implement(self)
        if _env_enabled("BAAS_ANDROID_ENABLE_UIAUTOMATOR_FALLBACK") and getattr(self, "u2", None) is not None:
            return original_implement(self)
        import time

        self.logger.info("Android accessibility mode skips UIAutomator refresh.")
        try:
            self.update_screenshot_array()
        except Exception as exc:  # noqa: BLE001 - refresh is best-effort
            self.logger.warning("Android accessibility refresh screenshot failed: " + str(exc))
        self.last_refresh_u2_time = time.time()
        return True

    refresh_uiautomator2.implement = implement
    try:
        from core import Baas_thread as baas_thread_module

        baas_thread_module.func_dict["refresh_uiautomator2"] = implement
    except Exception:
        pass
    refresh_uiautomator2._baas_service_injected = True


def _patch_android_rewarded_task() -> None:
    from module import rewarded_task

    if getattr(rewarded_task, "_baas_android_stable_navigation_injected", False):
        return

    original_to_bounty = rewarded_task.to_bounty
    original_to_choose_bounty = rewarded_task.to_choose_bounty

    @wraps(original_to_bounty)
    def to_bounty(self, num, skip_first_screenshot=False):
        if not _env_enabled("BAAS_ANDROID"):
            return original_to_bounty(self, num, skip_first_screenshot)
        self.logger.info("Android rewarded_task navigation uses fresh screenshot before co-detect.")
        return original_to_bounty(self, num, False)

    @wraps(original_to_choose_bounty)
    def to_choose_bounty(self):
        if not _env_enabled("BAAS_ANDROID"):
            return original_to_choose_bounty(self)
        from core import picture

        self.logger.info("Android rewarded_task choose-page navigation uses fresh screenshot before co-detect.")
        img_ends = "rewarded_task_location-select"
        img_possibles = {
            "normal_task_sweep-complete": (643, 585),
            "normal_task_start-sweep-notice": (887, 164),
            "rewarded_task_level-list": (57, 41),
            "rewarded_task_task-info": (1129, 141),
            "main_page_bus": (731, rewarded_task.BOUNTY_BUTTON_Y[self.server]),
            "rewarded_task_help": (1014, 135),
            "rewarded_task_purchase-bounty-ticket-notice": (888, 163),
        }
        rgb_possibles = {"main_page": (1198, 580)}
        img_possibles.update(picture.GAME_ONE_TIME_POP_UPS[self.server])
        picture.co_detect(self, None, rgb_possibles, img_ends, img_possibles, skip_first_screenshot=False)
        return None

    rewarded_task.to_bounty = to_bounty
    rewarded_task.to_choose_bounty = to_choose_bounty
    rewarded_task._baas_android_stable_navigation_injected = True


def apply_service_injections() -> None:
    global _APPLIED
    if _APPLIED:
        return
    _install_gui_stubs()
    _install_android_ocr_modules()
    _patch_logger()
    _patch_main()
    _patch_device_modules()
    _patch_baas_thread()
    _patch_android_coordinate_helpers()
    _patch_android_restart()
    _patch_android_uiautomator_refresh()
    _patch_android_rewarded_task()
    _patch_cafe_reward()
    _APPLIED = True
