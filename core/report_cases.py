"""Customer reports → cases: every report for the same voucher is ONE case on the staff page.

Each submission stays its own VoucherReport row (nothing a customer wrote is lost), but staff work on the case:
one card, one diagnosis, one "Fix it", one "Resolved" — the action is applied to every report in the case.

Same voucher = the same voucher in TapTap (also when typed in another case or through a code alias), else the
same code with spaces removed, case-insensitive.

An OPEN case holds every open report of that voucher. A RESOLVED case holds the reports closed together (they
share the same resolved_at), so "Reopen" brings back exactly what was closed in one go.
"""
from __future__ import annotations

import re
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from .models_voucher_reports import VoucherReport

ESCALATE_AT = (1, 3)        # notify staff on the 1st and 3rd send of an open case…
ESCALATE_EVERY = 5          # …then on every 5th


def norm_code(code):
    return re.sub(r'\s', '', str(code or '')).upper()


def _q(business, code, voucher_id):
    q = Q(code__iexact=norm_code(code))
    if voucher_id:
        q |= Q(voucher_id=voucher_id)
    return VoucherReport.objects.filter(business=business).filter(q)


def open_siblings(business, code, voucher_id=None):
    """Open reports already in this voucher's case (newest first)."""
    return _q(business, code, voucher_id).filter(status='open').order_by('-created_at')


def case_reports(report):
    """Every report in the same case as ``report`` (newest first)."""
    vid = report.voucher_id
    if not vid:
        from .online_portal import diagnose
        vid = diagnose(report.business, report.code).get('voucher_id')
    qs = _q(report.business, report.code, vid)
    if report.status == 'open':
        return list(qs.filter(status='open').order_by('-created_at'))
    return list(qs.filter(status='resolved', resolved_at=report.resolved_at).order_by('-created_at'))


def should_notify(n):
    """Staff are pinged on the 1st and 3rd send, then every 5th — not on every repeat."""
    return n in ESCALATE_AT or (n > ESCALATE_AT[-1] and n % ESCALATE_EVERY == 0)


def ordinal(n):
    return f'{n}{"th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")}'


def _ago(dt, now):
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60} min ago'
    if secs < 86400:
        return f'{secs // 3600} h ago'
    return f'{secs // 86400} d ago'


class Case:
    """One voucher's reports, ready for the template."""

    def __init__(self, key, reports, now):
        self.key = key
        self.reports = sorted(reports, key=lambda r: r.created_at, reverse=True)
        self.latest, self.first = self.reports[0], self.reports[-1]
        self.count = len(self.reports)
        self.status = self.latest.status
        self.now = getattr(self.latest, 'now', None) or {}
        self.waiting = _ago(self.first.created_at, now)
        self.last_seen = _ago(self.latest.created_at, now)
        self.heat = 'hot' if self.count >= 4 else ('warm' if self.count >= 2 else 'calm')
        phones, macs = {}, {}
        for r in reversed(self.reports):                        # oldest first: the first name given wins
            if r.phone:
                phones.setdefault(r.phone, r.name)
            if getattr(r, 'mac_norm', '') or r.mac:
                macs.setdefault(getattr(r, 'mac_norm', '') or r.mac, getattr(r, 'device_id', None))
        self.phones = [{'phone': p, 'name': n} for p, n in phones.items()]
        self.macs = [{'mac': m, 'device_id': d} for m, d in macs.items()]
        allowed = 1
        try:
            if self.latest.voucher_id and self.latest.voucher:
                allowed = max(1, int(self.latest.voucher.max_devices or 1))
        except Exception:
            pass
        self.allowed_devices = allowed
        self.more_devices_than_allowed = len(self.macs) > allowed
        self.fix = getattr(self.latest, 'fix', None)
        self.resolved_by = self.latest.resolved_by
        self.resolved_at = self.latest.resolved_at
        self.staff_note = self.latest.staff_note
        self.when_sent = self.first.diagnosis or {}

    @property
    def id(self):
        return self.latest.pk

    @property
    def code(self):
        return self.latest.code


def build(reports, now=None):
    """Group reports (each already carrying .now from diagnose()) into cases, busiest-latest first."""
    now = now or timezone.now()
    groups = {}
    for r in reports:
        d = getattr(r, 'now', None) or {}
        ident = f'v{d["voucher_id"]}' if d.get('found') and d.get('voucher_id') else 'c' + norm_code(r.code)
        if r.status == 'open':
            key = (ident, 'open')
        else:
            stamp = r.resolved_at.replace(microsecond=0) if isinstance(r.resolved_at, datetime) else None
            key = (ident, f'resolved:{stamp.isoformat() if stamp else r.pk}')
        groups.setdefault(key, []).append(r)
    cases = [Case(k, rs, now) for k, rs in groups.items()]
    cases.sort(key=lambda c: (c.status != 'open', -c.latest.created_at.timestamp()))
    return cases
