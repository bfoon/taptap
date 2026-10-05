"""Customer-facing email for member renewal receipts and expiry reminders."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models_member_plans import (
    MemberNotificationSettings,
    MemberReminderLog,
    MemberRenewal,
)

logger = logging.getLogger('taptap.member_notifications')


def _site():
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/')


def _send(to, subject, text_template, html_template, context):
    """Send one customer email through TapTap's configured Django mail backend."""
    context = dict(context)
    context.setdefault('site', _site())
    text = render_to_string(text_template, context)
    html = render_to_string(html_template, context)
    msg = EmailMultiAlternatives(
        subject=subject[:180],
        body=text,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[to],
    )
    msg.attach_alternative(html, 'text/html')
    return msg.send(fail_silently=False)


def send_renewal_receipt(renewal: MemberRenewal):
    """Email the printable renewal receipt. Returns (sent_bool, message)."""
    if not renewal.email_to:
        return False, 'No member email address is set.'

    business = renewal.business
    subject = (
        f'[{business.business_name}] Renewal receipt '
        f'{renewal.receipt_number}'
    )
    ctx = {
        'business': business,
        'renewal': renewal,
        'member': renewal.member,
        'support_phone': getattr(business, 'support_phone', ''),
    }
    try:
        _send(
            renewal.email_to,
            subject,
            'core/email/member_renewal_receipt.txt',
            'core/email/member_renewal_receipt.html',
            ctx,
        )
        renewal.email_sent_at = timezone.now()
        renewal.email_error = ''
        renewal.save(update_fields=['email_sent_at', 'email_error'])
        return True, f'Receipt emailed to {renewal.email_to}.'
    except Exception as exc:
        error = str(exc)[:500]
        MemberRenewal.objects.filter(pk=renewal.pk).update(
            email_error=error,
        )
        renewal.email_error = error
        logger.warning(
            'member renewal receipt %s to %s failed: %s',
            renewal.pk,
            renewal.email_to,
            exc,
        )
        return False, f'Receipt email failed: {error}'


def _threshold_for(pref, member, now):
    """Return the reminder threshold currently due, or None.

    Each reminder has a one-day delivery window:
      7-day: >6 and <=7 days left
      2-day: >1 and <=2 days left
      1-day: >0 and <=1 day left
    This avoids sending all older thresholds when reminders are switched on late.
    """
    if not member.expires_at or member.expires_at <= now:
        return None

    left = member.expires_at - now
    plan_minutes = int(getattr(member, 'duration_minutes', 0) or 0)
    choices = (
        (7, pref.remind_7_days, timedelta(days=6)),
        (2, pref.remind_2_days, timedelta(days=1)),
        (1, pref.remind_1_day, timedelta(0)),
    )
    for days, enabled, lower in choices:
        if not enabled:
            continue
        # A 3-day plan should not receive a "7 days left" reminder immediately.
        if plan_minutes and plan_minutes < days * 1440:
            continue
        upper = timedelta(days=days)
        if lower < left <= upper:
            return days
    return None


def _send_reminder(pref, days, now):
    member = pref.member
    business = member.business
    log, _ = MemberReminderLog.objects.get_or_create(
        member=member,
        expiry_at=member.expires_at,
        days_before=days,
        defaults={
            'email_to': pref.email,
            'status': 'pending',
        },
    )

    if log.status == 'sent':
        return False
    if log.attempts >= 3:
        return False
    if (
        log.last_attempt_at
        and now - log.last_attempt_at < timedelta(minutes=30)
    ):
        return False

    log.email_to = pref.email
    log.attempts += 1
    log.last_attempt_at = now
    log.status = 'pending'
    log.error = ''
    log.save(
        update_fields=[
            'email_to',
            'attempts',
            'last_attempt_at',
            'status',
            'error',
        ]
    )

    word = 'day' if days == 1 else 'days'
    subject = (
        f'[{business.business_name}] Your internet account expires '
        f'in {days} {word}'
    )
    ctx = {
        'business': business,
        'member': member,
        'days': days,
        'word': word,
        'expiry': timezone.localtime(member.expires_at),
        'support_phone': getattr(business, 'support_phone', ''),
    }

    try:
        _send(
            pref.email,
            subject,
            'core/email/member_expiry_reminder.txt',
            'core/email/member_expiry_reminder.html',
            ctx,
        )
        log.status = 'sent'
        log.sent_at = timezone.now()
        log.error = ''
        log.save(update_fields=['status', 'sent_at', 'error'])
        return True
    except Exception as exc:
        log.status = 'failed'
        log.error = str(exc)[:500]
        log.save(update_fields=['status', 'error'])
        logger.warning(
            'member expiry reminder %s (%sd) to %s failed: %s',
            member.pk,
            days,
            pref.email,
            exc,
        )
        return False


def send_due_member_reminders(now=None):
    """Send opt-in customer reminders. Safe to call every minute."""
    now = now or timezone.now()
    sent = 0
    prefs = (
        MemberNotificationSettings.objects
        .filter(reminders_enabled=True)
        .exclude(email='')
        .select_related('member__business')
    )

    for pref in prefs.iterator():
        member = pref.member
        if member.login_type != 'member' or member.deleted_at:
            continue
        # A frozen account's clock is paused; wait until it is running again.
        if member.frozen_at:
            continue
        if member.status == 'expired':
            continue
        days = _threshold_for(pref, member, now)
        if days and _send_reminder(pref, days, now):
            sent += 1
    return sent
