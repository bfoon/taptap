"""Traffic base-speed enforcement.

The owner can set a per-device base speed for:
    device > agent > plan > all plans

Traffic speed is the normal base ceiling. Fair Usage runs afterwards and may
reduce a heavy user further.

Direct API / TapTap Tunnel:
    TTSPD-* simple queues are reconciled against RouterOS every live pass.

TapTap Link:
    the existing safe `limit` / `unlimit` commands are reused.
"""
from __future__ import annotations

import ipaddress
import logging

from django.core.cache import cache
from django.utils import timezone

from .models_apps import TrafficSpeedRule

logger = logging.getLogger('taptap.traffic_speed')

PREFIX = 'TTSPD-'
STATE_TTL = 86400
LINK_REFRESH = 300


def _kbps(value):
    try:
        return max(32, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return 32


def _valid_ip(value):
    try:
        return ipaddress.ip_address(str(value or '')).version == 4
    except ValueError:
        return False


def _mac(value):
    return str(value or '').strip().upper().replace('-', ':')


def _qname(router_id, ip):
    return f'{PREFIX}{router_id}-{str(ip).replace(".", "-")}'[:80]


def _active_business_rules(business):
    return list(
        TrafficSpeedRule.objects
        .filter(business=business, enabled=True)
        .prefetch_related('plans', 'agents')
        .order_by('-updated_at', '-pk')
    )


def _rule_maps(rules):
    all_rules = []
    bypass_rules = []
    plan_rules = []
    agent_rules = []
    device_rules = []

    for rule in rules:
        if rule.scope == 'devices':
            wanted = []
            for d in rule.devices or []:
                wanted.append({
                    'router': int(d.get('router') or 0),
                    'mac': _mac(d.get('mac')),
                    'ip': str(d.get('ip') or '').strip(),
                })
            device_rules.append((rule, wanted))

        elif rule.scope == 'agents':
            agent_rules.append((
                rule,
                set(rule.agents.values_list('pk', flat=True)),
            ))

        elif rule.scope == 'plans':
            names = set()
            for p in rule.plans.all():
                names.add((p.name or '').strip().lower())
                if p.mikrotik_profile_name:
                    names.add(p.mikrotik_profile_name.strip().lower())
            plan_rules.append((rule, names))

        elif rule.scope == 'all_bypass':
            bypass_rules.append(rule)

        elif rule.scope == 'all_plans':
            all_rules.append(rule)

    return device_rules, agent_rules, plan_rules, bypass_rules, all_rules


def _pick_rule(router, session, voucher, maps):
    """First matching rule using device > agent > plan > all-plans."""
    device_rules, agent_rules, plan_rules, bypass_rules, all_rules = maps
    mac = _mac(session.get('mac-address') or session.get('mac'))
    ip = str(session.get('address') or session.get('ip') or '').strip()
    user = str(session.get('user') or '').strip()
    is_bypass = bool(session.get('bypass')) or user.upper().startswith('BYPASS:')

    for rule, devices in device_rules:
        for d in devices:
            if d['router'] and d['router'] != router.pk:
                continue
            if d['mac'] and mac and d['mac'] == mac:
                return rule
            if not d['mac'] and d['ip'] and ip and d['ip'] == ip:
                return rule

    if is_bypass:
        return bypass_rules[0] if bypass_rules else None

    if voucher is None:
        return None

    if voucher.agent_id:
        for rule, ids in agent_rules:
            if voucher.agent_id in ids:
                return rule

    plan = (voucher.plan_name or '').strip().lower()
    for rule, names in plan_rules:
        if plan and plan in names:
            return rule

    return all_rules[0] if all_rules else None


def desired_caps(router, active):
    """Return {queue_name: (ip, up_kbps, down_kbps, rule_id, label)}."""
    from .models import Voucher

    rows = []
    codes = set()

    for raw in active or []:
        user = str(raw.get('user') or '').strip()
        ip = str(raw.get('address') or raw.get('ip') or '').strip()
        mac = _mac(raw.get('mac-address') or raw.get('mac'))

        if not _valid_ip(ip):
            continue

        rows.append((raw, user, ip, mac))

        if user and not user.upper().startswith('BYPASS:'):
            codes.add(user.upper())

    vouchers = {}
    if codes:
        for v in (
            Voucher.objects
            .filter(business=router.business)
            .only('pk', 'code', 'plan_name', 'agent_id')
        ):
            if v.code.upper() in codes:
                vouchers[v.code.upper()] = v

    maps = _rule_maps(_active_business_rules(router.business))
    out = {}

    for raw, user, ip, mac in rows:
        voucher = vouchers.get(user.upper()) if user else None
        rule = _pick_rule(router, raw, voucher, maps)
        if not rule:
            continue

        qn = _qname(router.pk, ip)
        out[qn] = (
            ip,
            _kbps(rule.up_mbps),
            _kbps(rule.down_mbps),
            rule.pk,
            rule.name,
        )

    return out


def _reconcile_api(svc, router, want):
    """Make TTSPD queues on this router exactly equal to `want`."""
    queues = svc.resource('/queue/simple')
    rows = queues.get() or []

    have = {
        str(r.get('name', '')): r
        for r in rows
        if str(r.get('name', '')).startswith(f'{PREFIX}{router.pk}-')
    }

    changed = removed = 0
    first = next((r for r in rows if r.get('id')), None)

    for name, (ip, up, down, rule_id, label) in want.items():
        target = f'{ip}/32'
        limit = f'{up}k/{down}k'
        row = have.get(name)

        if row:
            if (
                str(row.get('target', '')) != target
                or str(row.get('max-limit', '')) != limit
                or str(row.get('disabled', 'false')).lower() == 'true'
            ):
                queues.set(
                    id=row['id'],
                    target=target,
                    max_limit=limit,
                    disabled='no',
                    comment=f'TapTap traffic speed #{rule_id}: {label}'[:255],
                )
                changed += 1
            continue

        fields = {
            'name': name,
            'target': target,
            'max_limit': limit,
            'comment': f'TapTap traffic speed #{rule_id}: {label}'[:255],
        }

        try:
            if first:
                queues.add(place_before=first['id'], **fields)
            else:
                queues.add(**fields)
        except Exception:
            queues.add(**fields)

        changed += 1

    for name, row in have.items():
        if name not in want:
            queues.remove(id=row['id'])
            removed += 1

    if want:
        try:
            rows = queues.get() or []
            ours = [
                r['id']
                for r in rows
                if str(r.get('name', '')).startswith(f'{PREFIX}{router.pk}-')
                and r.get('id')
            ]
            first = next((r for r in rows if r.get('id')), None)

            if ours and first and first['id'] not in ours:
                queues.call(
                    'move',
                    {
                        'numbers': ','.join(ours),
                        'destination': first['id'],
                    },
                )
        except Exception as exc:
            logger.info('traffic speed queue ordering %s: %s', router, exc)

    return {
        'set': changed,
        'removed': removed,
        'active': len(want),
    }


def _reconcile_link(router, want, force=False):
    """Use TapTap Link's existing safe limit/unlimit commands."""
    from .linkops import send

    key = f'tt:spd:link:{router.pk}'
    had = cache.get(key) or {}
    now = timezone.now().timestamp()

    changed = {}
    for name, (ip, up, down, rule_id, label) in want.items():
        state = [ip, up, down]
        if force or had.get(name, {}).get('state') != state:
            changed[name] = (ip, up, down, rule_id, label)

    removed = [name for name in had if name not in want]

    for name, (ip, up, down, rule_id, label) in changed.items():
        try:
            send(
                router,
                'limit',
                {
                    'name': ip,
                    'up': up / 1000,
                    'down': down / 1000,
                    'queue': name,
                },
                label=f'Speed {label}: {down / 1000:g}↓/{up / 1000:g}↑ Mb/s',
                minutes=10,
            )
        except Exception as exc:
            logger.info('traffic speed Link set %s %s: %s', router, name, exc)

    for name in removed:
        try:
            send(
                router,
                'unlimit',
                {
                    'name': had[name].get('state', [''])[0],
                    'queue': name,
                },
                label='Remove Traffic speed limit',
                minutes=10,
            )
        except Exception as exc:
            logger.info('traffic speed Link remove %s %s: %s', router, name, exc)

    cache.set(
        key,
        {
            name: {
                'state': [ip, up, down],
                'at': now,
            }
            for name, (ip, up, down, _rule_id, _label) in want.items()
        },
        STATE_TTL,
    )

    return {
        'set': len(changed),
        'removed': len(removed),
        'active': len(want),
    }


def enforce(router, active, now=None, svc=None, force=False):
    """Enforce Traffic speed rules for the sessions currently online."""
    want = desired_caps(router, active)

    if svc is not None:
        return _reconcile_api(svc, router, want)

    from .linkops import uses_link

    if uses_link(router):
        key = f'tt:spd:refresh:{router.pk}'
        periodic = force or cache.add(key, 1, LINK_REFRESH)
        return _reconcile_link(router, want, force=periodic)

    from .mikrotik import MikroTikService

    service = None
    try:
        service = MikroTikService(router).connect()
        return _reconcile_api(service, router, want)
    finally:
        if service is not None:
            service.close()


def cached_active(router):
    """Rebuild a session list from Traffic's live-session cache."""
    data = cache.get(f'tt:tr:users:{router.pk}') or {}
    rows = []

    for user, sessions in (data.get('users') or {}).items():
        for s in sessions or []:
            rows.append({
                'user': user,
                'address': s.get('ip', ''),
                'mac-address': s.get('mac', ''),
            })

    return rows


def apply_business(business, force=True):
    """Apply current rules now; live sync keeps them correct later."""
    from .linkops import uses_link
    from .mikrotik import MikroTikService

    results = []

    for router in business.routers.all():
        svc = None

        try:
            if uses_link(router):
                active = cached_active(router)
                result = enforce(router, active, force=force)
            else:
                svc = MikroTikService(router).connect()
                active = svc.active_users()
                result = enforce(router, active, svc=svc, force=force)

            results.append((router, True, result))

        except Exception as exc:
            results.append((router, False, str(exc)))

        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:
                    pass

    return results


def install():
    """Run Traffic speed before the existing Fair Usage enforcement.

    Fair Usage therefore stays the stricter final layer.
    """
    from . import fair_usage

    if getattr(fair_usage, '_taptap_traffic_speed_wrapped', False):
        return

    original = fair_usage.enforce

    def wrapped(router, active, now=None, svc=None):
        base = None

        try:
            base = enforce(
                router,
                active,
                now=now,
                svc=svc,
            )
        except Exception as exc:
            logger.info('traffic speed enforcement %s: %s', router, exc)

        result = original(
            router,
            active,
            now=now,
            svc=svc,
        )

        if isinstance(result, dict):
            result['traffic_speed'] = base or {}

        return result

    fair_usage.enforce = wrapped
    fair_usage._taptap_traffic_speed_wrapped = True
