"""Customer reports → "Fix it": Fit picks the fix from the live diagnosis, carries it out, and closes the ticket.

The fix always goes through Fit's action engine (core/fit_actions.py), so it obeys exactly the same locks as a fix
approved in the Fit chat:
* the person's own role permission for that kind of change;
* the owner's switch for the group in Fit settings (Devices, Vouchers, Customer reports…);
* a fresh check that the change still makes sense right now;
and it is recorded as a Fit action (Fit history, voucher history with "approved in Fit", activity log).
Pressing "Fix it" is the approval. The ticket is only closed when the fix worked.

Which fix (first match wins):
  customer's phone is blocked by TapTap        → unblock it
  voucher paused by staff                      → unfreeze it
  voucher disabled, time left                  → enable it
  every device slot taken, one slot idle 20 min → free that idle slot (the other devices stay connected)
  every device slot taken, none idle           → reset the voucher's devices
  voucher works now                            → close the ticket ("checked — it works now")
  expired / deleted / unknown code             → no automatic fix (needs a person: sell or add time, check the code)
"""
from __future__ import annotations

import logging

from django.utils import timezone

from . import fit_actions as fa

logger = logging.getLogger('taptap.report_fix')
CLOSE = 'close_report'                     # not a router change: only resolves the ticket
IDLE_MINUTES = 20                          # same default Fit's free_idle_slot uses: a slot unseen this long can go


def _settings(business):
    from .models_fit import FitSettings
    return FitSettings.objects.get_or_create(business=business)[0]


def plan(report, now_diag=None, macs=None):
    """What "Fix it" would do. {'kind', 'args', 'label', 'why'} or {'kind': None, 'why'} when a person must decide.

    ``macs``: every MAC in the report's case (a customer may have sent from more than one phone)."""
    from .online_portal import diagnose
    d = now_diag or diagnose(report.business, report.code)
    if not d.get('found'):
        return {'kind': None, 'why': 'The code is not in TapTap — check it with the customer.'}
    code = d.get('code') or report.code
    wanted = [m for m in (fa.norm_mac(x) for x in (macs if macs is not None else [report.mac])) if m]
    if wanted:
        try:
            from .device_block import blocked_macs
            blocked = set(blocked_macs(report.business))
            hit = next((m for m in wanted if m in blocked), None)
            if hit:
                return {'kind': 'unblock_device', 'args': {'mac': hit}, 'label': f'Unblock {hit}',
                        'why': 'The customer’s phone is blocked by TapTap.'}
        except Exception:
            logger.exception('blocked check for report %s', report.pk)
    state = d.get('state')
    if state == 'paused':
        return {'kind': 'unfreeze_voucher', 'args': {'code': code, 'reason': f'Customer report #{report.pk}'},
                'label': 'Unpause the voucher', 'why': 'Staff paused it.'}
    if state == 'disabled':
        return {'kind': 'enable_voucher', 'args': {'code': code, 'reason': f'Customer report #{report.pk}'},
                'label': 'Enable the voucher', 'why': 'It is disabled.'}
    if state == 'slots_full':
        v = report.business.vouchers.filter(code__iexact=code).first()
        try:
            from .fit_monitors import idle_binding
            idle = idle_binding(v, IDLE_MINUTES) if v else None
        except Exception:
            idle = None
        if idle is not None:
            return {'kind': 'free_idle_slot', 'args': {'code': code, 'min_idle_minutes': IDLE_MINUTES, 'reason': f'Customer report #{report.pk}'},
                    'label': 'Free an idle device slot', 'why': f'All slots are taken; {idle.label or idle.current_mac or "one device"} is not online.'}
        return {'kind': 'reset_devices', 'args': {'code': code, 'reason': f'Customer report #{report.pk}'},
                'label': 'Reset the device slots', 'why': 'All slots are taken and every device is online.'}
    if state == 'ok':
        return {'kind': CLOSE, 'args': {}, 'label': 'Close — it works now', 'why': 'TapTap finds nothing wrong with it now.'}
    if state == 'expired':
        return {'kind': None, 'why': 'Its time ran out — sell a new voucher or add time on the voucher page.'}
    if state == 'deleted':
        return {'kind': None, 'why': 'The voucher is in the bin — restore it first if it was deleted by mistake.'}
    return {'kind': None, 'why': d.get('title') or 'Needs a person to look at it.'}


def availability(p, business, perms, settings=None):
    """(can_press, hint). Mirrors the checks fit_actions.run() makes, so a disabled button says why."""
    kind = p.get('kind')
    if not kind:
        return False, p.get('why', '')
    settings = settings or _settings(business)
    if kind == CLOSE:
        ok = fa.has_perm(perms, fa.KINDS['resolve_report'][1])
        return ok, '' if ok else 'Your role cannot resolve reports.'
    group, need, *_ = fa.KINDS[kind]
    if not fa.has_perm(perms, need):
        return False, 'Your role does not allow this change.'
    if not settings.allows(group):
        return False, f'Allow “{fa.GROUPS[group][0]}” in Fit settings to let Fit fix this.'
    if not settings.allows('reports'):
        return False, 'Allow “Customer reports” in Fit settings so Fit can close the ticket.'
    return True, ''


def _close(reports, user, note):
    """Close every report of the case with ONE timestamp, so the case can be reopened as a whole."""
    from .models_voucher_reports import VoucherReport
    when = timezone.now()
    VoucherReport.objects.filter(pk__in=[r.pk for r in reports], status='open').update(
        status='resolved', resolved_at=when, resolved_by=user, staff_note=note[:500])
    for r in reports:
        r.status, r.resolved_at, r.resolved_by, r.staff_note = 'resolved', when, user, note[:500]


def fix(reports, user, perms):
    """Fix the case once and close all its reports when it worked. ``reports``: the case, newest first
    (a single report is accepted too). Returns (ok, message)."""
    from .models_fit import FitAction
    if not isinstance(reports, (list, tuple)):
        reports = [reports]
    reports = [r for r in reports if r.status == 'open']
    if not reports:
        return False, 'This report is already resolved.'
    report = reports[0]
    business = report.business
    settings = _settings(business)
    p = plan(report, macs=[r.mac for r in reports])
    ok, hint = availability(p, business, perms, settings)
    if not ok:
        return False, hint or 'There is nothing Fit can fix automatically here.'
    sends = f' ({len(reports)} reports closed)' if len(reports) > 1 else ''
    if p['kind'] == CLOSE:
        _close(reports, user, 'Checked by Fit — the voucher works now.')
        return True, f'{report.code} works now — report closed{sends}.'
    try:
        params, title, detail, _link = fa._validate(business, user, p['kind'], p['args'])
    except fa.FitActionError as exc:
        return False, str(exc)
    ids = ', '.join(f'#{r.pk}' for r in reports[:6])
    action = FitAction.objects.create(business=business, proposed_to=user, kind=p['kind'], params=params,
                                      title=title[:200], detail=f'Customer report {ids}: {p["why"]}'[:500])
    done, result = fa.run(action, user, perms, settings)
    if not done:
        return False, f'Fit could not fix it: {result}'
    _close(reports, user, f'Fixed by Fit: {title}. {result}')
    return True, f'{title} — done. Report closed{sends}.'
