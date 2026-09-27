"""Device presence alerts.

Every PRESENCE_SECONDS (default 60) live sync runs a discovery scan of the router
(neighbors, bridge hosts, DHCP, ARP, hotspot, Wi-Fi). Comparing who was online
before and after tells TapTap which devices dropped off or came back.

A device that disappears is only reported if it is *still* gone after the rule's
``min_offline_minutes`` (default 3) — a phone waking up or an ARP entry ageing
does not wake you up at night. "Back online" is only sent for devices you were
told went offline.

Rules decide what you hear about: mute rules always win, then the first alert
rule that matches. With no rules at all, TapTap watches network equipment
(switches, access points, routers) and stays quiet about customer phones.
"""
import ipaddress
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import AlertRule, DeviceAlert, RouterDevice, RouterNeighbor, SyncedIPBinding

logger = logging.getLogger('taptap.presence')


def presence_interval():
    return max(30, int(getattr(settings, 'PRESENCE_SECONDS', 60)))


def ensure_default_rules(business):
    if not business.alert_rules.exists():
        AlertRule.objects.create(business=business, name='Switches, access points and routers', subject='network', match='all', action='alert',
                                 min_offline_minutes=3, notify_recovery=True)
    return business.alert_rules.all()


def ip_type(dev, bypassed_macs=()):
    """Static / dynamic / hotspot / bypassed for a client device."""
    src = (dev.sources or '') if hasattr(dev, 'sources') else ''
    mac = (getattr(dev, 'mac_address', '') or '').upper()
    types = set()
    raw = getattr(dev, 'raw_data', None) or {}
    if 'hotspot-active' in src:
        types.add('hotspot')
    if mac and mac in bypassed_macs:
        types.add('bypassed')
    if 'dhcp' in src:
        dyn = str(raw.get('dynamic', 'true')).lower() in {'true', 'yes'}
        types.add('dynamic' if dyn else 'static')
    elif getattr(dev, 'ip_address', ''):
        types.add('static')
    ip = getattr(dev, 'ip_address', '') or getattr(dev, 'address', '')
    try:
        a = ipaddress.ip_address(str(ip).split('/')[0])
        types.add('private' if a.is_private else 'public')
    except ValueError:
        pass
    return types


def describe(obj, subject, bypassed_macs=()):
    if subject == 'network':
        return {'key': f'nb:{obj.neighbor_key}', 'name': obj.identity or obj.board or obj.address or obj.mac_address or 'Network device',
                'ip': obj.address, 'mac': (obj.mac_address or '').upper(), 'kind': obj.device_kind or 'network', 'types': ip_type(obj),
                'port': obj.interface_name, 'subject': 'network'}
    kind = 'wifi_client' if obj.connection_type == 'wifi' else 'wired'
    return {'key': f'dev:{obj.device_key}', 'name': obj.hostname or obj.ip_address or obj.mac_address or 'Device', 'ip': obj.ip_address,
            'mac': (obj.mac_address or '').upper(), 'kind': kind, 'types': ip_type(obj, bypassed_macs), 'port': obj.interface_name, 'subject': 'client'}


def _matches(rule, info):
    if rule.subject != 'any' and rule.subject != info['subject']:
        return False
    v = (rule.value or '').strip()
    if rule.match == 'all':
        return True
    if rule.match == 'ip':
        return bool(info['ip']) and info['ip'].split('/')[0] == v
    if rule.match == 'cidr':
        try:
            return bool(info['ip']) and ipaddress.ip_address(info['ip'].split('/')[0]) in ipaddress.ip_network(v, strict=False)
        except ValueError:
            return False
    if rule.match == 'mac':
        return info['mac'] == v.upper().replace('-', ':')
    if rule.match == 'name':
        return bool(v) and v.lower() in (info['name'] or '').lower()
    if rule.match == 'ip_type':
        return v in info['types']
    if rule.match == 'kind':
        return v == info['kind']
    return False


def rule_for(rules, info):
    """Mute rules win; otherwise the first matching alert rule (None = stay quiet)."""
    active = [r for r in rules if r.enabled]
    if any(r.action == 'mute' and _matches(r, info) for r in active):
        return None
    return next((r for r in active if r.action == 'alert' and _matches(r, info)), None)


def alert_state(rules, info):
    """For the UI: 'alerting' | 'muted' | 'quiet' and the deciding rule."""
    active = [r for r in rules if r.enabled]
    mute = next((r for r in active if r.action == 'mute' and _matches(r, info)), None)
    if mute:
        return 'muted', mute
    rule = next((r for r in active if r.action == 'alert' and _matches(r, info)), None)
    return ('alerting', rule) if rule else ('quiet', None)


def _online_map(router, bypassed):
    out = {}
    for n in RouterNeighbor.objects.filter(router=router, is_online=True):
        i = describe(n, 'network'); out[i['key']] = i
    for d in RouterDevice.objects.filter(router=router, is_online=True):
        i = describe(d, 'client', bypassed); out[i['key']] = i
    return out


def scan(router):
    """Discovery scan + presence comparison for one router. Returns counts."""
    from .live import push_event
    from .sync import refresh_router_topology
    business = router.business
    bypassed = set(SyncedIPBinding.objects.filter(router=router, binding_type='bypassed', disabled=False).values_list('mac_address', flat=True))
    before = _online_map(router, bypassed)
    refresh_router_topology(router, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10))
    after = _online_map(router, bypassed)
    return apply_changes(router, before, after)


def online_snapshot(router):
    """Who is online right now (used by TapTap Link syncs to compare before/after)."""
    bypassed = set(SyncedIPBinding.objects.filter(router=router, binding_type='bypassed', disabled=False).values_list('mac_address', flat=True))
    return _online_map(router, bypassed)


def apply_changes(router, before, after):
    """Turn a before/after comparison into confirmed offline / back-online alerts."""
    from .live import push_event
    business = router.business
    now = timezone.now()
    rules = list(ensure_default_rules(business))
    pkey, akey = f'tt:pres:pending:{router.pk}', f'tt:pres:alerted:{router.pk}'
    pending, alerted = cache.get(pkey) or {}, cache.get(akey) or {}
    out = {'offline': 0, 'online': 0}

    for k, info in before.items():
        if k not in after and k not in pending and k not in alerted:
            pending[k] = {'since': now.isoformat(), 'info': {**info, 'types': sorted(info['types'])}}
    for k in list(pending):
        if k in after:
            pending.pop(k)  # came back before it counted
            continue
        entry = pending[k]; info = {**entry['info'], 'types': set(entry['info']['types'])}
        rule = rule_for(rules, info)
        if rule is None:
            pending.pop(k); continue
        since = timezone.datetime.fromisoformat(entry['since'])
        if now - since >= timedelta(minutes=rule.min_offline_minutes):
            DeviceAlert.objects.create(business=business, router=router, rule=rule, subject=info['subject'], device_key=k, name=info['name'][:180],
                                       ip_address=info['ip'] or '', mac_address=info['mac'] or '', kind=info['kind'], event='offline', offline_since=since)
            alerted[k] = {'since': entry['since'], 'rule': rule.pk, 'info': entry['info']}
            pending.pop(k)
            push_event(business.pk, f'{info["name"]} ({info["ip"] or info["mac"]}) went offline on {router.name}', 'bad')
            from .notify import notify
            notify(business, 'device_offline', f'{info["name"]} went offline', f'{info["name"]} ({info["ip"] or info["mac"]}) on {router.name} '
                   f'has not been seen for {rule.min_offline_minutes} minutes.', link='/alerts/', key=f'dev:{router.pk}:{k}:off')
            out['offline'] += 1
    for k in list(alerted):
        if k in after:
            entry = alerted.pop(k); info = entry['info']
            rule = AlertRule.objects.filter(pk=entry.get('rule')).first()
            if rule is None or rule.notify_recovery:
                since = timezone.datetime.fromisoformat(entry['since'])
                mins = int((now - since).total_seconds() // 60)
                DeviceAlert.objects.create(business=business, router=router, rule=rule, subject=info['subject'], device_key=k, name=info['name'][:180],
                                           ip_address=info['ip'] or '', mac_address=info['mac'] or '', kind=info['kind'], event='online', offline_since=since)
                push_event(business.pk, f'{info["name"]} is back online on {router.name} after {mins} min', 'good')
                from .notify import notify
                notify(business, 'device_online', f'{info["name"]} is back online', f'{info["name"]} on {router.name} returned after {mins} minutes.',
                       link='/alerts/', key=f'dev:{router.pk}:{k}:on')
                out['online'] += 1
    cache.set(pkey, pending, 86400 * 3)
    cache.set(akey, alerted, 86400 * 30)
    return out
