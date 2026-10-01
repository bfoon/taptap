"""Traffic report, device alerts and topology device lists."""
import csv
import ipaddress
import re
from datetime import timedelta

from django.contrib import messages
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .finance import PRESETS, resolve_period
from .models import AlertRule, AppUsage, DeviceAlert, DeviceSignature, RouterDevice, RouterNeighbor, SyncedIPBinding, UsageRecord
from .presence import alert_state, describe, ensure_default_rules
from .traffic_report import human_bytes, report, right_now

MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$')


def _b(request):
    return request.user.business


# ─────────────────────────── traffic ───────────────────────────
def _traffic_args(request):
    business = _b(request)
    period = resolve_period(request.GET, '7d')
    router = request.GET.get('router') or None
    if router and not business.routers.filter(pk=router).exists():
        router = None
    return business, period, router


def _payload(business, period, router):
    """The whole report as JSON-safe data. Cached briefly: it only changes once per live-sync pass."""
    key = f'tt:trd:{business.pk}:{period.preset}:{period.start:%Y%m%d%H}:{period.end:%Y%m%d%H}:{router or 0}'
    try:
        hit = cache.get(key)
        if hit:
            return hit
    except Exception:
        pass
    data = report(business, period, router)
    data['insights'] = [list(x) for x in data['insights']]
    data.pop('category_order', None)
    data['generated_at'] = timezone.now().isoformat()
    data['period'] = {'preset': period.preset, 'bucket': period.bucket, 'days': period.days}
    try:
        cache.set(key, data, 15)
    except Exception:
        pass
    return data


@login_required
def traffic(request):
    business, period, router = _traffic_args(request)
    exp = request.GET.get('export')
    if exp:
        return _export(business, period, router, exp)
    data = _payload(business, period, router)
    return render(request, 'core/traffic.html', {'period': period, 'presets': PRESETS, 'data': data, 'routers': business.routers.all().order_by('name'),
                                                 'sel_router': router, 'now': right_now(business, router), 'query': request.GET.urlencode()})


@login_required
def traffic_data(request):
    business, period, router = _traffic_args(request)
    return JsonResponse(_payload(business, period, router), encoder=DjangoJSONEncoder)


@login_required
def traffic_now(request):
    return JsonResponse(right_now(_b(request), request.GET.get('router')))


def _export(business, period, router, kind):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="taptap-{kind}-{period.start:%Y%m%d}-{period.end:%Y%m%d}.csv"'
    resp.write('\ufeff')
    w = csv.writer(resp)
    if kind == 'users':
        qs = UsageRecord.objects.filter(business=business, hour__gte=period.start, hour__lt=period.end)
        if router: qs = qs.filter(router_id=router)
        w.writerow(['User / voucher', 'MAC', 'Download (MB)', 'Upload (MB)', 'Total (MB)'])
        for r in qs.values('username', 'mac_address').annotate(d=Sum('download'), u=Sum('upload')).order_by('-d'):
            w.writerow([r['username'], r['mac_address'], round((r['d'] or 0) / 1048576, 1), round((r['u'] or 0) / 1048576, 1), round(((r['d'] or 0) + (r['u'] or 0)) / 1048576, 1)])
    elif kind == 'apps':
        qs = AppUsage.objects.filter(business=business, hour__gte=period.start, hour__lt=period.end)
        if router: qs = qs.filter(router_id=router)
        w.writerow(['App / service', 'Category', 'Site', 'Download (MB)', 'Upload (MB)'])
        for r in qs.values('app', 'category', 'domain').annotate(d=Sum('download'), u=Sum('upload')).order_by('-d'):
            w.writerow([r['app'], r['category'], r['domain'], round((r['d'] or 0) / 1048576, 1), round((r['u'] or 0) / 1048576, 1)])
    else:  # hourly
        data = report(business, period, router)
        w.writerow(['Period', 'Download (GB)', 'Upload (GB)', 'Average Mb/s down', 'Peak Mb/s down'])
        c = data['chart']
        for i, label in enumerate(c['labels']):
            w.writerow([label, c['down_gb'][i], c['up_gb'][i], c['mbps_dn'][i], c['peak_dn'][i]])
    return resp


# ─────────────────────────── topology: devices behind a node ───────────────────────────
@login_required
def node_devices(request):
    """Every device behind a topology node — online and offline (seen in the last 30 days)."""
    business = _b(request)
    node = request.GET.get('node', '')
    since = timezone.now() - timedelta(days=30)
    routers = {r.id: r for r in business.routers.all()}
    devices, gear, title = RouterDevice.objects.none(), RouterNeighbor.objects.none(), ''
    m = re.match(r'^(router|clients|nb|wan):(.+)$', node)
    if not m:
        return JsonResponse({'ok': False, 'message': 'Unknown node.'}, status=400)
    kind, rest = m.groups()
    if kind == 'router':
        r = routers.get(int(rest)) if rest.isdigit() else None
        if not r: return JsonResponse({'ok': False, 'message': 'Router not found.'}, status=404)
        devices = r.devices.all(); gear = r.neighbors.all(); title = r.name
    elif kind == 'clients':
        rid, _, port = rest.partition(':')
        r = routers.get(int(rid)) if rid.isdigit() else None
        if not r: return JsonResponse({'ok': False, 'message': 'Router not found.'}, status=404)
        devices = r.devices.filter(interface_name=port); title = f'{r.name} · {port}'
    elif kind == 'nb':
        # nb:<MAC>  or  nb:<router id>:<neighbor key>
        if MAC_RE.match(rest):
            n = RouterNeighbor.objects.filter(router__business=business, mac_address__iexact=rest).order_by('-is_online', '-last_seen_at').first()
        else:
            rid, _, key = rest.partition(':')
            n = RouterNeighbor.objects.filter(router__business=business, router_id=rid, neighbor_key=key).first() if rid.isdigit() else None
        if not n: return JsonResponse({'ok': False, 'message': 'Device not found.'}, status=404)
        label = n.identity or n.address or n.mac_address
        devices = RouterDevice.objects.filter(router=n.router).filter(Q(parent_identity__in=[x for x in (n.identity, n.address, n.mac_address) if x]) |
                                                                      Q(interface_name=n.interface_name))
        devices = devices.exclude(mac_address__iexact=n.mac_address) if n.mac_address else devices
        gear = RouterNeighbor.objects.filter(pk=n.pk); title = label
    else:
        return JsonResponse({'ok': True, 'title': 'Internet link', 'devices': [], 'counts': {'online': 0, 'offline': 0}})

    devices = devices.filter(Q(is_online=True) | Q(last_seen_at__gte=since)).select_related('router').order_by('-is_online', 'hostname', 'ip_address')[:600]
    gear = gear.filter(Q(is_online=True) | Q(last_seen_at__gte=since)).select_related('router')
    rules = list(ensure_default_rules(business))
    bypassed = set(SyncedIPBinding.objects.filter(business=business, binding_type='bypassed', disabled=False).values_list('mac_address', flat=True))
    macs = [d.mac_address.upper() for d in devices if d.mac_address]
    sigs = {s.last_mac: (s.label or ' '.join(x for x in (s.model, s.os) if x)) for s in
            DeviceSignature.objects.filter(business=business, last_mac__in=macs).only('last_mac', 'label', 'model', 'os')} if macs else {}
    out = []
    for n in gear:
        info = describe(n, 'network'); state, rule = alert_state(rules, info)
        out.append({'key': info['key'], 'name': info['name'], 'ip': n.address, 'mac': n.mac_address, 'kind': n.device_kind or 'network', 'network': True,
                    'online': n.is_online, 'last_seen': n.last_seen_at.isoformat(), 'port': n.interface_name, 'router': n.router.name,
                    'detail': ' · '.join(x for x in (n.board, n.version and f'v{n.version}') if x), 'types': sorted(info['types']),
                    'alert': state, 'rule': rule.name if rule else ''})
    for d in devices:
        info = describe(d, 'client', bypassed); state, rule = alert_state(rules, info)
        out.append({'key': info['key'], 'name': info['name'], 'ip': d.ip_address, 'mac': d.mac_address, 'kind': info['kind'], 'network': False,
                    'online': d.is_online, 'last_seen': d.last_seen_at.isoformat(), 'port': d.interface_name, 'router': d.router.name,
                    'detail': sigs.get((d.mac_address or '').upper(), '') or (f'via {d.parent_identity}' if d.parent_identity else ''),
                    'types': sorted(info['types']), 'alert': state, 'rule': rule.name if rule else ''})
    return JsonResponse({'ok': True, 'title': title, 'devices': out,
                         'counts': {'online': sum(1 for x in out if x['online']), 'offline': sum(1 for x in out if not x['online'])}})


@login_required
@require_POST
def device_alert_toggle(request):
    """Bell on a device: mute it, or ask to be alerted about it specifically."""
    business = _b(request)
    mac = request.POST.get('mac', '').strip().upper().replace('-', ':')
    ip = request.POST.get('ip', '').strip()
    name = request.POST.get('name', '').strip()[:80] or mac or ip
    subject = 'network' if request.POST.get('network') == '1' else 'client'
    want = request.POST.get('want')  # 'mute' | 'alert' | 'default'
    match, value = ('mac', mac) if MAC_RE.match(mac) else ('ip', ip)
    if not value:
        return JsonResponse({'ok': False, 'message': 'This device has no MAC or IP to identify it.'}, status=400)
    business.alert_rules.filter(match=match, value__iexact=value).delete()
    if want == 'mute':
        AlertRule.objects.create(business=business, name=f'Muted: {name}', subject='any', match=match, value=value, action='mute')
    elif want == 'alert':
        AlertRule.objects.create(business=business, name=f'Watch: {name}', subject='any', match=match, value=value, action='alert', min_offline_minutes=3)
    return JsonResponse({'ok': True, 'message': {'mute': f'You will not be alerted about {name}.', 'alert': f'You will be alerted when {name} goes offline.',
                                                  'default': f'{name} now follows your general alert rules.'}.get(want, 'Saved.')})


# ─────────────────────────── alerts page & rules ───────────────────────────
@login_required
def alerts(request):
    business = _b(request)
    rules = ensure_default_rules(business)
    qs = business.device_alerts.select_related('router', 'rule')
    f = request.GET.get('f', 'all')
    if f == 'offline': qs = qs.filter(event='offline')
    elif f == 'unread': qs = qs.filter(read_at__isnull=True)
    # Which devices are still offline right now (last event was "offline").
    latest = {}
    for a in business.device_alerts.order_by('created_at').only('device_key', 'event', 'router_id'):
        latest[(a.router_id, a.device_key)] = a.event
    still_down = sum(1 for v in latest.values() if v == 'offline')
    from .models_events import EventRule
    from .business_alerts import PRESETS
    ev_rules = list(business.event_rules.all())
    if request.GET.get('f') == 'business':
        qs = qs.none()
    return render(request, 'core/alerts.html', {'alerts': qs[:200], 'rules': rules, 'f': f, 'still_down': still_down,
                                                'ev_rules': ev_rules, 'ev_kinds': EventRule.KINDS,
                                                'ev_firing': sum(1 for r in ev_rules if r.firing and r.enabled), 'ev_on': sum(1 for r in ev_rules if r.enabled), 'ev_levels': EventRule.LEVELS,
                                                'ev_alerts': business.event_alerts.select_related('rule')[:100],
                                                'ev_unread': business.event_alerts.filter(read_at__isnull=True).count(),
                                                'ev_presets': [p for p in PRESETS if not any(r.kind == p[0] for r in ev_rules)],
                                                'plan_names': list(business.plans.filter(active=True).order_by('name').values_list('name', flat=True)),
                                                'router_list': business.routers.order_by('name'),
                                                'unread': business.device_alerts.filter(read_at__isnull=True).count(),
                                                'subjects': AlertRule.SUBJECTS, 'matches': AlertRule.MATCHES, 'ip_types': AlertRule.IP_TYPES,
                                                'kinds': AlertRule.KINDS, 'actions': AlertRule.ACTIONS})


@login_required
@require_POST
def alert_rule_save(request):
    business = _b(request)
    p = request.POST
    rule = get_object_or_404(business.alert_rules, pk=p['id']) if p.get('id') else AlertRule(business=business)
    match = p.get('match', 'all') if p.get('match') in dict(AlertRule.MATCHES) else 'all'
    value = p.get('value', '').strip()
    if match == 'cidr':
        try: value = str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            messages.error(request, 'That IP range is not valid. Use the form 192.168.88.0/24.'); return redirect('alerts')
    elif match == 'ip':
        try: value = str(ipaddress.ip_address(value))
        except ValueError:
            messages.error(request, 'That IP address is not valid.'); return redirect('alerts')
    elif match == 'mac':
        value = value.upper().replace('-', ':')
        if not MAC_RE.match(value):
            messages.error(request, 'That MAC address is not valid.'); return redirect('alerts')
    elif match == 'ip_type':
        value = p.get('ip_type', 'static')
    elif match == 'kind':
        value = p.get('kind', 'switch')
    elif match == 'all':
        value = ''
    rule.match, rule.value = match, value[:120]
    rule.subject = p.get('subject', 'network') if p.get('subject') in dict(AlertRule.SUBJECTS) else 'network'
    rule.action = 'mute' if p.get('action') == 'mute' else 'alert'
    try: rule.min_offline_minutes = max(0, min(240, int(p.get('min_offline_minutes') or 3)))
    except ValueError: rule.min_offline_minutes = 3
    rule.notify_recovery = p.get('notify_recovery') == 'on'
    rule.enabled = p.get('enabled', 'on') == 'on'
    rule.name = (p.get('name') or '').strip()[:120] or f'{rule.get_action_display()}: {rule.get_subject_display().split(" (")[0]}' + (f' {value}' if value else '')
    rule.save()
    messages.success(request, f'Rule “{rule.name}” saved.')
    return redirect('alerts')


@login_required
@require_POST
def alert_rule_action(request, pk):
    rule = get_object_or_404(_b(request).alert_rules, pk=pk)
    if request.POST.get('action') == 'delete':
        rule.delete(); messages.success(request, 'Rule deleted.')
    else:
        rule.enabled = not rule.enabled; rule.save(update_fields=['enabled'])
    return redirect('alerts')


@login_required
@require_POST
def alerts_read(request):
    business = _b(request)
    business.device_alerts.filter(read_at__isnull=True).update(read_at=timezone.now())
    business.event_alerts.filter(read_at__isnull=True).update(read_at=timezone.now())
    if request.headers.get('x-requested-with') == 'fetch':
        return JsonResponse({'ok': True})
    return redirect(request.POST.get('next') or 'alerts')


def unread_alerts(business, since_id=0):
    qs = business.device_alerts.filter(read_at__isnull=True).select_related('router')
    fresh = [{'id': a.id, 'event': a.event, 'name': a.name, 'ip': a.ip_address, 'mac': a.mac_address, 'router': a.router.name,
              'at': a.created_at.isoformat(), 'since': a.offline_since.isoformat() if a.offline_since else None}
             for a in qs.filter(id__gt=since_id).order_by('-id')[:8]]
    return {'unread': qs.count(), 'fresh': fresh, 'last_id': qs.order_by('-id').values_list('id', flat=True).first() or since_id}



# ─────────────────────────── business alerts (stock, sales, routers…) ───────────────────────────
@login_required
@require_POST
def event_rule_save(request):
    from .models_events import EventRule
    business = _b(request)
    p = request.POST
    rule = get_object_or_404(business.event_rules, pk=p['id']) if p.get('id') else EventRule(business=business)
    kind = p.get('kind') if p.get('kind') in dict(EventRule.KINDS) else 'stock_low'
    def num(k, d=None):
        v = (p.get(k) or '').strip()
        return v if v == '' else (v if v.replace('.', '', 1).isdigit() else d)
    params = {}
    for k in ('threshold', 'hours', 'minutes', 'amount', 'open_from', 'open_to'):
        v = num(k)
        if v not in (None, ''):
            params[k] = v
    if p.get('plan'):
        params['plan'] = p['plan'][:120]
    if p.get('router', '').isdigit() and business.routers.filter(pk=int(p['router'])).exists():
        params['router'] = int(p['router'])
    rule.kind, rule.params = kind, params
    rule.level = p.get('level') if p.get('level') in dict(EventRule.LEVELS) else 'warning'
    for f in ('bell', 'sound', 'desktop', 'email'):
        setattr(rule, f, p.get(f) == 'on')
    rule.enabled = p.get('enabled', 'on') == 'on'
    try: rule.repeat_hours = max(0, min(168, int(p.get('repeat_hours') or 0)))
    except ValueError: rule.repeat_hours = 0
    for f in ('quiet_from', 'quiet_to'):
        v = (p.get(f) or '').strip()
        setattr(rule, f, max(0, min(23, int(v))) if v.isdigit() else None)
    rule.name = (p.get('name') or '').strip()[:120] or dict(EventRule.KINDS)[kind]
    if rule.pk:
        rule.firing = False          # re-check from scratch with the new settings
    rule.save()
    from .business_alerts import evaluate
    evaluate(business, force=True)
    messages.success(request, f'Alert “{rule.name}” saved. It is checked every minute.')
    return redirect('/alerts/?f=business')


@login_required
@require_POST
def event_rule_action(request, pk=None):
    from .business_alerts import PRESETS, fire
    from .models_events import EventRule
    business = _b(request)
    act = request.POST.get('action')
    if act == 'preset':
        k = request.POST.get('kind')
        for kind, name, params, level, repeat in PRESETS:
            if kind == k and not business.event_rules.filter(kind=kind).exists():
                EventRule.objects.create(business=business, kind=kind, name=name, params=params, level=level, repeat_hours=repeat)
                messages.success(request, f'Alert “{name}” added. Edit it to change the numbers.')
        from .business_alerts import evaluate
        evaluate(business, force=True)
        return redirect('/alerts/?f=business')
    rule = get_object_or_404(business.event_rules, pk=pk)
    if act == 'delete':
        rule.delete(); messages.success(request, 'Alert deleted.')
    elif act == 'test':
        fire(rule, rule.name, 'This is how this alert looks and sounds.', '/alerts/?f=business', timezone.now(), test=True)
        messages.info(request, 'Test alert sent — watch the bell (and listen) within a few seconds.')
    else:
        rule.enabled = not rule.enabled; rule.firing = False; rule.save(update_fields=['enabled', 'firing'])
    return redirect('/alerts/?f=business')
