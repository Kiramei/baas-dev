from __future__ import annotations

import json
from pathlib import Path


def test_bilibili_android_launcher_activity_matches_real_device():
    static_config = json.loads(Path("config/static.json").read_text(encoding="utf-8"))

    assert static_config["package_name"]["B服"] == "com.RoamingStar.BlueArchive.bilibili"
    assert static_config["activity_name"]["B服"] == "com.yostar.supersdk.activity.YoStarSplashActivity"
