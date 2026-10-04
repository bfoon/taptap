"""Track a plan, a batch or a voucher — and be told when something happens to it.

Anyone on the team can track any plan, batch or voucher they can open:
* **Everything**, or **only the events they choose** (new vouchers added, sold, first used, ended…);
* **In the app** (the bell, with sound and a desktop pop-up, only for them) or **in the app and by email**.

Busy items don't flood anyone: the same kind of event on the same tracked item within 10 minutes
updates one alert ("12 vouchers of Monthly were sold") instead of making twelve, and only the first
of them is emailed.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.tracking')

GROUP_MINUTES = 10

# event key: (label, icon, level, which kinds of tracked item offer it)
EVENTS = {
    'vouchers_added': ('New vouchers added', '➕', 'info', ('plan', 'batch')),
    'batch_issued': ('Given to an agent / returned', '🤝', 'info', ('plan', 'batch')),
    'sold': ('Sold', '💰', 'info', ('plan', 'batch', 'voucher')),
    'activated': ('First used (customer connected)', '📶', 'info', ('plan', 'batch', 'voucher')),
    'time_added': ('Time added', '⏱️', 'info', ('plan', 'batch', 'voucher')),
    'ended': ('Time ran out / expired', '⌛', 'warning', ('plan', 'batch', 'voucher')),
    'switched': ('Disabled or enabled', '⏻', 'warning', ('plan', 'batch', 'voucher')),
    'frozen': ('Frozen, warned or unfrozen', '❄️', 'warning', ('plan', 'batch', 'voucher')),
    'devices': ('Devices: reset, refused or disconnected', '📱', 'warning', ('plan', 'batch', 'voucher')),
    'fair_usage': ('Slowed down / back to full speed', '🐢', 'info', ('plan', 'batch', 'voucher')),
    'changed': ('Code, password or profile changed', '✏️', 'warning', ('plan', 'batch', 'voucher')),
    'deleted': ('Deleted or sale voided', '🗑️', 'danger', ('plan', 'batch', 'voucher')),
    'notes': ('Notes and other changes', '📝', 'info', ('plan', 'batch', 'voucher')),
}

# voucher history event → tracking event
FROM_HISTORY = {
    'extended': 'time_added', 'time_up': 'ended', 'disabled': 'switched', 'enabled': 'switched',
    'router_disabled': 'switched', 'router_enabled': 'switched', 'frozen': 'frozen', 'unfrozen': 'frozen',
    'warned': 'frozen', 'warning_accepted': 'frozen', 'mac_reset': 'devices', 'enforced': 'devices',
    'device': 'devices', 'shared_resolved': 'devices', 'fup_slowed': 'fair_usage', 'fup_restored': 'fair_usage',
    'fup_lifted': 'fair_usage', 'code_changed': 'changed', 'password_changed': 'changed', 'deleted': 'deleted',
    'sale_voided': 'deleted', 'archived': 'ended', 'note': 'notes',
}


def events_for(kind):
    return [(k, v[0], v[1]) for k, v in EVENTS.items() if kind in v[3]]


def watch_of(user, kind, object_id):
    from .models_watch import Watch
    if not getattr(user, 'is_authenticated', False):
        return None
    return Watch.objects.filter(user=user, kind=kind, object_id=object_id).first()


def _targets(business, voucher=None, batch=None, plan=None):
    """The tracked items an event touches: the voucher, its batch and its plan."""
    out = []
    if voucher is not None and voucher.pk:
        out.append(('voucher', voucher.pk))
        if voucher.batch_id:
            out.append(('batch', voucher.batch_id))
        if plan is None and voucher.plan_name:
            plan = business.plans.filter(name=voucher.plan_name).first()
    if batch is not None:
        out.append(('batch', batch.pk))
        if plan is None and batch.plan_id:
            plan = batch.plan
    if plan is not None:
        out.append(('plan', plan.pk))
    return list(dict.fromkeys(out))


def notify(business, event, text, *, voucher=None, batch=None, plan=None, link='', actor=None, count=1):
    """Tell everyone tracking the touched items (voucher / batch / plan) about an event."""
    from django.db.models import Q
    from .models_watch import Watch
    if event not in EVENTS:
        return 0
    targets = _targets(business, voucher, batch, plan)
    if not targets:
        return 0
    q = Q()
    for kind, oid in targets:
        q |= Q(kind=kind, object_id=oid)
    watches = list(Watch.objects.filter(business=business, active=True).filter(q).select_related('user'))
    sent = 0
    seen_users = set()
    # most specific first: someone tracking both the voucher and its plan gets one alert (from the voucher)
    order = {'voucher': 0, 'batch': 1, 'plan': 2}
    for w in sorted(watches, key=lambda x: order[x.kind]):
        if w.user_id in seen_users:
            continue
        if w.mode == 'some' and event not in (w.events or []):
            continue
        if actor is not None and getattr(actor, 'pk', None) == w.user_id and event not in ('vouchers_added',):
            continue        # you did it yourself — no need to tell you
        seen_users.add(w.user_id)
        _deliver(w, event, text, link or _link(voucher, batch, plan, w), count)
        sent += 1
    return sent


def _link(voucher, batch, plan, w):
    """Open the voucher concerned when there is one, else the tracked batch / plan."""
    if voucher is not None and voucher.pk:
        return f'/vouchers/{voucher.pk}/'
    return {'batch': f'/batches/{w.object_id}/', 'plan': f'/plans/{w.object_id}/'}.get(w.kind, f'/vouchers/{w.object_id}/')


def _deliver(w, event, text, link, count):
    from .models_events import EventAlert
    from .models_watch import Watch
    label, icon, level, _ = EVENTS[event]
    now = timezone.now()
    key = f'tt:watch:grp:{w.pk}:{event}'
    grp = cache.get(key)
    title_item = f'{w.get_kind_display()} {w.label}'
    if grp:
        # same kind of event on the same item a moment ago: update that alert instead of adding one
        n = grp['n'] + count
        EventAlert.objects.filter(pk=grp['id'], read_at__isnull=True).update(
            title=f'{icon} {title_item}: {label.lower()} ×{n}'[:160], body=f'Latest: {text}'[:400], created_at=now)
        if EventAlert.objects.filter(pk=grp['id'], read_at__isnull=True).exists():
            cache.set(key, {'id': grp['id'], 'n': n}, GROUP_MINUTES * 60)
            Watch.objects.filter(pk=w.pk).update(hits=w.hits + count, last_hit_at=now)
            return
    body = text + (f' · Why you track it: {w.note}' if w.note else '')
    a = EventAlert.objects.create(business=w.business, user=w.user, kind=f'track_{event}', level=level,
                                  title=f'{icon} {title_item}: {label.lower()}'[:160], body=body[:400], link=link, sound=True, desktop=True)
    cache.set(key, {'id': a.pk, 'n': count}, GROUP_MINUTES * 60)
    Watch.objects.filter(pk=w.pk).update(hits=w.hits + count, last_hit_at=now)
    if w.channel == 'app_email' and w.user.email:
        _email(w, a)


def _email(w, alert):
    from django.conf import settings
    from django.core.mail import send_mail
    site = str(getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    try:
        send_mail(f'[TapTap] {alert.title}', f'{alert.body}\n\nOpen it: {site}{alert.link}\n\n'
                  f'You get this because you track {w.get_kind_display().lower()} {w.label} in {w.business.business_name}. '
                  f'Change or stop it on the item\'s page (Track) or under Alerts › Tracking.',
                  getattr(settings, 'DEFAULT_FROM_EMAIL', None), [w.user.email], fail_silently=True)
    except Exception:
        logger.exception('tracking email for watch %s', w.pk)


def from_history(voucher, history_event, text='', actor=None):
    """Called by voucher_history.record for every entry in a voucher's history."""
    from .models import VoucherEvent
    event = FROM_HISTORY.get(history_event)
    if not event or voucher is None or not voucher.pk:
        return 0
    words = dict(VoucherEvent.EVENTS).get(history_event, history_event.replace('_', ' '))
    who = f' by {actor.get_full_name() or actor.username}' if getattr(actor, 'is_authenticated', False) else ''
    return notify(voucher.business, event, f'{voucher.code}: {words}{who}' + (f' — {text}' if text else ''), voucher=voucher, actor=actor)
