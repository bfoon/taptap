"""Security › Internet sharing protection."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import sharing
from .models_sharing import SharingCase, SharingTrust


def _b(request):
    return request.user.business


def _back():
    from django.urls import reverse
    return redirect(reverse('security') + '#sharing')


@login_required
@require_POST
def sharing_save(request):
    business = _b(request)
    p = sharing.policy(business)
    f = request.POST
    was = p.enabled
    p.enabled = f.get('enabled') == 'on'
    p.action = f.get('action') if f.get('action') in dict(p.ACTIONS) else 'monitor'
    p.notify = f.get('notify') if f.get('notify') in dict(p.NOTIFY) else 'app'
    for field, lo, hi in (('threshold', 40, 100), ('suspect_at', 20, 95), ('block_minutes', 1, 1440)):
        v = f.get(field, '')
        if v.isdigit():
            setattr(p, field, max(lo, min(hi, int(v))))
    if p.suspect_at > p.threshold:
        p.suspect_at = p.threshold
    p.message = (f.get('message') or '').strip()[:600] or p.message
    p.single_device_only = f.get('single_device_only') == 'on'
    p.save()
    p.exempt_plans.set(business.plans.filter(pk__in=[x for x in f.getlist('exempt_plans') if x.isdigit()]))
    msg = f'Internet sharing protection {"on" if p.enabled else "off"} — {p.get_action_display().lower()}, act at {p.threshold}%.'
    if p.enabled != was or f.get('apply') == '1':
        res = sharing.apply_all(business, p.enabled, request.user)
        msg += ' ' + ' · '.join(m for _, m in res)
    from .utils import log
    log(business, 'Sharing Protection', msg)
    messages.success(request, msg)
    return _back()


@login_required
@require_POST
def sharing_case(request, pk):
    business = _b(request)
    case = get_object_or_404(SharingCase, pk=pk, business=business)
    act = request.POST.get('action')
    if act in ('block', 'warn'):
        sharing.act(case, user=request.user, force=act)
        messages.success(request, f'{case.code}: {"blocked" if act == "block" else "warned"}.')
    elif act == 'trust':
        SharingTrust.objects.get_or_create(business=business, mac=case.mac, defaults={'note': request.POST.get('note', '')[:200] or f'Trusted from {case.code}', 'created_by': request.user})
        SharingCase.objects.filter(business=business, mac=case.mac, status__in=('suspected', 'warned', 'blocked')).update(status='trusted', handled_by=request.user)
        from django.core.cache import cache
        cache.delete(f'tt:share:block:{business.pk}:{case.mac}')
        messages.success(request, f'{case.mac} is trusted — it will not be flagged again.')
    elif act in ('clear', 'unblock'):
        from django.core.cache import cache
        cache.delete(f'tt:share:block:{business.pk}:{case.mac}')
        case.status, case.blocked_until, case.handled_by = 'cleared', None, request.user
        case.action_taken = 'unblocked by staff' if act == 'unblock' else 'cleared by staff'
        case.save(update_fields=['status', 'blocked_until', 'handled_by', 'action_taken', 'last_seen'])
        messages.info(request, f'{case.code}: {case.action_taken}.')
    return _back()


@login_required
@require_POST
def sharing_trust_remove(request, pk):
    business = _b(request)
    SharingTrust.objects.filter(pk=pk, business=business).delete()
    messages.info(request, 'Device removed from the trusted list.')
    return _back()
