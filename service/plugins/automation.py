"""Closed host adapters for shipped automation tools.

No entrypoint from an installed package is imported into the BAAS process.
Automation runs under the exact same owned-thread gate as single host tasks.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import math
import re
import time

from .errors import PluginError, require
from .hoard_plan import build_hoard_plan, instant

HOST_ACTIONS = {
    'baas.hoard-ap': frozenset({'plan', 'start'}),
    'baas.auto-push': frozenset({'normal', 'hard', 'story', 'activity_mission',
                                'activity_story', 'activity_challenge', 'current_stage', 'current_story'}),
}
TASKS = {'normal': 'explore_normal_task', 'hard': 'explore_hard_task', 'story': 'main_story',
         'activity_mission': 'explore_activity_mission', 'activity_story': 'explore_activity_story',
         'activity_challenge': 'explore_activity_challenge'}


class ConfigOverlay:
    """Ephemeral task parameters; persistent inventory writes still reach disk."""
    def __init__(self, original, overrides):
        self._original = original
        self._overrides = deepcopy(overrides)
        self.config = deepcopy(original.config)
        for key, value in overrides.items():
            setattr(self.config, key, deepcopy(value))

    def __getattr__(self, name):
        return getattr(self._original, name)

    def get(self, key, default=None):
        return deepcopy(self._overrides[key]) if key in self._overrides else self._original.get(key, default)

    def set(self, key, value):
        if key in self._overrides:
            self._overrides[key] = deepcopy(value)
            setattr(self.config, key, deepcopy(value))
        else:
            self._original.set(key, value)
            setattr(self.config, key, deepcopy(value))

    update = set

    def __getitem__(self, key):
        return self.get(key)


@contextmanager
def configuration_overlay(baas, overrides):
    original_set, original_config = baas.config_set, baas.config
    overlay = ConfigOverlay(original_set, overrides)
    baas.config_set = overlay
    baas.config = deepcopy(original_config)
    for key, value in overrides.items():
        setattr(baas.config, key, deepcopy(value))
    try:
        yield
    finally:
        baas.config_set, baas.config = original_set, original_config


def validate_auto_settings(settings):
    for key, hard in (('normal_stages', False), ('hard_stages', True)):
        entries = settings[key].split(',')
        require(0 < len(entries) <= 100 and all(re.fullmatch(r'\s*\d{1,2}(?:-' + ('[1-3]' if hard else '[1-5A]') + r')?\s*', item)
                                                  for item in entries),
                'INVALID_MISSIONS', 'Use comma-separated chapters or missions, such as 12,13-1,13-2.')
    story = settings['story_episodes'].strip()
    require(not story or all(re.fullmatch(r'[1-9]\d?', item.strip()) for item in story.split(',')),
            'INVALID_EPISODES', 'Use a comma-separated ordered list of positive story episode numbers.')
    if settings['team_method'] != 'profile':
        require(bool(settings['team_plan']), 'TEAM_PLAN_REQUIRED', 'Supply a team attribute plan for the selected team source.')
        if settings['team_method'] == 'side':
            require(len(settings['team_plan']) == 1 and len(settings['team_plan'][0]) <= 4,
                    'INVALID_TEAMS', 'Side teams use exactly one group with at most four formation slots.')
        require(any(attribute != 'unused' for group in settings['team_plan'] for attribute in group),
                'INVALID_TEAMS', 'At least one formation must have a damage attribute.')


def _activity_supported(baas, action):
    require(bool(baas.current_game_activity), 'UNSUPPORTED_ACTIVITY', 'The backend has no supported current activity.')
    # current_game_activity is selected by the host, never a plugin module path.
    try:
        module = importlib.import_module('module.activities.' + baas.current_game_activity)
        function = 'explore_' + action.split('_', 1)[1] if action.startswith('activity_') else 'sweep'
        require(callable(getattr(module, function, None)), 'UNSUPPORTED_ACTIVITY', 'This activity does not implement the requested task.')
    except ImportError:
        raise PluginError('UNSUPPORTED_ACTIVITY', 'This activity has no installed automation route.') from None


def run_auto_push(baas, action, settings):
    validate_auto_settings(settings)
    overrides = {'explore_normal_task_list': settings['normal_stages'],
                 'explore_hard_task_list': settings['hard_stages'],
                 'main_story_regions': settings['story_episodes'],
                 'explore_task_use_simple_mode': settings['simple_mode'], 'manual_boss': settings['manual_boss']}
    if settings['team_method'] != 'profile':
        method = settings['team_method']
        overrides.update(choose_team_method=method,
                         **{('side_team_attribute' if method == 'side' else 'preset_team_attribute'): settings['team_plan']})
    with configuration_overlay(baas, overrides):
        if action.startswith('activity_'):
            _activity_supported(baas, action)
        if action.startswith('current_'):
            from .automation_current import push_current_stage, push_current_story
            function = push_current_stage if action == 'current_stage' else push_current_story
            completed = function(baas)
        else:
            completed = baas.solve(TASKS[action])
        require(completed is not False, 'DEVICE_TASK_FAILED', 'The host route did not finish successfully.')
    return {'completed': True, 'action': action, 'settings_applied_to_profile': False}


def _read_ap(baas):
    """Do not use get_ap's 999-on-OCR-failure fallback for purchase/clear guards."""
    baas.to_main_page()
    baas.update_screenshot_array()
    region = (485, 23, 586, 54) if baas.server == 'CN' else (536, 26, 619, 44)
    text = baas.ocr.get_region_res(baas, region, 'en-us', 'AP', '0123456789/')
    match = re.fullmatch(r'\s*(\d{1,3})(?:\s*/\s*\d{1,3})?\s*', str(text))
    require(match is not None and int(match[1]) <= 999, 'AP_OCR_FAILED', 'Current AP could not be verified; automation stopped.')
    return int(match[1])



def _sweep_rows(baas, normal):
    priority = getattr(baas.config, 'mainlinePriority' if normal else 'hardPriority', None)
    if priority is None:
        # Compatibility for older host adapters; current Config always has the
        # authoritative priority list. Never refresh or consume its disk queue.
        return deepcopy(getattr(baas.config, 'unfinished_normal_tasks' if normal else 'unfinished_hard_tasks', []))
    entries = priority.split(',') if isinstance(priority, str) else priority
    require(isinstance(entries, list), 'CLEAR_ROUTE_REQUIRED', 'Configure a valid sweep priority list first.')
    maximum = getattr(baas.static_config, 'explore_normal_task_region_range' if normal else 'explore_hard_task_region_range')[1]
    rows = []
    for entry in entries:
        match = re.fullmatch(r'\s*([1-9]\d?)-([1-5])-([1-9]\d*|max)\s*', str(entry))
        require(match is not None and int(match[1]) <= maximum and (normal or int(match[2]) <= 3),
                'CLEAR_ROUTE_REQUIRED', 'The selected sweep priority contains an unsupported stage or count.')
        rows.append([int(match[1]), int(match[2]), match[3]])
    return rows


def _clear_ap(baas, task, target):
    """Consume bounded runs of the chosen profile route, measuring each pass."""
    current = _read_ap(baas)
    for _ in range(5):
        if current <= target:
            return current
        cost = 10 if task == 'normal_task' else 20 if task == 'hard_task' else 15 if task == 'scrimmage' else 40 if task == 'clear_special_task_power' else 10
        if task == 'activity_sweep':
            _activity_supported(baas, 'sweep')
            from module.activities.activity_utils import get_stage_data, preprocess_activity_region
            stages = preprocess_activity_region(baas.config.activity_sweep_task_number)
            require(len(stages) == 1, 'CLEAR_ROUTE_REQUIRED', 'Select exactly one activity mission for bounded AP spending.')
            data = get_stage_data(baas)
            costs = data.get('sweep_ap_cost_mission', [])
            require(0 <= stages[0] < len(costs) and 1 <= costs[stages[0]] <= 100,
                    'UNSUPPORTED_ACTIVITY', 'The activity has no declared AP cost for this mission.')
            cost = costs[stages[0]]
        count = math.ceil((current - target) / cost)
        overrides = {'purchase_scrimmage_ticket_times': 0}
        if task in {'normal_task', 'hard_task'}:
            key = 'unfinished_normal_tasks' if task == 'normal_task' else 'unfinished_hard_tasks'
            configured = _sweep_rows(baas, task == 'normal_task')
            require(bool(configured), 'CLEAR_ROUTE_REQUIRED', 'Configure a sweep stage for the selected spending task first.')
            rows, remaining = [], count
            for row in configured:
                take = min(remaining, 3 if task == 'hard_task' else remaining)
                rows.append([row[0], row[1], take])
                remaining -= take
                if remaining <= 0:
                    break
            overrides[key] = rows
        elif task == 'clear_special_task_power':
            values = str(baas.config.special_task_times).split(',')
            selected = next((i for i, value in enumerate(values) if value.strip() not in {'0', ''}), None)
            require(selected is not None, 'CLEAR_ROUTE_REQUIRED', 'Configure a commission type before spending AP.')
            overrides['special_task_times'] = ','.join(str(count if i == selected else 0) for i in range(2))
        elif task == 'scrimmage':
            values = str(baas.config.scrimmage_times).split(',')
            selected = next((i for i, value in enumerate(values) if value.strip() not in {'0', ''}), None)
            require(selected is not None, 'CLEAR_ROUTE_REQUIRED', 'Configure a scrimmage school before spending AP.')
            overrides['scrimmage_times'] = ','.join(str(count if i == selected else 0) for i in range(3))
        else:
            _activity_supported(baas, 'sweep')
            overrides['activity_sweep_times'] = str(count)
        with configuration_overlay(baas, overrides):
            require(baas.solve(task) is not False, 'DEVICE_TASK_FAILED', 'The selected spending route failed.')
        after = _read_ap(baas)
        require(after < current, 'CLEAR_NO_PROGRESS', 'The spending route did not reduce AP; automation stopped.')
        current = after
    require(current <= target, 'CLEAR_INCOMPLETE', 'The available sweep stages or tickets could not create enough AP headroom.')
    return current


def run_hoard(baas, settings, *, clock=time.time, sleep=time.sleep):
    # The device reading replaces the estimate; never clear based on a stale
    # setting and never infer successful OCR from a fallback return value.
    actual = _read_ap(baas)
    plan = build_hoard_plan({**settings, 'current_ap': actual}, now=datetime.fromtimestamp(clock(), timezone.utc))
    require(plan['executable'], 'MANUAL_STEP_REQUIRED',
            'This plan needs manual premium purchases or selective mail collection. Complete those steps and replan before automatic execution.')
    completed = []
    for step in plan['steps']:
        deadline = instant(step['at']).timestamp()
        while clock() < deadline:
            require(baas.flag_run, 'AUTOMATION_STOPPED', 'Automation was stopped.')
            sleep(min(0.25, max(0, deadline - clock())))
        require(baas.flag_run, 'AUTOMATION_STOPPED', 'Automation was stopped.')
        action = step['action']
        if action == 'ready':
            completed.append({'action': action, 'ap': _read_ap(baas)})
            continue
        if action in {'clear', 'spend'}:
            ap = _clear_ap(baas, settings['spend_task'], step.get('target_ap', 9))
        else:
            before = _read_ap(baas)
            require(before + step['amount'] <= 999, 'MAIL_VERIFICATION_REQUIRED',
                    'Live AP would overflow into mail and cannot be verified by the AP counter. Check the mailbox and replan first.')
            if action == 'cafe':
                from module import cafe_reward
                cafe_reward.to_cafe(baas, True)
                cafe_reward.collect(baas)
            else:
                task = {'daily': 'collect_daily_power', 'club': 'group', 'free': 'collect_daily_free_power',
                        'jjc': 'tactical_challenge_shop', 'mail': 'mail'}[action]
                overrides = {}
                if action == 'jjc':
                    prices = baas.static_config.tactical_challenge_shop_price_list[baas.server]
                    goods = [0] * len(prices)
                    indices = {str(row[0]).upper().replace(' ', ''): i for i, row in enumerate(prices)}
                    require('30AP' in indices and '60AP' in indices, 'UNSUPPORTED_SHOP', 'This server shop has no identifiable AP goods.')
                    # Resolve by the current host catalog, whose order differs
                    # between CN, Global and JP. Never use a fallback index.
                    goods[indices['30AP']] = 1
                    goods[indices['60AP']] = int(settings['jjc_mode'] == '30_60')
                    overrides = {'TacticalChallengeShopList': goods, 'TacticalChallengeShopRefreshTime': settings['jjc_refreshes']}
                with configuration_overlay(baas, overrides):
                    require(baas.solve(task) is not False, 'DEVICE_TASK_FAILED', 'An AP source task failed.')
            ap = _read_ap(baas)
            require(ap >= before + step['amount'], 'SOURCE_NOT_VERIFIED',
                    'The expected AP gain could not be verified; check the source and replan.')
        completed.append({'action': action, 'ap': ap})
    return {'completed': True, 'steps': completed, 'plan': plan, 'actual_ap': _read_ap(baas)}
