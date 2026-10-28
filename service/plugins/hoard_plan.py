"""Deterministic AP/cafe/mail timeline; no game imports or device side effects.

Unlike the retired Qt planner, sources, caps and deadlines are explicit data.
Mail is collected as a whole because the host's mail task has no selective API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import heapq
import math

from .errors import PluginError, require

HARD_CAP = 999
MAIL_LIFETIME = timedelta(hours=24)
MARGIN = timedelta(minutes=10)


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None and result.utcoffset() is not None,
                'INVALID_TIME', 'Times must include an explicit time zone.')
        return result
    except (TypeError, ValueError, AttributeError):
        raise PluginError('INVALID_TIME', 'Use an ISO 8601 date and time with an explicit time zone.') from None


def tube_cost(count: int, already_bought: int = 0) -> int:
    return sum(30 if index < 3 else 60 for index in range(already_bought, already_bought + count))


def build_hoard_plan(settings: dict, *, now: datetime | None = None) -> dict:
    server_zone = timezone(timedelta(minutes=settings['server_utc_offset_minutes']))
    target = instant(settings['target_at']).astimezone(server_zone)
    now = (now or datetime.now(timezone.utc)).astimezone(server_zone)
    require(now < target <= now + timedelta(days=7), 'INVALID_TARGET', 'Spend time must be in the next seven days.')
    require(settings['cafe_stock'] <= settings['cafe_capacity'], 'INVALID_SETTINGS', 'Cafe stock exceeds its capacity.')
    require(settings['purchases_today'] <= settings['tube_count'] or settings['tube_count'] == 0,
            'INVALID_SETTINGS', 'Purchases already made exceed the planned daily purchase count.')
    steps, warnings, mail = [], [], []
    events = []
    sequence = 0
    ap = float(settings['current_ap'])
    cafe = float(settings['cafe_stock'])
    clock = now
    premium_cost = 0
    pending_mail_ap = 0
    discarded = 0
    executable = True
    available_at_target = None

    def enqueue(at, action, amount=0, **metadata):
        nonlocal sequence
        sequence += 1
        heapq.heappush(events, (at, sequence, action, amount, metadata))

    def add_mail(amount, expires):
        if amount:
            mail.append({'amount': int(amount), 'expires_at': expires})
            if expires - MARGIN < target:
                enqueue(max(clock, expires - MARGIN), 'mail')

    for parcel in settings['mail']:
        expires = instant(parcel['expires_at']).astimezone(server_zone)
        require(expires > now, 'EXPIRED_MAIL', 'Remove expired mail before building a plan.')
        add_mail(parcel['amount'], expires)

    # Browser date controls may transmit UTC Z. Server-day boundaries instead
    # use the separately configured game zone, never the wire timestamp zone.
    day_start = now.replace(hour=settings['reset_hour'], minute=0, second=0, microsecond=0)
    if day_start > now:
        day_start -= timedelta(days=1)
    days = []
    while day_start < target:
        days.append(day_start)
        day_start += timedelta(days=1)
    purchase_days = days[-settings['tube_days']:]
    for index, day in enumerate(days):
        claim_at = max(now, min(day + timedelta(days=1) - MARGIN, target - timedelta(minutes=2)))
        if day in purchase_days and settings['tube_count']:
            already = settings['purchases_today'] if index == 0 else 0
            count = max(0, settings['tube_count'] - already)
            if count:
                enqueue(claim_at - timedelta(seconds=1) if claim_at > now else now,
                        'manual_purchase', count * 120, count=count, pyroxene=tube_cost(count, already))
        daily = settings['daily_ap'] if index == 0 else 150
        club = settings['club_ap'] if index == 0 else 10
        if settings['claim_free_ap']:
            enqueue(claim_at, 'free', 10)
        if daily:
            enqueue(claim_at, 'daily', daily)
        if club:
            enqueue(claim_at, 'club', club)
        jjc = {'none': 0, '30': 30, '30_60': 90}[settings['jjc_mode']] * (settings['jjc_refreshes'] + 1)
        if jjc:
            enqueue(claim_at, 'jjc', jjc)
        # Keep the final cafe stock for the spend window, and collect earlier
        # days before capping rather than inventing AP beyond cafe capacity.
        if day + timedelta(days=1) < target:
            enqueue(claim_at, 'cafe')
        if settings['strategy'] == 'regeneration' and day < target - timedelta(hours=24):
            enqueue(max(now, day + timedelta(minutes=1)), 'regenerate')
    enqueue(target, 'ready')

    def advance(at):
        nonlocal clock, ap, cafe, discarded
        hours = (at - clock).total_seconds() / 3600
        if ap < settings['soft_cap']:
            ap = min(settings['soft_cap'], ap + hours * 10)
        produced = cafe + hours * settings['cafe_rate']
        discarded += max(0, produced - settings['cafe_capacity'])
        cafe = min(settings['cafe_capacity'], produced)
        clock = at

    def emit(action, before, amount=0, **metadata):
        steps.append({'at': clock.isoformat(), 'action': action, 'amount': int(amount),
                      'ap_before': math.floor(before), 'ap_after': math.floor(ap),
                      'mail_ap': sum(p['amount'] for p in mail), **metadata})

    def clear_to(limit, reason):
        nonlocal ap
        if ap > limit:
            before = ap
            # Sweeps consume indivisible AP. Planner uses a conservative
            # lower bound; the executor measures the actual result with OCR.
            ap = max(0, math.floor(limit / 10) * 10)
            emit('clear', before, before - ap, target_ap=math.floor(limit), reason=reason,
                 task=settings['spend_task'])

    def gain(action, amount, direct=False, **metadata):
        nonlocal ap, executable
        if direct:
            clear_to(HARD_CAP - amount, 'direct_purchase_headroom')
        before = ap
        ap += amount
        overflow = max(0, math.floor(ap - HARD_CAP))
        if overflow:
            # The host has no verified mailbox inventory API. A saturated AP
            # reading proves no overflow amount, so this is a manual estimate.
            executable = False
            warnings.append('OVERFLOW_REQUIRES_MAIL_VERIFICATION')
            metadata['verification'] = 'manual_mail'
        ap = min(HARD_CAP, ap)
        add_mail(overflow, clock + MAIL_LIFETIME)
        emit(action, before, amount, overflow_ap=overflow, **metadata)

    while events:
        at, _, action, amount, metadata = heapq.heappop(events)
        if at > target:
            break
        advance(at)
        if action == 'mail':
            urgent = any(p['expires_at'] - MARGIN <= clock for p in mail)
            if not urgent:
                continue
            total = sum(p['amount'] for p in mail)
            if total > HARD_CAP:
                executable = False
                warnings.append('MAIL_REQUIRES_SELECTIVE_COLLECTION')
                emit('manual_mail', ap, total)
                # Never pretend an oversized mailbox was collected safely.
                break
            clear_to(HARD_CAP - total, 'expiring_mail_headroom')
            before = ap
            ap += total
            mail.clear()
            emit('mail', before, total)
        elif action == 'club':
            # Club sign-in delivers an AP mail, not character AP. The live CN
            # dialog and mailbox confirm delivery, but show no exact expiry.
            # Keep unverified deliveries out of the known inventory entirely:
            # neither a guessed lifetime nor a saturated AP counter proves it.
            executable = False
            pending_mail_ap += amount
            warnings.append('CLUB_REWARD_REQUIRES_MAIL_VERIFICATION')
            emit(action, ap, amount, expected_mail_ap=amount,
                 delivery='mail', verification='manual_mail')
        elif action == 'manual_purchase':
            premium_cost += metadata['pyroxene']
            gain(action, amount, direct=True, **metadata)
            executable = False
        elif action == 'regenerate':
            clear_to(max(0, settings['soft_cap'] - 120), 'natural_recovery')
        elif action == 'cafe':
            amount = math.floor(cafe)
            cafe -= amount
            if amount:
                gain(action, amount)
        elif action == 'ready':
            available_at_target = {'character_ap': math.floor(ap), 'mail_ap': sum(p['amount'] for p in mail),
                                   'cafe_ap': math.floor(cafe),
                                   'total_ap': math.floor(ap) + math.floor(cafe) + sum(p['amount'] for p in mail)}
            emit('ready', ap)
            if settings['spend_at_target']:
                before = ap
                ap = 0
                emit('spend', before, before, task=settings['spend_task'])
                # Target-window AP includes the cafe and mailbox. Show each
                # withdrawal explicitly instead of stopping after character AP.
                if cafe >= 1:
                    clear_to(HARD_CAP - math.floor(cafe), 'cafe_headroom')
                    amount = math.floor(cafe)
                    cafe -= amount
                    gain('cafe', amount)
                    before = ap
                    ap = 0
                    emit('spend', before, before, task=settings['spend_task'])
                total = sum(p['amount'] for p in mail)
                if total > HARD_CAP:
                    executable = False
                    warnings.append('MAIL_REQUIRES_SELECTIVE_COLLECTION')
                    emit('manual_mail', ap, total)
                elif total:
                    clear_to(HARD_CAP - total, 'target_mail_headroom')
                    before = ap
                    ap += total
                    mail.clear()
                    emit('mail', before, total)
                    before = ap
                    ap = 0
                    emit('spend', before, before, task=settings['spend_task'])
        else:
            gain(action, amount, direct=action == 'free')
    if premium_cost:
        warnings.append('PREMIUM_PURCHASES_ARE_MANUAL')
    if discarded:
        warnings.append('CAFE_CAPACITY_LOSS')
    if mail:
        warnings.append('MAIL_IS_COLLECTED_AS_A_WHOLE')
    result = {'generated_at': now.isoformat(), 'target_at': target.isoformat(), 'executable': executable,
            'steps': steps, 'pending_mail_ap': pending_mail_ap, 'summary': {'character_ap': math.floor(ap),
            'mail_ap': sum(p['amount'] for p in mail), 'cafe_ap': math.floor(cafe),
            'total_ap': math.floor(ap) + math.floor(cafe) + sum(p['amount'] for p in mail),
            'manual_pyroxene_budget': premium_cost, 'cafe_capacity_loss': math.floor(discarded)},
            'warnings': list(dict.fromkeys(warnings))}
    # An interrupted timeline has no target forecast. Omit that optional field
    # instead of presenting the current snapshot as target-time availability.
    if available_at_target is not None:
        result['available_at_target'] = available_at_target
    return result
