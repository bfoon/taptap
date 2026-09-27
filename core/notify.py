"""Email notifications for TapTap.

notify() is called wherever something worth knowing happens. It never sends mail
itself: it records a Notification, and deliver() (every minute, from the live
scheduler) sends instant ones, bundles digest ones hourly and writes a morning
summary. Owners choose per event: instant, in the hourly digest, or off.

* Quiet hours: non-critical instant alerts wait for the next digest.
* Duplicates are suppressed: the same event for the same thing within its cool-down
  (e.g. a router flapping) is sent once.
* Several instant alerts in one minute go out as one email, not a flood.
"""
import logging
import secrets
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db.models import Count, Sum
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Notification, NotificationSettings

logger = logging.getLogger('taptap.notify')

# key: (label, explanation, default mode, severity, group)
EVENTS = OrderedDict([
    ('router_offline', ('A router goes offline', 'TapTap cannot reach a router (direct or TapTap Link).', 'instant', 'critical', 'Routers')),
    ('router_online', ('A router is back online', 'Sent after a router that went offline reconnects.', 'instant', 'info', 'Routers')),
    ('device_offline', ('A watched device goes offline', 'Switches, access points and devices your alert rules watch.', 'instant', 'warning', 'Network')),
    ('device_online', ('A watched device is back', 'Sent when a device you were told about returns.', 'digest', 'info', 'Network')),
    ('traffic_guard', ('Traffic guard triggers', 'A port reached its speed limit and was slowed down or switched off.', 'instant', 'warning', 'Network')),
    ('session_enforced', ('Expired voucher disconnected', 'Automatic fixes of sessions whose voucher ran out or was disabled.', 'digest', 'info', 'Hotspot')),
    ('sync_failed', ('Router sync fails', 'A full synchronisation could not finish.', 'instant', 'warning', 'Routers')),
    ('backup_done', ('Backup saved', 'Manual and nightly router backups.', 'digest', 'info', 'Routers')),
    ('backup_failed', ('Backup failed', 'A router backup could not be saved.', 'instant', 'warning', 'Routers')),
    ('link_connected', ('Router connects with TapTap Link', 'A router finished TapTap Link setup.', 'instant', 'info', 'Security')),
    ('link_ip_change', ('TapTap Link from a new address', 'A Link router started calling in from a different public IP.', 'instant', 'warning', 'Security')),
    ('link_rejected', ('TapTap Link refused a connection', 'A revoked token or wrong IP tried to connect.', 'instant', 'critical', 'Security')),
    ('stock_low', ('Voucher stock running low', 'A plan has fewer than 20 unsold vouchers (checked each morning).', 'digest', 'warning', 'Business')),
    ('subscription', ('TapTap subscription ending', 'Reminder 3 days and 1 day before your subscription ends.', 'instant', 'warning', 'Business')),
])
COOLDOWN_MIN = {'router_offline': 15, 'router_online': 15, 'device_offline': 30, 'device_online': 30, 'link_ip_change': 360,
                'link_rejected': 60, 'traffic_guard': 30, 'stock_low': 1440, 'subscription': 1440}
SEVERITY_COLOR = {'critical': '#d64545', 'warning': '#f59e0b', 'info': '#1769e0', 'good': '#18a66a'}


def prefs(business):
    s, _ = NotificationSettings.objects.get_or_create(business=business, defaults={'unsubscribe_token': secrets.token_urlsafe(24)})
    if not s.unsubscribe_token:
        s.unsubscribe_token = secrets.token_urlsafe(24); s.save(update_fields=['unsubscribe_token'])
    return s


def mode_for(s, event):
    return (s.events or {}).get(event) or EVENTS.get(event, ('', '', 'instant'))[2]


def recipients(business, s=None):
    s = s or prefs(business)
    main = business.email or getattr(business.user, 'email', '')
    extra = [x.strip() for x in (s.extra_recipients or '').replace(';', ',').split(',') if '@' in x]
    return list(dict.fromkeys([e for e in [main, *extra] if e]))


def in_quiet_hours(s, when=None):
    if not s.quiet_start or not s.quiet_end:
        return False
    t = timezone.localtime(when or timezone.now()).time()
    a, b = s.quiet_start, s.quiet_end
    return (a <= t < b) if a < b else (t >= a or t < b)


def email_configured():
    return bool(getattr(settings, 'EMAIL_HOST', '')) or 'console' not in getattr(settings, 'EMAIL_BACKEND', '')


def notify(business, event, subject, body, severity=None, link='', key=None):
    """Record a notification (delivery happens in deliver())."""
    try:
        s = prefs(business)
        sev = severity or EVENTS.get(event, ('', '', '', 'info'))[3]
        mode = mode_for(s, event)
        if key:
            cool = timezone.now() - timedelta(minutes=COOLDOWN_MIN.get(event, 10))
            if Notification.objects.filter(business=business, dedupe_key=key, created_at__gte=cool).exists():
                return None
        if not s.enabled or mode == 'off':
            status = 'skipped'
        elif mode == 'digest' or (in_quiet_hours(s) and sev != 'critical'):
            status = 'digest'
        else:
            status = 'queued'
        return Notification.objects.create(business=business, event=event, severity=sev, subject=subject[:200], body=body,
                                           link=link[:300], dedupe_key=(key or '')[:200], status=status)
    except Exception as exc:  # notifications must never break the caller
        logger.warning('notify %s failed: %s', event, exc)
        return None


# ─────────────────────────── sending ───────────────────────────
def _site():
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/')


def _send(business, subject, items, heading, intro='', s=None, summary=None):
    s = s or prefs(business)
    to = recipients(business, s)
    if not to:
        raise ValueError('No email address: add a business email in Settings.')
    ctx = {'business': business, 'heading': heading, 'intro': intro, 'items': items, 'site': _site(), 'summary': summary,
           'colors': SEVERITY_COLOR, 'manage_url': f'{_site()}/notifications/', 'unsubscribe_url': f'{_site()}/n/off/{s.unsubscribe_token}/'}
    html = render_to_string('core/email/notification.html', ctx)
    text = render_to_string('core/email/notification.txt', ctx)
    msg = EmailMultiAlternatives(subject=f'[{business.business_name}] {subject}'[:180], body=text, from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                                 to=to, connection=get_connection(fail_silently=False))
    msg.attach_alternative(html, 'text/html')
    msg.extra_headers = {'List-Unsubscribe': f'<{ctx["unsubscribe_url"]}>'}
    msg.send()
    return ', '.join(to)


def _item(n):
    return {'subject': n.subject, 'body': n.body, 'severity': n.severity, 'when': timezone.localtime(n.created_at),
            'label': EVENTS.get(n.event, (n.event,))[0], 'link': (n.link if n.link.startswith('http') else _site() + n.link) if n.link else ''}


def deliver():
    """Send what is due. Called every minute by the scheduler; safe to call more often."""
    from .models import Business
    now = timezone.now()
    sent = 0
    for business in Business.objects.filter(notifications__status__in=['queued', 'digest']).distinct():
        s = prefs(business)
        queued = list(business.notifications.filter(status='queued').order_by('created_at')[:50])
        if queued:
            try:
                if len(queued) == 1:
                    n = queued[0]
                    to = _send(business, n.subject, [_item(n)], n.subject, s=s)
                else:
                    worst = 'critical' if any(n.severity == 'critical' for n in queued) else 'warning'
                    to = _send(business, f'{len(queued)} alerts', [_item(n) for n in queued], f'{len(queued)} things need your attention', s=s)
                business.notifications.filter(pk__in=[n.pk for n in queued]).update(status='sent', sent_at=now, recipients=to[:600], error='')
                sent += 1
            except Exception as exc:
                business.notifications.filter(pk__in=[n.pk for n in queued]).update(status='failed', error=str(exc)[:300])
                logger.warning('email to %s failed: %s', business, exc)
        due = not s.last_digest_at or now - s.last_digest_at >= timedelta(hours=1)
        if due and not in_quiet_hours(s):
            waiting = list(business.notifications.filter(status='digest').order_by('created_at')[:200])
            if waiting:
                try:
                    to = _send(business, f'Hourly digest — {len(waiting)} update{"s" if len(waiting) != 1 else ""}', [_item(n) for n in waiting],
                               'Your hourly digest', 'Things that happened on your network in the last hour.', s=s)
                    business.notifications.filter(pk__in=[n.pk for n in waiting]).update(status='sent', sent_at=now, recipients=to[:600], error='')
                    sent += 1
                except Exception as exc:
                    business.notifications.filter(pk__in=[n.pk for n in waiting]).update(status='failed', error=str(exc)[:300])
                s.last_digest_at = now; s.save(update_fields=['last_digest_at'])
    sent += daily_jobs()
    return sent


def daily_jobs():
    """Morning summary, low stock and subscription reminders — once a day per business."""
    from .models import Business
    now = timezone.localtime()
    n = 0
    for business in Business.objects.all():
        s = prefs(business)
        if s.last_summary_on == now.date() or now.hour < s.summary_hour:
            continue
        s.last_summary_on = now.date(); s.save(update_fields=['last_summary_on'])
        try:
            _stock_and_subscription(business)
            if s.enabled and s.daily_summary and recipients(business, s):
                _send(business, f'Your day yesterday — {(now - timedelta(days=1)):%a %d %b}', [], 'Good morning', s=s, summary=build_summary(business))
                n += 1
        except Exception as exc:
            logger.warning('daily summary %s: %s', business, exc)
    return n


def _stock_and_subscription(business):
    for plan in business.plans.filter(active=True):
        left = business.vouchers.filter(plan_name=plan.name, status='active', used_at__isnull=True, sold_at__isnull=True).count()
        if left < 20:
            notify(business, 'stock_low', f'Only {left} unsold {plan.name} voucher{"s" if left != 1 else ""} left',
                   f'Generate a new batch of {plan.name} vouchers before they run out.', link='/vouchers/generate/', key=f'stock:{plan.pk}')
    ends = getattr(business, 'access_expires_at', None)
    ends = ends() if callable(ends) else ends
    if ends:
        days = (ends - timezone.now()).days
        if days in (3, 1):
            notify(business, 'subscription', f'Your TapTap subscription ends in {days} day{"s" if days != 1 else ""}',
                   'Renew to keep live sync, alerts and your hotspot pages running.', link='/subscription/', key=f'sub:{days}:{ends:%Y%m%d}')


def build_summary(business):
    from .models import DeviceAlert, SessionIncident, UsageRecord, VoucherSale
    now = timezone.localtime()
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    sales = VoucherSale.objects.filter(business=business, sold_at__gte=start, sold_at__lt=end)
    total = sales.aggregate(v=Sum('amount'))['v'] or Decimal('0')
    prev = VoucherSale.objects.filter(business=business, sold_at__gte=start - timedelta(days=7), sold_at__lt=end - timedelta(days=7)).aggregate(v=Sum('amount'))['v'] or Decimal('0')
    top = sales.values('plan_name').annotate(c=Count('id'), v=Sum('amount')).order_by('-v').first()
    usage = UsageRecord.objects.filter(business=business, hour__gte=start, hour__lt=end)
    data = usage.aggregate(d=Sum('download'), u=Sum('upload'))
    by_hour = {}
    for r in usage.values('hour').annotate(t=Sum('download')):
        h = timezone.localtime(r['hour']).hour; by_hour[h] = by_hour.get(h, 0) + (r['t'] or 0)
    peak = max(by_hour, key=by_hour.get) if by_hour else None
    routers = list(business.routers.values_list('name', 'status'))
    return {
        'sales': total, 'sales_count': sales.count(), 'vs_last_week': (round(float((total - prev) * 100 / prev)) if prev else None),
        'currency': business.currency or 'D', 'top_plan': top,
        'data_gb': round(((data['d'] or 0) + (data['u'] or 0)) / 1024 ** 3, 1), 'users': usage.values('username').distinct().count(),
        'peak': f'{peak:02d}:00–{(peak + 1) % 24:02d}:00' if peak is not None else None,
        'routers_online': sum(1 for _, st in routers if st == 'Online'), 'routers': len(routers),
        'offline_routers': [n for n, st in routers if st != 'Online'],
        'device_alerts': DeviceAlert.objects.filter(business=business, event='offline', created_at__gte=start, created_at__lt=end).count(),
        'enforced': SessionIncident.objects.filter(business=business, status='fixed', fixed_at__gte=start, fixed_at__lt=end).count(),
    }


def send_test(business):
    s = prefs(business)
    return _send(business, 'Test email', [{'subject': 'Email notifications are working', 'severity': 'good', 'when': timezone.localtime(),
                                           'label': 'Test', 'body': 'You will receive TapTap alerts at this address. Change what you receive any time.', 'link': ''}],
                 'It works!', s=s)
