"""Reference-tool migrations: real drop data and independently counted boards."""
from __future__ import annotations

import importlib.util
import asyncio
from itertools import combinations, product
import json
from math import comb
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from service.plugins.schema import validate_manifest, validate_value, with_defaults
from service.plugins import PluginManager
from service.plugins.worker import execute_request

ROOT = Path(__file__).resolve().parents[2] / "service/plugins/bundled"


@pytest.fixture(autouse=True)
def no_package_bytecode(monkeypatch):
    # Production compute workers run with -B. In-process reference tests must
    # likewise avoid mutating trusted package bytes and invalidating discovery.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


def load(plugin):
    source = ROOT / plugin / "main.py"
    spec = importlib.util.spec_from_file_location(plugin.replace(".", "_"), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(plugin, action_id, values=None):
    root = ROOT / plugin
    manifest = validate_manifest(json.loads((root / "plugin.json").read_text()))
    action = next(row for row in manifest["actions"] if row["id"] == action_id)
    values = with_defaults(action["input_schema"], values or {})
    validate_value(action["input_schema"], values)
    response = execute_request(str(root), {"entrypoint": action["entrypoint"], "context": {}, "input": values})
    assert response["ok"], response
    validate_value(action["output_schema"], response["result"])
    return response["result"]


@pytest.mark.parametrize("plugin,action", [
    ("baas.inventory-calculator", "solve"),
    ("baas.equipment-planner", "plan"),
    ("baas.equipment-planner", "estimate"),
    ("baas.equipment-planner", "eleph"),
    ("baas.equipment-planner", "catalog"),
])
def test_every_portable_action_runs_through_real_worker_and_declared_schema(plugin, action):
    request(plugin, action)


def test_new_bundled_tools_execute_in_real_disposable_host_processes(tmp_path):
    profile = tmp_path / "config" / "one"
    profile.mkdir(parents=True)
    (profile / "config.json").write_text('{"name":"test"}')
    (profile / "event.json").write_text('{}')
    manager = PluginManager(tmp_path, SimpleNamespace(), bundled_root=ROOT)

    async def scenario():
        for plugin, action in [("baas.equipment-planner", "plan"), ("baas.inventory-calculator", "solve")]:
            await manager.set_enabled("one", {"plugin_id": plugin, "enabled": True})
            job = await manager.run("one", {"plugin_id": plugin, "action_id": action,
                                           "input": {}, "request_id": str(uuid.uuid4())})
            for _ in range(200):
                state = await manager.get_job("one", job["id"])
                if state["state"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert state["state"] == "succeeded", state
            assert state["result"]["status"] in {"planned", "solved"}
            assert manager._processes == {}
        await manager.stop_all()

    asyncio.run(scenario())


def item(width=1, height=1, count=0):
    return {"width": width, "height": height, "count": count}


def test_domino_reference_geometry_and_probability_mass():
    result = request("baas.inventory-calculator", "solve", {"items": [item(2, 1, 1), item(), item()]})
    # 5 rows × 8 horizontal placements + 4 rows × 9 vertical placements.
    assert result["total_arrangements"] == "76"
    assert result["cells"][0]["group_a"] == pytest.approx(2 / 76)
    assert result["cells"][22]["group_a"] == pytest.approx(4 / 76)
    assert sum(row["any_item"] for row in result["cells"]) == pytest.approx(2)
    assert 22 in result["best_cells"] and 0 not in result["best_cells"]


def test_closed_form_uses_big_integers_beyond_uint64_and_js_precision():
    result = request("baas.inventory-calculator", "solve", {"items": [item(1, 1, 7)] * 3})
    expected = comb(45, 7) * comb(38, 7) * comb(31, 7)
    assert expected > 2 ** 64
    assert result["total_arrangements"] == str(expected)
    assert result["cells"][0]["group_b"] == pytest.approx(7 / 45)
    assert len(result["best_cells"]) == 45


def test_frontier_dp_matches_independent_brute_force_for_mixed_rotated_groups():
    # Only a 3x3 corner is unrevealed. Two dominoes of A and one square of B.
    free = {r * 9 + c for r in range(3) for c in range(3)}
    empty = sorted(set(range(45)) - free)
    choices = []
    for sizes in [{(1, 2), (2, 1)}, {(2, 2)}]:
        placements = set()
        for h, w in sizes:
            for r in range(5 - h + 1):
                for c in range(9 - w + 1):
                    cells = frozenset(rr * 9 + cc for rr in range(r, r + h) for cc in range(c, c + w))
                    if cells <= free:
                        placements.add(cells)
        choices.append(sorted(placements, key=lambda cells: tuple(sorted(cells))))
    total, counts = 0, [[0] * 45 for _ in range(3)]
    for a, b in product(combinations(choices[0], 2), choices[1]):
        if a[0] & a[1] or (a[0] | a[1]) & b:
            continue
        total += 1
        for cell in a[0] | a[1]:
            counts[0][cell] += 1
        for cell in b:
            counts[1][cell] += 1
    assert total > 0
    result = request("baas.inventory-calculator", "solve", {
        "items": [item(2, 1, 2), item(2, 2, 1), item()], "empty_cells": empty,
    })
    assert result["total_arrangements"] == str(total)
    for cell, row in enumerate(result["cells"]):
        assert row["group_a"] == pytest.approx(counts[0][cell] / total)
        assert row["group_b"] == pytest.approx(counts[1][cell] / total)
        if cell in empty:
            assert row["revealed"] and row["any_item"] == 0


def test_found_items_are_fixed_and_subtracted_from_total_count():
    result = request("baas.inventory-calculator", "solve", {
        "items": [item(2, 1, 1), item(1, 1, 1), item()],
        "empty_cells": [2],
        "placed": [{"item_index": 0, "row": 0, "col": 0, "rotated": True}],
    })
    assert result["total_arrangements"] == "42"
    for cell in (0, 9):
        assert result["cells"][cell]["group_a"] == 1
        assert result["cells"][cell]["revealed"]
        assert result["cells"][cell]["group_b"] == 0
    assert result["cells"][2]["any_item"] == 0
    assert result["cells"][1]["group_b"] == pytest.approx(1 / 42)


def test_impossible_board_and_resource_limit_never_return_fake_probabilities(monkeypatch):
    module = load("baas.inventory-calculator")
    impossible = module.solve({}, {"items": [item(4, 4, 3), item(), item()]})
    assert impossible["status"] == "impossible" and impossible["cells"] == []
    monkeypatch.setattr(module, "MAX_STATES", 1)
    limited = module.solve({}, {"items": [item(2, 1, 1), item(), item()]})
    assert limited["status"] == "limit"
    assert limited["cells"] == [] and limited["best_cells"] == []


@pytest.mark.parametrize("inputs", [
    {"items": [item(True, 1, 1), item(), item()]},
    {"items": [item(5, 1, 1), item(), item()]},
    {"empty_cells": [0, 0]},
    {"items": [item(2, 2, 1), item(), item()], "placed": [{"item_index": 0, "row": 4, "col": 8, "rotated": False}]},
    {"items": [item(2, 1, 1), item(), item()], "empty_cells": [0], "placed": [{"item_index": 0, "row": 0, "col": 0, "rotated": False}]},
    {"items": [item(1, 1, 0), item(), item()], "placed": [{"item_index": 0, "row": 0, "col": 0, "rotated": False}]},
])
def test_invalid_board_is_rejected_without_silent_clamping(inputs):
    with pytest.raises((ValueError, KeyError)):
        load("baas.inventory-calculator").solve({}, inputs)


def test_real_cn_fixed_drop_reference_example_and_multiplier():
    result = request("baas.equipment-planner", "estimate", {"sweeps": "1-1-3,1-1-2", "drop_multiplier": 2})
    assert result["normal_sweep"] == "1-1-5"
    assert result["hard_sweep"] == ""
    assert result["total_ap"] == 50
    assert {row["equipment_id"]: row["expected"] for row in result["yields"]} == {
        "gloves_t1": 10, "hat_t1": 10, "shoes_t1": 10,
    }


def test_gear_targets_subtract_owned_and_respect_unlocked_and_run_limits():
    result = request("baas.equipment-planner", "plan", {
        "needs": [{"slot": "hat", "tier": 1, "quantity": 10, "owned": 6}],
        "max_area": 1, "normal_runs_limit": 3,
    })
    assert result["status"] == "partial"
    assert sum(row["runs"] for row in result["plan"]) == 3
    assert result["remaining"] == [{"slot": "hat", "tier": 1, "quantity": 1}]
    assert result["total_ap"] == 30
    covered = request("baas.equipment-planner", "plan", {
        "needs": [{"slot": "necklace", "tier": 9, "quantity": 5, "owned": 5}],
    })
    assert covered["status"] == "covered" and covered["plan"] == []


def test_hard_equipment_plan_caps_each_stage_and_uses_backend_hard_syntax():
    result = request("baas.equipment-planner", "plan", {
        "needs": [{"slot": "hat", "tier": 1, "quantity": 100, "owned": 0}],
        "include_normal": False, "include_hard": True, "max_area": 1,
        "hard_runs_per_stage": 3,
    })
    assert result["status"] == "partial"
    assert result["plan"] and all(row["runs"] <= 3 for row in result["plan"])
    assert result["normal_sweep"] == ""
    assert "h" not in result["hard_sweep"]


def test_reference_strategies_select_fixed_vs_random_drops_without_discounting_actual_yields(monkeypatch):
    module = load("baas.equipment-planner")
    monkeypatch.setattr(module, "_table", lambda: {
        "equipment": [{"id": "hat_t1", "slot": "hat", "tier": 1, "kind": "gear"}],
        "stages": [
            {"id": "1-1", "ap": 10, "expected": {"hat_t1": 2}, "fixed": {}},
            {"id": "1-2", "ap": 10, "expected": {"hat_t1": 1}, "fixed": {"hat_t1": 1}},
        ],
    })
    values = {"needs": [{"slot": "hat", "tier": 1, "quantity": 2}], "max_stages": 1}
    random_plan = module.plan({}, {**values, "strategy": "high_random"})
    fixed_plan = module.plan({}, {**values, "strategy": "low_fixed"})
    assert random_plan["plan"] == [{"stage_id": "1-1", "runs": 1, "ap": 10}]
    assert fixed_plan["plan"] == [{"stage_id": "1-2", "runs": 2, "ap": 20}]
    assert random_plan["remaining"] == fixed_plan["remaining"] == []


def test_eleph_rank_costs_actual_cn_stages_daily_yields_and_completed_goal():
    result = request("baas.equipment-planner", "eleph", {
        "students": [{"name": "优香", "current_rank": "S2", "target_rank": "S3", "owned": 5}],
        "max_area": 4,
    })
    student = result["students"][0]
    assert student["fragments_needed"] == 75  # S2→S3 requires 80.
    assert student["expected_daily"] == pytest.approx(2.4)  # H1-1 + H4-3, 0.4 each × 3 runs.
    assert student["estimated_days"] == 32
    assert result["hard_sweep"] == "1-1-3,4-3-3"
    assert result["daily_ap"] == 120
    done = request("baas.equipment-planner", "eleph", {
        "students": [{"name": "优香", "current_rank": "S5", "target_rank": "Z2", "owned": 120}],
    })
    assert done["hard_sweep"] == "" and done["daily_ap"] == 0
    assert done["students"][0]["status"] == "covered"


@pytest.mark.parametrize("text", ["1-1-3junk", "unknown-1-2", "h99-1-3", "1-1-0", "1-1--2", "1-1-999999"])
def test_edited_sweeps_reject_partial_parse_or_unknown_stage(text):
    with pytest.raises(ValueError):
        load("baas.equipment-planner").estimate({}, {"sweeps": text})
