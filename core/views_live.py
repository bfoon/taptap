"""Live sync heartbeat, IP bindings (clear on/off state), session enforcement, missing sales."""
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .finance import effective_price, record_sale
from .live import beat_alive, fix_incident, interval, push_event, recent_events, watch_business
from .mikrotik import MikroTikService, ros_bool
from .models import IPBindingAccessExpiry, SessionIncident, SyncedIPBinding, Voucher, VoucherSale
from .utils import log


def _on_link(router):
    """True when this router must be handled through TapTap Link right now
    (enrolled in Link and its TapTap Tunnel is not healthy)."""
    from .linkops import uses_link
    return uses_link(router)


MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$')
TYPES = ('bypassed', 'regular', 'blocked')


def _b(request):
    return request.user.business


def _status(business):
    routers = list(business.routers.all().order_by('name'))
    last = max((r.last_watch_at for r in routers if r.last_watch_at), default=None)
    return {
        'ok': True, 'live': business.live_sync, 'mode': 'scheduler' if beat_alive() else 'browser', 'interval': interval(),
        'last_watch_at': last.isoformat() if last else None, 'now': timezone.now().isoformat(),
        'routers': [{'id': r.id, 'name': r.name, 'status': r.status, 'last_watch_at': r.last_watch_at.isoformat() if r.last_watch_at else None} for r in routers],
        'incidents': business.session_incidents.filter(status='open').count(),
        'events': recent_events(business.pk)[:15],
    }


@login_required
def live_tick(request):
    """Called by every open TapTap page. Runs a live pass itself only when no scheduler is running."""
    business = _b(request)
    ran = None
    if business.live_sync and not beat_alive() and cache.add(f'tt:watch:biz:{business.pk}', 1, interval()):
        ran = watch_business(business)
    # No scheduler running: this page does the strict expiry sweep itself (live sync on or off).
    if not beat_alive() and cache.add(f'tt:expiry:biz:{business.pk}', 1, interval()):
        try:
            from .expiry import sweep
            sweep(business=business, watched=[r['router_id'] for r in (ran or []) if isinstance(r, dict) and r.get('router_id') and not r.get('error')])
        except Exception:
            pass
    data = _status(business)
    if ran is not None:
        data['ran'] = ran
    from .views_traffic import unread_alerts
    try:
        since = int(request.GET.get('since') or 0)
    except ValueError:
        since = 0
    data['alerts'] = unread_alerts(business, since)
    # Business alerts: checked here too when no scheduler runs (at most once a minute per business).
    from . import business_alerts as ba
    if not beat_alive():
        try:
            ba.evaluate(business)
        except Exception:
            pass
    try:
        esince = int(request.GET.get('esince') or 0)
    except ValueError:
        esince = 0
    data['biz_alerts'] = ba.unread(business, esince, request.user)
    return JsonResponse(data)


@login_required
@require_POST
def live_now(request):
    business = _b(request)
    results = watch_business(business, force=True)
    try:
        from .expiry import sweep
        sweep(business=business, recheck=True, watched=[r['router_id'] for r in results if isinstance(r, dict) and r.get('router_id') and not r.get('error')])
    except Exception:
        pass
    data = _status(business)
    data['ran'] = results
    changed = sum((r.get('new_vouchers', 0) + r.get('activated', 0) + r.get('users_changed', 0) + r.get('bindings_changed', 0)) for r in results)
    errors = [f"{r.get('router')}: {r['error']}" for r in results if r.get('error')]
    data['message'] = ('; '.join(errors) if errors else (f'{changed} change(s) picked up.' if changed else 'Everything is already up to date.'))
    return JsonResponse(data)


@login_required
@require_POST
def live_settings(request):
    business = _b(request)
    business.live_sync = request.POST.get('live_sync') == 'on'
    business.auto_enforce = request.POST.get('auto_enforce') == 'on'
    try:
        business.enforce_grace_minutes = max(0, min(240, int(request.POST.get('enforce_grace_minutes') or 5)))
    except ValueError:
        pass
    business.save(update_fields=['live_sync', 'auto_enforce', 'enforce_grace_minutes'])
    # Re-time open incidents to the new grace period.
    for inc in business.session_incidents.filter(status='open'):
        inc.fix_due_at = inc.first_seen + timedelta(minutes=business.enforce_grace_minutes)
        inc.save(update_fields=['fix_due_at'])
    messages.success(request, 'Live sync and enforcement settings saved.')
    return redirect(request.POST.get('next') or 'security')


# ─────────────────────────── IP bindings ───────────────────────────
def _binding_dict(b, expiries):
    exp = expiries.get((b.router_id, b.mikrotik_id))
    return {'id': b.pk, 'router_id': b.router_id, 'router': b.router.name, 'item_id': b.mikrotik_id, 'mac': b.mac_address, 'address': b.address,
            'type': b.binding_type or 'regular', 'comment': b.comment, 'server': b.server, 'active': not b.disabled, 'present': b.is_present,
            'source': b.source, 'sync': b.sync_status, 'error': b.sync_error, 'seen': b.last_seen_at.isoformat() if b.last_seen_at else None,
            'until': exp.expires_at.isoformat() if exp else None}


def _bindings_payload(business):
    qs = business.synced_ip_bindings.select_related('router').filter(is_present=True).order_by('router__name', 'comment', 'mac_address')
    expiries = {(e.router_id, e.binding_id): e for e in IPBindingAccessExpiry.objects.filter(business=business)}
    rows = [_binding_dict(b, expiries) for b in qs]
    from .bypass_pay import last_payments
    paid = last_payments(business, {r['mac'] for r in rows if r.get('mac')})
    from .fair_usage import bypass_status
    fup = bypass_status(business, [r['mac'].upper() for r in rows if r.get('mac') and r.get('type') == 'bypassed' and r.get('active')])
    for r in rows:
        r['paid'] = paid.get(r.get('mac'))
        r['fup'] = fup.get((r.get('mac') or '').upper())
    return {'rows': rows, 'counts': {'total': len(rows), 'active': sum(1 for r in rows if r['active']), 'disabled': sum(1 for r in rows if not r['active']),
                                     'bypassed': sum(1 for r in rows if r['type'] == 'bypassed'), 'blocked': sum(1 for r in rows if r['type'] == 'blocked')}}


@login_required
def ip_bindings(request):
    business = _b(request)
    if request.method == 'POST':  # add a binding
        r = get_object_or_404(business.routers, pk=request.POST.get('router_id'))
        mac = request.POST.get('mac_address', '').strip().upper().replace('-', ':')
        if mac and not MAC_RE.match(mac):
            messages.error(request, 'That MAC address is not valid. Use the form AA:BB:CC:DD:EE:FF.')
            return redirect('ip_bindings')
        kind = request.POST.get('type', 'bypassed') if request.POST.get('type') in TYPES else 'bypassed'
        from . import bypass_pay
        pay = None
        if kind == 'bypassed' and request.POST.get('start_disabled') != 'on':
            try:   # free internet for a device = a sale: the payment comes first
                pay = bypass_pay.parse(request.POST)
            except bypass_pay.PaymentNeeded as e:
                messages.error(request, f'Not added: {e}')
                return redirect('ip_bindings')
        binding = SyncedIPBinding.objects.create(business=business, router=r, mac_address=mac, address=request.POST.get('address', '').strip(),
                                                 server=request.POST.get('server', 'all').strip() or 'all', binding_type=kind,
                                                 comment=request.POST.get('comment', '').strip() or 'TapTap', disabled=request.POST.get('start_disabled') == 'on',
                                                 source='taptap', sync_status='Pending')
        if _on_link(r):
            from .linkops import send, QUEUED
            try:
                if not mac:
                    raise ValueError('A MAC address is needed for bindings sent through TapTap Link.')
                send(r, 'binding_upsert', {'mac': mac, 'type': kind, 'address': binding.address, 'server': binding.server,
                                           'comment': binding.comment, 'disabled': binding.disabled}, label=f'Add binding {binding.comment or mac}', user=request.user)
                binding.sync_status = 'Queued'; binding.save(update_fields=['sync_status', 'updated_at'])
                hours = request.POST.get('hours')
                if pay:
                    bypass_pay.book(business, binding, pay, request.user, hours=int(hours) if hours and hours.isdigit() else None)
                if hours and hours.isdigit():
                    IPBindingAccessExpiry.objects.update_or_create(business=business, router=r, binding_id=f'mac:{mac}',
                                                                   defaults={'mac_address': mac, 'expires_at': timezone.now() + timedelta(hours=int(hours))})
                messages.success(request, f'Binding for {binding.comment or mac}: {QUEUED}')
            except ValueError as e:
                binding.sync_status = 'Error'; binding.sync_error = str(e); binding.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
                messages.warning(request, f'Saved in TapTap but not sent: {e}')
            return redirect('ip_bindings')
        try:
            with MikroTikService(r) as svc:
                _, item_id = svc.upsert_binding(binding)
            binding.mikrotik_id = str(item_id or ''); binding.sync_status = 'Synced'; binding.sync_error = ''
            binding.save(update_fields=['mikrotik_id', 'sync_status', 'sync_error', 'updated_at'])
            hours = request.POST.get('hours')
            if hours and hours.isdigit() and binding.mikrotik_id:
                IPBindingAccessExpiry.objects.update_or_create(business=business, router=r, binding_id=binding.mikrotik_id,
                                                               defaults={'mac_address': mac, 'expires_at': timezone.now() + timedelta(hours=int(hours))})
            if pay:
                bypass_pay.book(business, binding, pay, request.user, hours=int(hours) if hours and hours.isdigit() else None)
            messages.success(request, f'Binding for {binding.comment or mac} added on {r.name}.' + (f' Payment of {business.currency}{pay["amount"]} booked in Finance.' if pay and not pay['free'] else ''))
            push_event(business.pk, f'Binding {binding.comment or mac} added on {r.name}')
        except Exception as e:
            binding.sync_status = 'Error'; binding.sync_error = str(e); binding.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
            messages.warning(request, f'Saved in TapTap but the router did not accept it: {e}')
        return redirect('ip_bindings')
    payload = _bindings_payload(business)
    from .models import PAYMENT_METHODS
    return render(request, 'core/ip_bindings.html', {'payload': payload, 'routers': business.routers.all().order_by('name'),
                                                     'pay_methods': [m for m in PAYMENT_METHODS if m[0] != 'auto'],
                                                     'never_synced': not business.synced_ip_bindings.exists()})


@login_required
def ip_bindings_data(request):
    return JsonResponse(_bindings_payload(_b(request)))


def _apply(svc, b, action, value=None):
    """Apply one change on the router, read it back and update the mirror. Returns the new row."""
    if action in ('enable', 'disable'):
        svc.toggle_binding(b.mikrotik_id, action == 'disable')
    elif action == 'type':
        svc.set_binding(b.mikrotik_id, type=value)
    elif action == 'comment':
        svc.set_binding(b.mikrotik_id, comment=value)
    elif action == 'delete':
        svc.delete_binding(b.mikrotik_id)
        IPBindingAccessExpiry.objects.filter(router=b.router, binding_id=b.mikrotik_id).delete()
        b.delete()
        return None
    row = svc.get_binding(b.mikrotik_id)
    if row is None:
        b.is_present = False; b.save(update_fields=['is_present', 'updated_at'])
        raise ValueError('The router no longer has this binding.')
    b.disabled = ros_bool(row.get('disabled', False)); b.binding_type = str(row.get('type', b.binding_type)); b.comment = str(row.get('comment', ''))[:255]
    b.sync_status, b.sync_error, b.last_seen_at = 'Synced', '', timezone.now()
    b.save(update_fields=['disabled', 'binding_type', 'comment', 'sync_status', 'sync_error', 'last_seen_at', 'updated_at'])
    return b


def _binding_set_via_link(request, router, group, action, value, errors, ok_list=None):
    """IP binding changes for a TapTap Link router: queued commands + optimistic mirror update."""
    from .linkops import send
    n = 0
    for b in group:
        try:
            if not b.mac_address:
                raise ValueError('no MAC address')
            if action in ('enable', 'disable', 'timed'):
                send(router, 'binding_set', {'mac': b.mac_address, 'enabled': action != 'disable'}, label=f'{action.capitalize()} binding {b.comment or b.mac_address}', user=request.user)
                b.disabled = action == 'disable'
                if action == 'timed':
                    hours = max(1, min(24 * 90, int(value or 24)))
                    IPBindingAccessExpiry.objects.update_or_create(business=router.business, router=router, binding_id=b.mikrotik_id or f'mac:{b.mac_address}',
                                                                   defaults={'mac_address': b.mac_address, 'expires_at': timezone.now() + timedelta(hours=hours)})
                else:
                    IPBindingAccessExpiry.objects.filter(router=router, mac_address=b.mac_address).delete()
            elif action in ('type', 'comment'):
                if action == 'type': b.binding_type = value
                else: b.comment = value[:255]
                send(router, 'binding_upsert', {'mac': b.mac_address, 'type': b.binding_type, 'address': b.address, 'server': b.server or 'all',
                                                'comment': b.comment, 'disabled': b.disabled}, label=f'Update binding {b.comment or b.mac_address}', user=request.user)
            elif action == 'delete':
                send(router, 'binding_remove', {'mac': b.mac_address}, label=f'Delete binding {b.comment or b.mac_address}', user=request.user)
                IPBindingAccessExpiry.objects.filter(router=router, mac_address=b.mac_address).delete()
                b.delete(); n += 1
                continue
            b.sync_status = 'Queued'
            b.save(update_fields=['disabled', 'binding_type', 'comment', 'sync_status', 'updated_at'])
            n += 1
            if ok_list is not None:
                ok_list.append(b)
        except ValueError as exc:
            errors.append(f'{b.comment or b.mac_address}: {exc}')
    return n


@login_required
@require_POST
def ip_binding_set(request):
    """Set a binding to an explicit state (never a blind flip), confirm it on the router, return the result."""
    business = _b(request)
    ids = [int(x) for x in request.POST.getlist('ids') if str(x).isdigit()][:300]
    action = request.POST.get('action', '')
    value = request.POST.get('value', '').strip()
    if action not in ('enable', 'disable', 'type', 'comment', 'delete', 'timed', 'collect'):
        return JsonResponse({'ok': False, 'message': 'Unknown action.'}, status=400)
    if action == 'type' and value not in TYPES:
        return JsonResponse({'ok': False, 'message': 'Unknown type.'}, status=400)
    items = list(business.synced_ip_bindings.select_related('router').filter(pk__in=ids).exclude(mikrotik_id=''))
    # Turning on free internet for a device is a sale: no bypass goes on before its payment is entered.
    from . import bypass_pay
    if action == 'collect':
        items = list(business.synced_ip_bindings.select_related('router').filter(pk__in=ids))
    activating = [b for b in items if (action in ('enable', 'timed') and b.binding_type == 'bypassed' and b.disabled)
                  or (action == 'type' and value == 'bypassed' and b.binding_type != 'bypassed' and not b.disabled)]
    pay = None
    if activating or action == 'collect':
        try:
            pay = bypass_pay.parse(request.POST)
        except bypass_pay.PaymentNeeded as e:
            return JsonResponse({'ok': False, 'need_payment': True, 'ids': [b.pk for b in (activating or items)], 'message': str(e)}, status=400)
    hours = max(1, min(24 * 90, int(value or 24))) if action == 'timed' and str(value or '24').isdigit() else None
    if action == 'collect':
        for b in items:
            bypass_pay.book(business, b, pay, request.user, activated=not b.disabled)
        payload = _bindings_payload(business)
        n = len(items)
        return JsonResponse({'ok': True, 'done': n, 'message': (f'{business.currency}{pay["amount"]} × {n} booked in Finance.' if not pay['free']
                                                                 else f'Free access noted for {n} device(s).'), **payload})
    succeeded = []
    by_router = {}
    for b in items:
        by_router.setdefault(b.router, []).append(b)
    done, errors, rows = 0, [], []
    for router, group in by_router.items():
        if _on_link(router):
            done += _binding_set_via_link(request, router, group, action, value, errors, succeeded)
            continue
        try:
            with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)) as svc:
                for b in group:
                    try:
                        if action == 'timed':
                            hours = max(1, min(24 * 90, int(value or 24)))
                            _apply(svc, b, 'enable')
                            IPBindingAccessExpiry.objects.update_or_create(business=business, router=router, binding_id=b.mikrotik_id,
                                                                           defaults={'mac_address': b.mac_address, 'expires_at': timezone.now() + timedelta(hours=hours)})
                        else:
                            if action in ('enable', 'disable'):
                                IPBindingAccessExpiry.objects.filter(router=router, binding_id=b.mikrotik_id).delete()
                            _apply(svc, b, action, value)
                        done += 1
                        succeeded.append(b)
                    except Exception as exc:
                        errors.append(f'{b.comment or b.mac_address}: {exc}')
        except Exception as exc:
            errors.append(f'{router.name}: {exc}')
    booked = 0
    for b in activating:
        if b in succeeded:     # book only what really went on
            bypass_pay.book(business, b, pay, request.user, hours=hours)
            booked += 1
    if done:
        label = {'enable': 'enabled', 'disable': 'disabled', 'type': f'set to {value}', 'comment': 'renamed', 'delete': 'deleted', 'timed': f'enabled for {value} h'}[action]
        log(business, 'IP Binding', f'{done} binding(s) {label}')
        push_event(business.pk, f'{done} binding(s) {label}')
    payload = _bindings_payload(business)
    msg = '; '.join(errors)[:500] if errors else f'{done} binding(s) updated on the router.'
    if booked and pay and not pay['free']:
        msg += f' {business.currency}{pay["amount"]} × {booked} booked in Finance.'
    return JsonResponse({'ok': not errors, 'done': done, 'message': msg, **payload})


# ─────────────────────────── Enforcement ───────────────────────────
@login_required
@require_POST
def incident_fix(request, pk):
    inc = get_object_or_404(_b(request).session_incidents.select_related('router', 'business'), pk=pk)
    ok, msg = fix_incident(inc, user=request.user, by='user')
    if request.headers.get('x-requested-with') == 'fetch':
        return JsonResponse({'ok': ok, 'message': msg})
    (messages.success if ok else messages.error)(request, msg)
    return redirect('security')


@login_required
@require_POST
def incident_fix_all(request):
    business = _b(request)
    ok = fail = 0
    for inc in business.session_incidents.select_related('router', 'business').filter(status='open'):
        good, _ = fix_incident(inc, user=request.user, by='user')
        ok += good; fail += not good
    (messages.success if not fail else messages.warning)(request, f'{ok} session(s) fixed' + (f', {fail} failed — see details.' if fail else '.'))
    return redirect('security')


@login_required
@require_POST
def incident_ignore(request, pk):
    inc = get_object_or_404(_b(request).session_incidents, pk=pk)
    if inc.status == 'ignored':
        inc.status = 'open'; inc.fix_due_at = timezone.now() + timedelta(minutes=inc.business.enforce_grace_minutes)
    else:
        inc.status = 'ignored'
    inc.save(update_fields=['status', 'fix_due_at'])
    return redirect('security')


# ─────────────────────────── Missing sales ───────────────────────────
def missing_sales(business, since=None):
    """Vouchers that were used but have no sale — what Finance is missing."""
    qs = business.vouchers.filter(used_at__isnull=False, sale__isnull=True).exclude(login_type='member', price=0)   # free members are given away
    if since:
        qs = qs.filter(used_at__gte=since)
    priced, unpriced = [], 0
    plan_prices = {p.name: p.price for p in business.plans.all()}
    free = set(business.plans.filter(is_free=True).values_list('name', flat=True))
    for v in qs.only('id', 'price', 'plan_name', 'used_at', 'code', 'business_id'):
        if v.plan_name in free and not (v.price and v.price > 0):
            continue  # given away on purpose — nothing is missing
        price = v.price if v.price and v.price > 0 else plan_prices.get(v.plan_name) or Decimal('0')
        if price > 0:
            priced.append((v, price))
        else:
            unpriced += 1
    return priced, unpriced


@login_required
@require_POST
def finance_book_missing(request):
    business = _b(request)
    since = None
    try:
        if request.POST.get('since'):
            since = timezone.make_aware(timezone.datetime.fromisoformat(request.POST['since']))
    except ValueError:
        since = None
    priced, _ = missing_sales(business, since)
    n = 0
    for v, _price in priced:
        try:
            if record_sale(business, v, method='auto', notes='Booked from router activation history', when=v.used_at, user=request.user):
                n += 1
        except Exception:
            pass
    log(business, 'Sales Booked', f'{n} sale(s) booked from activations')
    messages.success(request, f'{n} sale(s) booked on the dates the vouchers were first used.')
    return redirect(request.POST.get('next') or 'finance')
