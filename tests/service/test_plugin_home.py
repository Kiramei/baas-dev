"""The home editor stores portable profile layout, without executable actions."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from service.plugins import PluginError, PluginManager
from service.plugins.schema import validate_manifest, validate_value, with_defaults

BUNDLED = Path(__file__).resolve().parents[2] / "service/plugins/bundled"
PLUGIN = "baas.home-editor"
DEFAULTS = {
    "assets_visible": True, "tasks_visible": True, "shortcuts_visible": True,
    "show_edit_entry": True, "section_order": ["shortcuts", "assets", "tasks", "logs"],
}


def manager(tmp_path):
    for name in ("one", "two"):
        profile = tmp_path / "config" / name
        profile.mkdir(parents=True)
        (profile / "config.json").write_text(json.dumps({"name": name}))
        (profile / "event.json").write_text("{}")
    return PluginManager(tmp_path, SimpleNamespace(), bundled_root=BUNDLED)


def test_home_manifest_declares_only_portable_layout_and_has_valid_defaults():
    manifest = validate_manifest(json.loads((BUNDLED / PLUGIN / "plugin.json").read_text()))
    assert manifest["permissions"] == [] and manifest["actions"] == []
    assert not list((BUNDLED / PLUGIN).glob("*.py"))
    values = with_defaults(manifest["settings_schema"], {})
    assert values == DEFAULTS
    validate_value(manifest["settings_schema"], values)


def test_official_catalog_has_exactly_the_five_migrated_tools_and_excludes_examples(tmp_path):
    async def scenario():
        catalog = await manager(tmp_path).catalog("one")
        assert catalog["errors"] == []
        assert {row["id"] for row in catalog["plugins"]} == {
            "baas.hoard-ap", "baas.auto-push", "baas.equipment-planner",
            "baas.inventory-calculator", "baas.home-editor",
        }
        assert all(row["source"] == "builtin" and row["trusted"] for row in catalog["plugins"])
        assert all(not row["enabled"] for row in catalog["plugins"])
        examples = BUNDLED.parents[2] / "examples/plugins"
        for plugin_id in ("baas.ap-budget", "baas.resource-budget", "baas.task-launcher", "example.notes"):
            assert (examples / plugin_id / "plugin.json").is_file()

    asyncio.run(scenario())


def test_home_settings_save_to_disk_and_reload_per_profile_without_touching_game_config(tmp_path):
    host = manager(tmp_path)
    custom = {**DEFAULTS, "assets_visible": False, "shortcuts_visible": False,
              "section_order": ["logs", "tasks", "assets", "shortcuts"]}

    async def scenario():
        assert (await host.settings("one", PLUGIN))["values"] == DEFAULTS
        result = await host.update_settings("one", {"plugin_id": PLUGIN, "revision": 0, "values": custom})
        assert result == {"plugin_id": PLUGIN, "config_id": "one", "revision": 1, "values": custom}
        assert (await host.settings("two", PLUGIN))["values"] == DEFAULTS
        restarted = PluginManager(tmp_path, SimpleNamespace(), bundled_root=BUNDLED)
        assert (await restarted.settings("one", PLUGIN))["values"] == custom
        assert (await restarted.settings("two", PLUGIN))["revision"] == 0
        saved = json.loads((tmp_path / "config/one/plugins.json").read_text())
        assert saved["plugins"][PLUGIN]["values"] == custom
        assert not (tmp_path / "config/two/plugins.json").exists()
        assert json.loads((tmp_path / "config/one/config.json").read_text()) == {"name": "one"}

    asyncio.run(scenario())


def test_home_stale_revision_cannot_overwrite_newer_layout_and_enablement_is_independent(tmp_path):
    host = manager(tmp_path)

    async def scenario():
        await host.update_settings("one", {"plugin_id": PLUGIN, "revision": 0,
                                           "values": {"tasks_visible": False}})
        with pytest.raises(PluginError) as error:
            await host.update_settings("one", {"plugin_id": PLUGIN, "revision": 0,
                                               "values": {"tasks_visible": True}})
        assert error.value.code == "REVISION_CONFLICT"
        assert (await host.settings("one", PLUGIN))["values"]["tasks_visible"] is False
        enabled = await host.set_enabled("one", {"plugin_id": PLUGIN, "enabled": True})
        assert enabled["enabled"] and enabled["permissions"] == [] and enabled["actions"] == []
        assert (await host.settings("one", PLUGIN))["revision"] == 1
        second = next(row for row in (await host.catalog("two"))["plugins"] if row["id"] == PLUGIN)
        assert second["enabled"] is False
        with pytest.raises(PluginError) as error:
            await host.run("one", {"plugin_id": PLUGIN, "action_id": "run", "input": {},
                                   "request_id": str(uuid.uuid4())})
        assert error.value.code == "ACTION_NOT_FOUND"
        assert host._processes == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("values", [
    {"assets_visible": "false"}, {"unknown": True},
    {"section_order": ["logs"]},
    {"section_order": ["logs", "assets", "tasks", "<script>"]},
])
def test_home_settings_reject_wrong_types_unknown_sections_and_incomplete_order(tmp_path, values):
    async def scenario():
        host = manager(tmp_path)
        with pytest.raises(PluginError) as error:
            await host.update_settings("one", {"plugin_id": PLUGIN, "revision": 0, "values": values})
        assert error.value.code == "INVALID_SETTINGS"
        assert not (tmp_path / "config/one/plugins.json").exists()

    asyncio.run(scenario())
