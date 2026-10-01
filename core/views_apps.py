"""Traffic › App & site control."""
import re
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import app_catalog, app_control as ac
from .models_apps import AppRule, AppControlState

DOMAIN_RE = re.compile(r'^(?=.{3,120}$)([a-z0-9-]+\.)+[a-z]{2,}$')


def _b(request):
    return request.user.business


def _time(v):
    try:
        return datetime.strptime((v or '').strip(), '%H:%M').time()
    except ValueError:
        return None


def _dt(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        return timezone.make_aware(datetime.strptime(v, '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


def _mbps(v, d):
    try:
        return max(Decimal('0.06'), min(Decimal('1000'), Decimal(str(v).strip())))
    except (InvalidOperation, ValueError):
        return Decimal(d)


@login_required
def app_control(request):
    business = _b(request)
    now = timezone.now()
    rules = list(business.app_rules.prefetch_related('routers'))
    for r in rules:
        r.when_text = ac.describe_when(r, business)
        r.service_names = app_catalog.names(r.services) + list(r.custom_domains or [])
        r.chips = [(app_catalog.SERVICES[k]['name'], app_catalog.SERVICES[k]['color']) for k in r.services if k in app_catalog.SERVICES] + [(d, '#64748b') for d in (r.custom_domains or [])]
        r.state = ('ended' if r.ends_at and r.ends_at <= now else 'scheduled' if r.starts_at and r.starts_at > now else
                   'on' if r.enabled else 'paused')
        r.router_names = [x.name for x in r.routers.all()]
    states = {s.router_id: s for s in AppControlState.objects.filter(router__business=business)}
    routers = list(business.routers.order_by('name'))
    for rt in routers:
        rt.app_state = states.get(rt.pk)
    return render(request, 'core/app_control.html', {
        'rules': rules, 'catalog': app_catalog.choices(), 'routers': routers, 'days': ac.DAYS,
        'actions': AppRule.ACTIONS, 'whens': AppRule.WHEN, 'scopes': AppRule.SCOPES,
        'peak_set': bool(business.peak_from and business.peak_to)})


@login_required
@require_POST
def app_rule_save(request):
    business = _b(request)
    p = request.POST
    rule = get_object_or_404(business.app_rules, pk=p['id']) if p.get('id') else AppRule(business=business, created_by=request.user)
    services = [k for k in p.getlist('services') if k in app_catalog.SERVICES]
    custom = []
    for raw in re.split(r'[\s,;]+', p.get('custom_domains', '').lower()):
        d = raw.strip().lstrip('*.').replace('https://', '').replace('http://', '').split('/')[0]
        if d and DOMAIN_RE.match(d):
            custom.append(d)
    if not services and not custom:
        messages.error(request, 'Choose at least one app or type a site (e.g. example.com).')
        return redirect('app_control')
    rule.services, rule.custom_domains = services, custom[:30]
    rule.action = p.get('action') if p.get('action') in dict(AppRule.ACTIONS) else 'block'
    rule.down_mbps, rule.up_mbps = _mbps(p.get('down_mbps'), '1'), _mbps(p.get('up_mbps'), '0.5')
    rule.when = p.get('when') if p.get('when') in dict(AppRule.WHEN) else 'always'
    rule.from_time, rule.to_time = _time(p.get('from_time')), _time(p.get('to_time'))
    if rule.when == 'window' and not (rule.from_time and rule.to_time):
        messages.error(request, 'Set the hours (from and to) for this rule.')
        return redirect('app_control')
    rule.days = [d for d in p.getlist('days') if d in ac.DAYS]
    if len(rule.days) == 7:
        rule.days = []
    # how long: permanent, for a while, or a set period
    now = timezone.now()
    span = p.get('span', 'permanent')
    if span == 'keep' and rule.pk:
        span = 'period'            # editing a temporary rule: keep its dates (shown in the form)
    rule.starts_at = rule.ends_at = None
    if span in ('30m', '1h', '3h', '1d', '1w'):
        rule.ends_at = now + {'30m': timedelta(minutes=30), '1h': timedelta(hours=1), '3h': timedelta(hours=3),
                              '1d': timedelta(days=1), '1w': timedelta(weeks=1)}[span]
    elif span == 'today':
        rule.ends_at = timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=1), time.min))
    elif span == 'period':
        rule.starts_at, rule.ends_at = _dt(p.get('starts_at')), _dt(p.get('ends_at'))
        if not rule.ends_at or (rule.starts_at and rule.starts_at >= rule.ends_at):
            messages.error(request, 'Choose when the period ends (after it starts).')
            return redirect('app_control')
    rule.scope = p.get('scope') if p.get('scope') in dict(AppRule.SCOPES) else 'customers'
    rule.block_quic = p.get('block_quic') == 'on'
    rule.enabled = p.get('enabled', 'on') == 'on'
    names = app_catalog.names(services) + custom
    verb = dict(AppRule.ACTIONS)[rule.action]
    rule.name = (p.get('name') or '').strip()[:120] or f'{verb} {", ".join(names[:3])}' + (f' +{len(names) - 3}' if len(names) > 3 else '')
    rule.save()
    rule.routers.set(business.routers.filter(pk__in=[x for x in p.getlist('routers') if str(x).isdigit()]))
    msgs = [m for _, ok, m in ac.push_all(business, user=request.user) if m not in ('unchanged', 'nothing to do')]
    messages.success(request, f'“{rule.name}” saved.' + (' ' + ' · '.join(msgs) if msgs else ''))
    return redirect('app_control')


@login_required
@require_POST
def app_rule_action(request, pk=None):
    business = _b(request)
    act = request.POST.get('action')
    if act == 'peak':
        business.peak_from, business.peak_to = _time(request.POST.get('peak_from')), _time(request.POST.get('peak_to'))
        business.peak_days = [d for d in request.POST.getlist('peak_days') if d in ac.DAYS]
        business.save(update_fields=['peak_from', 'peak_to', 'peak_days'])
        messages.success(request, 'Peak hours saved.' if business.peak_from and business.peak_to else 'Peak hours cleared.')
    elif act == 'apply':
        for r, ok, m in ac.push_all(business, force=True, user=request.user):
            (messages.info if ok else messages.warning)(request, f'{r.name}: {m}')
        return redirect('app_control')
    else:
        rule = get_object_or_404(business.app_rules, pk=pk)
        if act == 'delete':
            rule.delete(); messages.success(request, 'Rule deleted.')
        else:
            rule.enabled = not rule.enabled; rule.save(update_fields=['enabled'])
    ac.push_all(business, user=request.user)
    return redirect('app_control')
