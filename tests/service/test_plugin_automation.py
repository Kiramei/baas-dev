from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid
from zipfile import ZipFile

import pytest

from service.plugins import PluginError, PluginManager
from service.plugins.automation import configuration_overlay, run_auto_push, validate_auto_settings, _clear_ap, _read_ap, run_hoard
from service.plugins.automation_current import mission_label
from service.plugins.hoard_plan import build_hoard_plan, tube_cost
from service.plugins.schema import validate_manifest, validate_value, with_defaults
from service.runtime import ServiceRuntime

BUNDLED = Path(__file__).resolve().parents[2] / 'service/plugins/bundled'
NOW = datetime(2026, 10, 28, 10, tzinfo=timezone(timedelta(hours=8)))


def settings(plugin='baas.hoard-ap', **changes):
    manifest = json.loads((BUNDLED / plugin / 'plugin.json').read_text())
    validate_manifest(manifest)
    result = with_defaults(manifest['settings_schema'], changes)
    validate_value(manifest['settings_schema'], result)
    return result


def hoard(**changes):
    return settings(target_at=(NOW + timedelta(hours=2)).isoformat(), **changes)


def test_hoard_plan_excludes_unverified_club_delivery_from_ap_and_target_assets():
    result = build_hoard_plan(hoard(), now=NOW)
    assert result['executable'] is False
    assert result['summary'] == {'character_ap': 320, 'mail_ap': 0, 'cafe_ap': 61,
                                 'total_ap': 381, 'manual_pyroxene_budget': 0, 'cafe_capacity_loss': 0}
    assert result['pending_mail_ap'] == 10
    assert result['available_at_target'] == {'character_ap': 320, 'mail_ap': 0, 'cafe_ap': 61, 'total_ap': 381}
    assert result['warnings'] == ['CLUB_REWARD_REQUIRES_MAIL_VERIFICATION']
    assert [s['action'] for s in result['steps']] == ['free', 'daily', 'club', 'ready']
    club = result['steps'][2]
    assert club['ap_before'] == club['ap_after'] == 320
    assert club['mail_ap'] == 0 and club['expected_mail_ap'] == club['amount'] == 10
    assert club['delivery'] == 'mail' and club['verification'] == 'manual_mail'
    assert 'expires_at' not in club and 'overflow_ap' not in club
    assert all(0 <= s['ap_after'] <= 999 for s in result['steps'])


def test_mail_deadline_clears_real_headroom_and_never_rehoards_mail():
    result = build_hoard_plan(hoard(current_ap=900, daily_ap=0, club_ap=0, claim_free_ap=False,
                                  mail=[{'amount': 200, 'expires_at': (NOW + timedelta(hours=1)).isoformat()}]), now=NOW)
    clear, claim, ready = result['steps']
    assert clear['action'] == 'clear' and clear['target_ap'] == 799 and clear['ap_after'] == 790
    assert claim['action'] == 'mail' and claim['ap_after'] == 990 and claim['mail_ap'] == 0
    assert datetime.fromisoformat(claim['at']) == NOW + timedelta(minutes=50)
    assert ready['action'] == 'ready'


def test_mail_overflow_has_24_hour_lifetime_and_is_rescued_before_expiry():
    result = build_hoard_plan(settings(target_at=(NOW + timedelta(days=2)).isoformat(), current_ap=999,
                                      claim_free_ap=False, cafe_rate=0), now=NOW)
    claims = [s for s in result['steps'] if s['action'] == 'mail']
    assert claims and all(0 <= s['ap_after'] <= 999 for s in result['steps'])
    assert all(s['mail_ap'] == 0 for s in claims)
    assert any(s['action'] == 'clear' and s['reason'] == 'expiring_mail_headroom' for s in result['steps'])


def test_premium_budget_counts_purchases_already_made_but_never_claims_auto_execution():
    cfg = settings(target_at=(NOW + timedelta(days=1)).isoformat(), tube_count=3, tube_days=2, purchases_today=2)
    result = build_hoard_plan(cfg, now=NOW)
    assert result['summary']['manual_pyroxene_budget'] == 120
    assert [s['count'] for s in result['steps'] if s['action'] == 'manual_purchase'] == [1, 3]
    assert result['executable'] is False and 'PREMIUM_PURCHASES_ARE_MANUAL' in result['warnings']
    assert tube_cost(6) == 270 and tube_cost(3, 3) == 180


def test_oversized_mail_is_explicitly_manual_instead_of_simulated_as_collected():
    expiry = (NOW + timedelta(hours=1)).isoformat()
    result = build_hoard_plan(hoard(mail=[{'amount': 600, 'expires_at': expiry}, {'amount': 500, 'expires_at': expiry}]), now=NOW)
    assert result['executable'] is False
    assert result['steps'][-1]['action'] == 'manual_mail'
    assert 'MAIL_REQUIRES_SELECTIVE_COLLECTION' in result['warnings']


@pytest.mark.parametrize('target', ['2026-10-28T12:00:00', 'garbage', NOW.isoformat(), (NOW + timedelta(days=8)).isoformat()])
def test_hoard_rejects_ambiguous_past_or_unbounded_targets(target):
    with pytest.raises(PluginError):
        build_hoard_plan(settings(target_at=target), now=NOW)


def test_expired_mail_and_invalid_cafe_inventory_are_rejected():
    with pytest.raises(PluginError, match='expired'):
        build_hoard_plan(hoard(mail=[{'amount': 10, 'expires_at': NOW.isoformat()}]), now=NOW)
    with pytest.raises(PluginError, match='capacity'):
        build_hoard_plan(hoard(cafe_stock=500, cafe_capacity=100), now=NOW)


def test_regeneration_strategy_actually_schedules_ap_consumption_before_stacking():
    cfg = settings(target_at=(NOW + timedelta(days=3)).isoformat(), strategy='regeneration')
    result = build_hoard_plan(cfg, now=NOW)
    assert any(s['action'] == 'clear' and s['reason'] == 'natural_recovery' for s in result['steps'])
    assert all(a['at'] <= b['at'] for a, b in zip(result['steps'], result['steps'][1:]))


class DiskConfig:
    def __init__(self, path):
        self.path = path
        self.config = SimpleNamespace(**json.loads(path.read_text()))

    def get(self, key, default=None):
        self.config = SimpleNamespace(**json.loads(self.path.read_text()))
        return getattr(self.config, key, default)

    def set(self, key, value):
        data = json.loads(self.path.read_text())
        data[key] = value
        self.path.write_text(json.dumps(data))
        self.config = SimpleNamespace(**data)


def fake_baas(tmp_path):
    directory = tmp_path / 'config/one'
    directory.mkdir(parents=True)
    path = directory / 'config.json'
    path.write_text(json.dumps({'name': 'original', 'choose_team_method': 'order', 'explore_normal_task_list': '7',
                                'main_story_regions': '1', 'side_team_attribute': [['burst']],
                                'unfinished_normal_tasks': [[7, 1, 'max']], 'asset_count': 1}))
    (directory / 'event.json').write_text('[]')
    config = DiskConfig(path)
    baas = SimpleNamespace(config_set=config, config=SimpleNamespace(**vars(config.config)), flag_run=True,
                           logger=SimpleNamespace(), scheduler=object())
    return baas, path


def test_overlay_restores_user_settings_on_failure_while_persisting_inventory(tmp_path):
    baas, path = fake_baas(tmp_path)
    original_set, original_config = baas.config_set, baas.config
    with pytest.raises(RuntimeError):
        with configuration_overlay(baas, {'explore_normal_task_list': '12', 'unfinished_normal_tasks': [[12, 1, 3]]}):
            assert baas.config_set.get('explore_normal_task_list') == '12'
            baas.config_set.set('unfinished_normal_tasks', [])
            baas.config_set.set('asset_count', 42)
            raise RuntimeError('route failed')
    assert baas.config_set is original_set and baas.config is original_config
    data = json.loads(path.read_text())
    assert data['explore_normal_task_list'] == '7' and data['unfinished_normal_tasks'] == [[7, 1, 'max']]
    assert data['asset_count'] == 42


def test_auto_push_applies_independent_team_plan_to_real_route_without_persisting_it(tmp_path):
    baas, path = fake_baas(tmp_path)
    seen = []
    def solve(task):
        seen.append((task, baas.config_set.config.explore_normal_task_list,
                     baas.config.choose_team_method, baas.config.side_team_attribute))
        return True
    baas.solve = solve
    values = settings('baas.auto-push', normal_stages='12,13-1', team_method='side', team_plan=[['burst', 'mystic']])
    assert run_auto_push(baas, 'normal', values)['completed'] is True
    assert seen == [('explore_normal_task', '12,13-1', 'side', [['burst', 'mystic']])]
    assert json.loads(path.read_text())['explore_normal_task_list'] == '7'


@pytest.mark.parametrize('changes', [{'normal_stages': '1-0'}, {'hard_stages': '1-5'}, {'story_episodes': '1-3'},
                                    {'team_method': 'side'}, {'team_method': 'side', 'team_plan': [['unused', 'unused', 'unused', 'unused', 'burst']]},
                                    {'team_method': 'preset', 'team_plan': [['unused']]}])
def test_auto_push_rejects_invalid_routes_and_empty_team_plans(changes):
    with pytest.raises(PluginError):
        validate_auto_settings(settings('baas.auto-push', **changes))


def test_takeover_labels_are_strict_and_do_not_invent_stage_ids():
    assert mission_label('12-3') == (12, 3)
    assert mission_label('9-A') == (9, 6)
    assert mission_label('buy 120') is None and mission_label('1-0') is None


def test_failed_ocr_does_not_become_999_or_trigger_ap_spending(tmp_path):
    baas, path = fake_baas(tmp_path)
    baas.server = 'CN'
    baas.to_main_page = lambda: None
    baas.update_screenshot_array = lambda: None
    baas.ocr = SimpleNamespace(get_region_res=lambda *args: '')
    baas.solve = lambda *args: pytest.fail('No game action is allowed after failed OCR')
    with pytest.raises(PluginError, match='verified'):
        _clear_ap(baas, 'normal_task', 400)


def test_bounded_clear_uses_verified_ap_and_does_not_empty_the_original_sweep_queue(tmp_path):
    baas, path = fake_baas(tmp_path)
    baas.server = 'CN'
    state = {'ap': 160}
    baas.to_main_page = lambda: None
    baas.update_screenshot_array = lambda: None
    baas.ocr = SimpleNamespace(get_region_res=lambda *args: f"{state['ap']}/160")
    def solve(task):
        assert task == 'normal_task'
        row = baas.config.unfinished_normal_tasks[0]
        assert row == [7, 1, 6]
        state['ap'] -= row[2] * 10
        baas.config_set.set('unfinished_normal_tasks', [])
        return True
    baas.solve = solve
    assert _clear_ap(baas, 'normal_task', 100) == 100
    assert json.loads(path.read_text())['unfinished_normal_tasks'] == [[7, 1, 'max']]


def test_external_package_cannot_claim_bundled_host_automation_even_with_matching_id(tmp_path):
    manifest = json.loads((BUNDLED / 'baas.auto-push/plugin.json').read_text())
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('plugin.json', json.dumps(manifest))
    manager = PluginManager(tmp_path, object())
    async def run():
        with pytest.raises(PluginError) as error:
            await manager.install(stream.getvalue())
        assert error.value.code == 'UNSUPPORTED_AUTOMATION'
    asyncio.run(run())


def test_runtime_exclusive_ownership_cancel_restores_overlay_and_allows_regular_task(tmp_path):
    baas, path = fake_baas(tmp_path)
    runtime = ServiceRuntime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def solve(task):
        calls.append((task, baas.config.explore_normal_task_list))
        if len(calls) == 1:
            entered.set()
            while baas.flag_run and not release.wait(.01):
                pass
        return True
    def send(command, task=None):
        if command == 'stop':
            baas.flag_run = False
            release.set()
            return True
        return solve(task)
    baas.solve, baas.send = solve, send
    session = SimpleNamespace(baas=baas, thread=None)
    runtime._sessions['one'] = session
    async def scenario():
        await runtime.plugins.set_enabled('one', {'plugin_id': 'baas.auto-push', 'enabled': True})
        config = await runtime.plugins.settings('one', 'baas.auto-push')
        await runtime.plugins.update_settings('one', {'plugin_id': 'baas.auto-push', 'revision': config['revision'],
                                                      'values': {**config['values'], 'normal_stages': '12'}})
        job = await runtime.plugins.run('one', {'plugin_id': 'baas.auto-push', 'action_id': 'normal', 'request_id': str(uuid.uuid4())})
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        assert (await runtime.solve_task('one', 'lesson'))['status'] == 'already-running'
        assert (await runtime.plugins.cancel('one', job['id']))['state'] == 'cancelled'
        assert baas.config.explore_normal_task_list == '7'
        assert json.loads(path.read_text())['explore_normal_task_list'] == '7'
        assert (await runtime.solve_task('one', 'lesson'))['status'] == 'ok'
        if session.thread is not None:
            await asyncio.to_thread(session.thread.join, 1)
        assert calls[0] == ('explore_normal_task', '12') and calls[-1] == ('lesson', '7')
    asyncio.run(scenario())


def live_ap_baas(tmp_path, initial=160):
    baas, path = fake_baas(tmp_path)
    state = {'ap': initial, 'time': NOW.timestamp(), 'calls': []}
    baas.server = 'CN'
    baas.to_main_page = lambda: None
    baas.update_screenshot_array = lambda: None
    baas.ocr = SimpleNamespace(get_region_res=lambda *args: f"{state['ap']}/160")
    def solve(task):
        state['calls'].append(task)
        state['ap'] += {'collect_daily_power': 150, 'collect_daily_free_power': 10}.get(task, 0)
        return True
    baas.solve = solve
    return baas, state


def test_hoard_executor_reads_actual_ap_and_follows_timeline_without_real_device_io(tmp_path):
    baas, state = live_ap_baas(tmp_path)
    values = settings(target_at=(NOW + timedelta(seconds=3)).isoformat(), current_ap=999, club_ap=0, cafe_rate=0)
    result = run_hoard(baas, values, clock=lambda: state['time'], sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert result['completed'] is True and result['actual_ap'] == 320
    assert state['calls'] == ['collect_daily_free_power', 'collect_daily_power']
    assert result['plan']['summary']['character_ap'] == 320


@pytest.mark.parametrize('server', ['CN', 'Global', 'JP'])
def test_jjc_plan_buys_only_ap_goods_using_the_actual_server_catalog(tmp_path, server):
    from core.config.default_config import STATIC_DEFAULT_CONFIG
    baas, state = live_ap_baas(tmp_path)
    baas.server = server
    catalog = json.loads(STATIC_DEFAULT_CONFIG)['tactical_challenge_shop_price_list']
    baas.static_config = SimpleNamespace(tactical_challenge_shop_price_list=catalog)
    seen = []
    def solve(task):
        assert task == 'tactical_challenge_shop'
        selected = [catalog[server][i][0] for i, count in enumerate(baas.config.TacticalChallengeShopList) if count]
        seen.extend(selected)
        state['ap'] += 90
        return True
    baas.solve = solve
    values = settings(target_at=(NOW + timedelta(seconds=3)).isoformat(), daily_ap=0, club_ap=0,
                      claim_free_ap=False, jjc_mode='30_60', cafe_rate=0)
    result = run_hoard(baas, values, clock=lambda: state['time'], sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert seen == ['30AP', '60AP'] and result['actual_ap'] == 250
    assert not hasattr(baas.config, 'TacticalChallengeShopList')


def test_target_auto_spend_includes_cafe_and_mail_with_explicit_withdrawals():
    result = build_hoard_plan(hoard(current_ap=500, cafe_stock=200, cafe_rate=0, claim_free_ap=False,
                                  daily_ap=0, club_ap=0, spend_at_target=True,
                                  mail=[{'amount': 100, 'expires_at': (NOW + timedelta(hours=5)).isoformat()}]), now=NOW)
    assert [step['action'] for step in result['steps']].count('spend') == 3
    assert any(step['action'] == 'cafe' and step['amount'] == 200 for step in result['steps'])
    assert any(step['action'] == 'mail' and step['amount'] == 100 for step in result['steps'])


def test_runtime_reports_specific_host_error_and_restores_config_on_failure(tmp_path):
    baas, path = fake_baas(tmp_path)
    runtime = ServiceRuntime(tmp_path)
    runtime._sessions['one'] = SimpleNamespace(baas=baas, thread=None)
    def solve(task):
        raise PluginError('TEAM_PLAN_REQUIRED', 'No matching team.')
    baas.solve = solve
    async def scenario():
        with pytest.raises(PluginError) as error:
            await runtime.run_plugin_automation('one', 'baas.auto-push', 'normal', settings('baas.auto-push'))
        assert error.value.code == 'TEAM_PLAN_REQUIRED'
        assert baas.config.explore_normal_task_list == '7'
    asyncio.run(scenario())


def test_date_time_schema_hint_is_narrow_and_preserves_empty_initial_settings():
    from service.plugins.schema import validate_schema
    validate_schema({'type': 'string', 'format': 'date-time', 'default': ''})
    validate_value({'type': 'string', 'format': 'date-time'}, '')
    for schema in ({'type': 'string', 'format': 'url'}, {'type': 'integer', 'format': 'date-time'},
                   {'type': 'string', 'format': ['date-time']}):
        with pytest.raises(PluginError) as error:
            validate_schema(schema)
        assert error.value.code == 'INVALID_SCHEMA'
    manifest = json.loads((BUNDLED / 'baas.hoard-ap/plugin.json').read_text())
    props = manifest['settings_schema']['properties']
    assert props['target_at']['format'] == props['mail']['items']['properties']['expires_at']['format'] == 'date-time'


def test_failed_small_mail_claim_is_not_hidden_by_an_ocr_tolerance(tmp_path):
    baas, state = live_ap_baas(tmp_path)
    values = settings(target_at=(NOW + timedelta(minutes=10)).isoformat(), daily_ap=0, club_ap=0,
                      claim_free_ap=False, cafe_rate=0,
                      mail=[{'amount': 3, 'expires_at': (NOW + timedelta(minutes=5)).isoformat()}])
    baas.solve = lambda task: True  # Host completed its scan but claimed nothing.
    with pytest.raises(PluginError) as error:
        run_hoard(baas, values, clock=lambda: state['time'], sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert error.value.code == 'SOURCE_NOT_VERIFIED'


def test_overflow_sources_are_manual_until_mailbox_gain_can_be_verified(tmp_path):
    values = hoard(current_ap=999, claim_free_ap=False)
    plan = build_hoard_plan(values, now=NOW)
    assert plan['executable'] is False and 'OVERFLOW_REQUIRES_MAIL_VERIFICATION' in plan['warnings']
    baas, state = live_ap_baas(tmp_path, 999)
    with pytest.raises(PluginError) as error:
        run_hoard(baas, values, clock=lambda: state['time'], sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert error.value.code == 'MANUAL_STEP_REQUIRED' and state['calls'] == []


def test_current_story_initializes_grid_route_data_for_a_fresh_session(tmp_path, monkeypatch):
    from module import main_story
    from module import main_story
    from core import picture
    from service.plugins.automation_current import push_current_story
    baas, _ = fake_baas(tmp_path)
    screens = iter(['main_story_episode-info', 'main_story_plot-not-open'])
    monkeypatch.setattr(picture, 'co_detect', lambda *args, **kwargs: next(screens))
    route_data = {'example': {'start': []}}
    monkeypatch.setattr(main_story, 'get_stage_data', lambda: route_data)
    observed = []
    def clear(active, _skip=False):
        observed.append(active.main_story_stage_data)
        return True
    monkeypatch.setattr(main_story, 'clear_current_plot', clear)
    assert not hasattr(baas, 'main_story_stage_data')
    assert push_current_story(baas) is True and observed == [route_data]


def test_current_story_unknown_screen_fails_before_clicking(tmp_path, monkeypatch):
    from module import main_story
    from core import picture
    from service.plugins.automation_current import push_current_story
    baas, _ = fake_baas(tmp_path)
    baas.click = lambda *args, **kwargs: pytest.fail('Unknown screens must not be clicked')
    monkeypatch.setattr(picture, 'co_detect', lambda *args, **kwargs: 'unknown_blue_button')
    with pytest.raises(PluginError) as error:
        push_current_story(baas)
    assert error.value.code == 'UNSUPPORTED_PAGE'


def test_current_hard_stage_settles_and_reopens_between_multiple_objectives(tmp_path, monkeypatch):
    from module import main_story
    from core import image, picture
    from module.explore_tasks import explore_task, task_utils
    from service.plugins.automation_current import push_current_stage
    baas, _ = fake_baas(tmp_path)
    baas.config.explore_task_use_simple_mode = False
    baas.update_screenshot_array = lambda: None
    baas.swipe = lambda *args, **kwargs: None
    baas.ocr = SimpleNamespace(get_region_res=lambda *args: '12-1')
    state = {'screen': 'list', 'fights': 0, 'opened': 0, 'judged': []}
    monkeypatch.setattr(picture, 'co_detect', lambda *args, **kwargs: 'event_hard')
    monkeypatch.setattr(image, 'get_image_all_appear_position', lambda *args, **kwargs: [(1100, 250)] if state['fights'] < 2 else [])
    monkeypatch.setattr(task_utils, 'get_stage_data', lambda *args: {'12-1-sss': {}, '12-1-present': {}})
    def open_mission(*args):
        assert state['screen'] == 'list'
        state.update(screen='info', opened=state['opened'] + 1)
        return True
    monkeypatch.setattr(task_utils, 'to_mission_info', open_mission)
    def need_fight(_baas, name, normal):
        assert state['screen'] == 'info' and normal is False
        state['judged'].append(name)
        return True
    monkeypatch.setattr(explore_task, 'need_fight', need_fight)
    monkeypatch.setattr(explore_task, 'set_explore_task_mode', lambda *args: None)
    monkeypatch.setattr(task_utils, 'execute_grid_task', lambda *args: True)
    def fight(*args):
        state.update(screen='battle', fights=state['fights'] + 1)
    monkeypatch.setattr(main_story, 'auto_fight', fight)
    def settle(*args):
        assert state['screen'] in {'battle', 'list'}
        state['screen'] = 'list'
    monkeypatch.setattr(task_utils, 'to_normal_event', settle)
    monkeypatch.setattr(task_utils, 'to_hard_event', settle)
    assert push_current_stage(baas) is True
    assert state['fights'] == state['opened'] == 2 and state['judged'] == ['12-1-sss', '12-1-present']


def test_current_a_stage_skips_nonexistent_grid_mode_selector(tmp_path, monkeypatch):
    from module import main_story
    from core import image, picture
    from module.explore_tasks import explore_task, task_utils
    from service.plugins.automation_current import push_current_stage
    baas, _ = fake_baas(tmp_path)
    baas.config.explore_task_use_simple_mode = False
    baas.update_screenshot_array = lambda: None
    baas.swipe = lambda *args, **kwargs: None
    baas.ocr = SimpleNamespace(get_region_res=lambda *args: '12-A')
    state = {'fought': False}
    monkeypatch.setattr(picture, 'co_detect', lambda *args, **kwargs: 'event_normal')
    monkeypatch.setattr(image, 'get_image_all_appear_position', lambda *args, **kwargs: [] if state['fought'] else [(1100, 250)])
    monkeypatch.setattr(task_utils, 'get_stage_data', lambda *args: {'12-6': {'start': [['burst', [0, 0]]]}})
    monkeypatch.setattr(task_utils, 'to_mission_info', lambda *args: True)
    monkeypatch.setattr(explore_task, 'need_fight', lambda *args: True)
    monkeypatch.setattr(explore_task, 'set_explore_task_mode', lambda *args: pytest.fail('A missions have no grid mode selector'))
    monkeypatch.setattr(task_utils, 'convert_team_config', lambda *args: {'burst': [[0, 1]]})
    monkeypatch.setattr(task_utils, 'employ_units', lambda *args: True)
    monkeypatch.setattr(main_story, 'auto_fight', lambda *args: state.update(fought=True))
    monkeypatch.setattr(task_utils, 'to_normal_event', lambda *args: None)
    monkeypatch.setattr(task_utils, 'to_hard_event', lambda *args: None)
    assert push_current_stage(baas) is True and state['fought'] is True


def test_hoard_clear_uses_current_profile_priority_without_requiring_a_scheduler_run(tmp_path):
    baas, state = live_ap_baas(tmp_path)
    baas.config.mainlinePriority = '12-2-max'
    baas.config.unfinished_normal_tasks = []
    baas.static_config = SimpleNamespace(explore_normal_task_region_range=[1, 30])
    def solve(task):
        assert baas.config.unfinished_normal_tasks == [[12, 2, 6]]
        state['ap'] -= 60
        return True
    baas.solve = solve
    assert _clear_ap(baas, 'normal_task', 100) == 100
    assert baas.config.unfinished_normal_tasks == []
    assert baas.config.mainlinePriority == '12-2-max'


def test_browser_utc_date_encoding_does_not_shift_the_game_reset_or_mail_deadline():
    target = NOW + timedelta(days=1)
    parcel_expiry = NOW + timedelta(hours=23)
    local = settings(target_at=target.isoformat(), mail=[{'amount': 50, 'expires_at': parcel_expiry.isoformat()}])
    encoded = settings(target_at=target.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                       mail=[{'amount': 50, 'expires_at': parcel_expiry.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}])
    a = build_hoard_plan(local, now=NOW)
    b = build_hoard_plan(encoded, now=NOW.astimezone(timezone.utc))
    assert a == b
    reset_claim = next(step for step in a['steps'] if step['action'] == 'daily')
    assert datetime.fromisoformat(reset_claim['at']).hour == 3
    assert datetime.fromisoformat(reset_claim['at']).minute == 50


def test_japan_game_zone_resets_at_four_even_when_browser_transmits_utc():
    result = build_hoard_plan(settings(target_at=(NOW + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                                       server_utc_offset_minutes=540), now=NOW)
    first_claim = next(step for step in result['steps'] if step['action'] == 'daily')
    at = datetime.fromisoformat(first_claim['at'])
    assert at.hour == 3 and at.minute == 50 and at.utcoffset() == timedelta(hours=9)


def test_club_delivery_has_no_invented_expiry_and_never_becomes_spendable_inventory():
    result = build_hoard_plan(settings(target_at=(NOW + timedelta(days=3)).isoformat(),
                                       current_ap=160, cafe_rate=0, daily_ap=0,
                                       claim_free_ap=False, spend_at_target=True), now=NOW)
    club = [step for step in result['steps'] if step['action'] == 'club']
    assert len(club) == 4  # The current day plus three future sign-ins.
    assert result['pending_mail_ap'] == 40 and result['executable'] is False
    assert all(step['ap_before'] == step['ap_after'] and step['mail_ap'] == 0 for step in club)
    assert all(step['expected_mail_ap'] == 10 and 'expires_at' not in step and 'overflow_ap' not in step for step in club)
    assert not any(step['action'] in {'mail', 'manual_mail'} for step in result['steps'])
    assert 'OVERFLOW_REQUIRES_MAIL_VERIFICATION' not in result['warnings']
    assert result['available_at_target']['character_ap'] == 610
    assert result['available_at_target']['mail_ap'] == result['summary']['mail_ap'] == 0
    assert result['available_at_target']['total_ap'] == 610


def test_confirmed_mail_keeps_its_explicit_deadline_separate_from_unverified_club_delivery():
    expiry = NOW + timedelta(hours=25)
    result = build_hoard_plan(settings(target_at=(NOW + timedelta(hours=25)).isoformat(),
                                       current_ap=160, cafe_rate=0, daily_ap=0,
                                       claim_free_ap=False,
                                       mail=[{'amount': 10, 'expires_at': expiry.isoformat()}]), now=NOW)
    # Only the user-entered old mail can be collected. New club deliveries have
    # no verified expiry, cannot inherit the 24h overflow estimate, and stay out.
    claims = [step for step in result['steps'] if step['action'] == 'mail']
    assert len(claims) == 1 and claims[0]['amount'] == 10
    assert datetime.fromisoformat(claims[0]['at']) == expiry - timedelta(minutes=10)
    assert result['pending_mail_ap'] == 20
    assert result['summary']['character_ap'] == 320  # 160 + tomorrow's daily 150 + known mail 10.
    assert result['summary']['mail_ap'] == 0
    assert 'CLUB_REWARD_REQUIRES_MAIL_VERIFICATION' in result['warnings']


def test_club_mail_plan_is_rejected_before_any_source_or_following_step(tmp_path):
    baas, state = live_ap_baas(tmp_path, 999)
    values = settings(target_at=(NOW + timedelta(seconds=3)).isoformat(), daily_ap=0,
                      claim_free_ap=False, cafe_rate=0)
    with pytest.raises(PluginError) as error:
        run_hoard(baas, values, clock=lambda: state['time'],
                  sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert error.value.code == 'MANUAL_STEP_REQUIRED'
    assert state['ap'] == 999 and state['calls'] == [] and state['time'] == NOW.timestamp()


def test_underestimated_source_cannot_report_success_from_a_saturated_ap_counter(tmp_path):
    baas, state = live_ap_baas(tmp_path, 900)
    def solve(task):
        state['calls'].append(task)
        assert task == 'collect_daily_power'
        state['overflow_ap'] = max(0, state['ap'] + 150 - 999)
        state['ap'] = min(999, state['ap'] + 150)
        return True
    baas.solve = solve
    values = settings(target_at=(NOW + timedelta(seconds=3)).isoformat(), daily_ap=50,
                      club_ap=0, claim_free_ap=False, cafe_rate=0)
    with pytest.raises(PluginError) as error:
        run_hoard(baas, values, clock=lambda: state['time'],
                  sleep=lambda seconds: state.update(time=state['time'] + seconds))
    assert error.value.code == 'MAIL_VERIFICATION_REQUIRED'
    assert state['calls'] == ['collect_daily_power'] and state['ap'] == 999 and state['overflow_ap'] == 51
    assert state['time'] < (NOW + timedelta(seconds=3)).timestamp()


@pytest.mark.parametrize('changes', [
    {}, {'club_ap': 0},
    {'current_ap': 999, 'claim_free_ap': False},
    {'tube_count': 3},
    {'spend_at_target': True, 'cafe_stock': 200,
     'mail': [{'amount': 100, 'expires_at': (NOW + timedelta(hours=5)).isoformat()}]},
    {'mail': [{'amount': 600, 'expires_at': (NOW + timedelta(hours=1)).isoformat()},
              {'amount': 500, 'expires_at': (NOW + timedelta(hours=1)).isoformat()}]},
])
def test_hoard_plan_output_schema_covers_pending_delivery_and_interrupted_timelines(changes):
    manifest = json.loads((BUNDLED / 'baas.hoard-ap/plugin.json').read_text())
    output = build_hoard_plan(hoard(**changes), now=NOW)
    validate_value(manifest['actions'][0]['output_schema'], output)
    if output['steps'][-1]['action'] == 'manual_mail':
        assert 'available_at_target' not in output


def test_real_manager_plan_validates_club_output_and_start_refuses_before_device_initialization(tmp_path, monkeypatch):
    from service.plugins import hoard_plan
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
    # Exercise the production manager/runtime with a deterministic game clock,
    # including when CI runs close to the real server-day reset.
    monkeypatch.setattr(hoard_plan, 'datetime', FixedDateTime)
    fake_baas(tmp_path)
    runtime = ServiceRuntime(tmp_path)
    initialized = []
    async def denied(*args, **kwargs):
        initialized.append(True)
        pytest.fail('A manual club plan must not initialize a device or claim any source')
    runtime._run_plugin_owned_task = denied
    target = NOW + timedelta(hours=2)
    old_mail = {'amount': 10, 'expires_at': (target + timedelta(days=3)).isoformat()}
    new_mail = {'amount': 10, 'expires_at': (target + timedelta(days=6)).isoformat()}
    async def finish(job):
        for _ in range(200):
            state = await runtime.plugins.get_job('one', job['id'])
            if state['state'] not in {'queued', 'running'}:
                return state
            await asyncio.sleep(.01)
        pytest.fail('The real plugin job did not settle')
    async def run(action):
        return await finish(await runtime.plugins.run('one', {'plugin_id': 'baas.hoard-ap',
                            'action_id': action, 'input': {}, 'request_id': str(uuid.uuid4())}))
    async def scenario():
        await runtime.plugins.set_enabled('one', {'plugin_id': 'baas.hoard-ap', 'enabled': True})
        stored = await runtime.plugins.settings('one', 'baas.hoard-ap')
        values = {**stored['values'], 'target_at': target.isoformat(), 'current_ap': 999,
                  'soft_cap': 240, 'daily_ap': 0, 'cafe_rate': 0, 'claim_free_ap': False,
                  'mail': [old_mail]}
        await runtime.plugins.update_settings('one', {'plugin_id': 'baas.hoard-ap',
                                             'revision': stored['revision'], 'values': values})
        job = await run('plan')
        assert job['state'] == 'succeeded', job
        result = job['result']
        assert result['executable'] is False and result['pending_mail_ap'] == 10
        assert result['available_at_target']['mail_ap'] == 10
        assert result['available_at_target']['total_ap'] == 1009
        club = next(step for step in result['steps'] if step['action'] == 'club')
        assert club['ap_before'] == club['ap_after'] == 999 and club['mail_ap'] == 10
        assert club['expected_mail_ap'] == 10 and club['delivery'] == 'mail'
        assert result['warnings'] == ['CLUB_REWARD_REQUIRES_MAIL_VERIFICATION', 'MAIL_IS_COLLECTED_AS_A_WHOLE']
        refused = await run('start')
        assert refused['state'] == 'failed' and refused['error']['code'] == 'MANUAL_STEP_REQUIRED'
        assert initialized == []
        # After manual sign-in the user verifies the new mail and explicitly
        # lists it once, disabling today's pending club reward before replanning.
        stored = await runtime.plugins.settings('one', 'baas.hoard-ap')
        await runtime.plugins.update_settings('one', {'plugin_id': 'baas.hoard-ap',
               'revision': stored['revision'], 'values': {**values, 'club_ap': 0, 'mail': [old_mail, new_mail]}})
        replanned = await run('plan')
        assert replanned['state'] == 'succeeded', replanned
        assert replanned['result']['pending_mail_ap'] == 0 and replanned['result']['executable'] is True
        assert replanned['result']['available_at_target']['mail_ap'] == 20
        assert replanned['result']['available_at_target']['total_ap'] == 1019
        assert initialized == []
    asyncio.run(scenario())


def test_forecast_totals_do_not_combine_unspendable_fractions_from_separate_ap_bars():
    result = build_hoard_plan(settings(target_at=(NOW + timedelta(minutes=50)).isoformat(),
                                       current_ap=100, daily_ap=0, club_ap=0, claim_free_ap=False), now=NOW)
    for assets in (result['available_at_target'], result['summary']):
        assert assets['character_ap'] == 108 and assets['cafe_ap'] == 25
        assert assets['total_ap'] == assets['character_ap'] + assets['cafe_ap'] + assets['mail_ap'] == 133
