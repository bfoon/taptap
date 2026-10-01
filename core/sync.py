import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from django.db import transaction
from django.utils import timezone

from .mikrotik import MikroTikService, ros_bool, redact
from .models import (
    Voucher, VoucherPlan, RouterHotspotProfile, RouterHotspotUser,
    SyncedIPBinding, RouterInterface, RouterNeighbor, RouterDevice,
    RouterInterfaceRole, RouterConfigSnapshot,
)
from .durations import best_unit, parse_routeros as _routeros_minutes, router_limit
from .utils import log, voucher_profile
from .finance import mark_activated


def _has_uptime(value):
    text = str(value or '').strip().lower()
    return bool(text) and text not in {'0', '0s', '00:00:00', 'none'} and any(ch.isdigit() and ch != '0' for ch in text)


def _routeros_seconds(value):
    """'1w2d3h4m5s' / '12:30:00' / '1d 02:00:00' -> seconds (0 when unknown)."""
    text = str(value or '').strip().lower()
    total = 0
    for number, unit in re.findall(r'(\d+)(w|d|h|m|s)(?![a-z])', text):
        total += int(number) * {'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
    clock = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)
    if clock:
        h, m, sec = (int(x) for x in clock.groups())
        total += h * 3600 + m * 60 + sec
    return total


def first_use_estimate(uptime, now, created_at=None):
    """When a voucher was probably first used: now minus its uptime, never before it was created.

    With live sync running this is within seconds of the real login; for vouchers seen
    late it is still far closer than "the moment someone pressed Sync".
    """
    when = now - timedelta(seconds=_routeros_seconds(uptime))
    if created_at and when < created_at:
        when = created_at
    return min(when, now)


def _clean(row):
    return {str(k).lstrip('.'): v for k, v in dict(row or {}).items()}


def _safe_int(value):
    try: return int(value or 0)
    except (TypeError, ValueError): return 0


def _routeros_hours(value, default=24):
    """Best-effort RouterOS duration parser: 1w2d3h4m, 12:30:00, etc."""
    text = str(value or '').strip().lower()
    if not text or text in {'0', 'none', 'unlimited'}:
        return default
    total_seconds = 0
    for number, unit in re.findall(r'(\d+)(w|d|h|m|s)', text):
        n = int(number)
        total_seconds += n * {'w':604800,'d':86400,'h':3600,'m':60,'s':1}[unit]
    if total_seconds:
        return max(1, int((total_seconds + 3599) // 3600))
    match = re.search(r'(?:(\d+)d)?\s*(\d{1,2}):(\d{2}):(\d{2})', text)
    if match:
        days, hours, mins, secs = [int(x or 0) for x in match.groups()]
        total_seconds = days*86400 + hours*3600 + mins*60 + secs
        return max(1, int((total_seconds + 3599)//3600))
    return default


def _neighbor_kind(row):
    hay = ' '.join(str(row.get(k, '')) for k in ('identity', 'platform', 'board')).lower()
    if any(word in hay for word in ('cap', 'wap', 'hap', 'access point', 'wifi', 'wireless', 'unifi', 'omada')): return 'wifi'
    if any(word in hay for word in ('switch', 'sw-', 'crs')): return 'switch'
    if any(word in hay for word in ('router', 'rb', 'ccr', 'hex')): return 'router'
    return 'network'


def _normalize_mac(value):
    return str(value or '').strip().upper().replace('-', ':')


# ───────────── prices ─────────────
# RouterOS has no price field. Operators keep it in one of these places:
#  * Mikhmon on-login script:  :put (",rem,5000,1d,6000,,Disable,");  -> mode, price, validity, selling price, , lock
#  * a comment:                "price: 10", "Price=10", "D10", "GMD 10", "10 GMD", "10 dalasi"
MIKHMON_RE = re.compile(r'",\s*([a-z]*)\s*,\s*([\d.]+)\s*,\s*([0-9wdhms:]*)\s*,\s*([\d.]*)\s*,', re.I)
PRICE_WORD_RE = re.compile(r'(?:price|prix|amount|cost|tarif)\s*[:=]?\s*(?:[A-Za-z$]{1,4}\s*)?(\d[\d,]*(?:\.\d+)?)', re.I)
AMOUNT_UNIT_RE = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s?(?:gmd|dalasis?)\b', re.I)


def _num(text):
    try:
        value = Decimal(str(text).replace(',', ''))
        return value if value > 0 else None
    except (InvalidOperation, ValueError):
        return None


def parse_mikhmon(script):
    """Return (price, validity_minutes) from a Mikhmon-style on-login script, or (None, None)."""
    m = MIKHMON_RE.search(str(script or ''))
    if not m:
        return None, None
    price = _num(m.group(4)) or _num(m.group(2))
    validity = _routeros_minutes(m.group(3), 0) if m.group(3) else 0
    return price, (validity or None)


def price_from_text(text, currency='D'):
    """Find a price written in free text (comment or profile name). Never guesses from bare numbers."""
    text = str(text or '')
    if not text:
        return None
    m = PRICE_WORD_RE.search(text) or AMOUNT_UNIT_RE.search(text)
    if m:
        return _num(m.group(1))
    cur = re.escape(str(currency or 'D').strip())
    if cur:
        m = re.search(r'(?:(?<![A-Za-z])' + cur + r'|GMD)\s?(\d[\d,]*(?:\.\d+)?)(?![\d.]*\s*(?i:h|hr|hrs|hours?|d|days?|m|mins?|w|weeks?|mb|gb|mbps|kbps|k)\b)', text)
        if m:
            return _num(m.group(1))
    return None


def profile_price(row, currency='D'):
    """(price, validity_minutes, source) for a RouterOS HotSpot user profile."""
    price, validity = parse_mikhmon(row.get('on-login', row.get('on_login', '')))
    if price:
        return price, validity, 'Mikhmon on-login script'
    for key, label in (('comment', 'profile comment'), ('name', 'profile name')):
        found = price_from_text(row.get(key, ''), currency)
        if found:
            return found, validity, label
    return None, validity, ''


def _profile_to_plan(router, row, summary, now):
    name = str(row.get('name', '')).strip()
    if not name:
        return None
    shared = max(1, _safe_int(row.get('shared-users', row.get('shared_users', 1))) or 1)
    price, validity, price_source = profile_price(row, router.business.currency)
    rate = str(row.get('rate-limit', row.get('rate_limit', '')) or '')
    session = str(row.get('session-timeout', row.get('session_timeout', '')) or '')
    RouterHotspotProfile.objects.update_or_create(
        router=router, name=name,
        defaults={
            'business': router.business, 'mikrotik_id': str(row.get('id', '')), 'rate_limit': rate,
            'shared_users': shared, 'session_timeout': session,
            'idle_timeout': str(row.get('idle-timeout', row.get('idle_timeout', '')) or ''),
            'keepalive_timeout': str(row.get('keepalive-timeout', row.get('keepalive_timeout', '')) or ''),
            'address_pool': str(row.get('address-pool', row.get('address_pool', '')) or ''),
            'is_present': True, 'raw_data': row, 'last_seen_at': now,
        },
    )
    plan = VoucherPlan.objects.filter(business=router.business, name__iexact=name).first()
    if not plan and VoucherPlan.all_objects.binned().filter(business=router.business, name__iexact=name).exists():
        summary['deleted_plans_skipped'] = summary.get('deleted_plans_skipped', 0) + 1
        return None
    if plan:
        summary['duplicate_plans_skipped'] += 1
        if plan.source == 'mikrotik':
            plan.max_devices = shared
            plan.speed_limit = rate
            if plan.duration_unit != 'unlimited':
                minutes = _routeros_minutes(session, validity or plan.duration_minutes or 1440)
                if minutes != plan.duration_minutes:
                    plan.duration_minutes = minutes; plan.duration_unit = best_unit(minutes)
            plan.mikrotik_profile_name = name
            if not plan.imported_from_router_id: plan.imported_from_router = router
            fields = ['max_devices','speed_limit','duration_minutes','duration_unit','mikrotik_profile_name','imported_from_router']
            if price and plan.price != price and plan.price_source != 'manual' and not plan.is_free:
                plan.price = price; plan.price_source = 'router'; fields += ['price', 'price_source']
                summary['prices_found'] = summary.get('prices_found', 0) + 1
            plan.save(update_fields=fields)
            if not plan.price and not plan.is_free: summary.setdefault('plans_without_price', []).append(name)
        elif price and not plan.price and not plan.is_free:
            plan.price = price; plan.price_source = 'router'; plan.save(update_fields=['price', 'price_source']); summary['prices_found'] = summary.get('prices_found', 0) + 1
        return plan
    plan = VoucherPlan.objects.create(
        business=router.business, name=name, price=price or 0, price_source='router' if price else '',
        duration_minutes=_routeros_minutes(session, validity or 1440),
        duration_unit=best_unit(_routeros_minutes(session, validity or 1440)), max_devices=shared,
        speed_limit=rate, active=True, source='mikrotik', imported_from_router=router,
        mikrotik_profile_name=name,
    )
    summary['pulled_plans'] += 1
    if price: summary['prices_found'] = summary.get('prices_found', 0) + 1
    else: summary.setdefault('plans_without_price', []).append(name)
    return plan


def sync_router(router, progress=None):
    """Two-way RouterOS sync with de-duplication and full MikroTik inventory import."""
    def notify(percent, phase):
        if not progress:
            return
        try:
            progress(max(0, min(100, int(percent))), str(phase))
        except Exception:
            pass

    notify(2, 'Opening RouterOS connection')
    summary = {
        'pulled_plans': 0, 'duplicate_plans_skipped': 0,
        'pulled_vouchers': 0, 'duplicate_vouchers_skipped': 0, 'pulled_users': 0,
        'pulled_bindings': 0, 'pushed_vouchers': 0, 'updated_vouchers': 0,
        'pushed_bindings': 0, 'updated_bindings': 0, 'devices_discovered': 0,
        'config_sections': 0, 'errors': [],
        'unassigned_vouchers': router.business.vouchers.filter(router__isnull=True, source='taptap').count(),
    }
    svc = MikroTikService(router).connect()
    now = timezone.now()
    notify(8, 'Connected — reading HotSpot plans')
    try:
        RouterHotspotProfile.objects.filter(router=router).update(is_present=False)
        profile_map = {}
        for raw in svc.hotspot_profiles():
            row = _clean(raw)
            plan = _profile_to_plan(router, row, summary, now)
            if plan: profile_map[plan.name.lower()] = plan

        notify(22, 'Plans imported — reading vouchers and HotSpot users')

        RouterHotspotUser.objects.filter(router=router).update(is_present=False)
        from .voucher_bin import deleted_codes, remove_with_service
        from .voucher_codes import aliases as code_aliases, rename_with_service
        binned, binned_seen = deleted_codes(router.business), []
        renamed, renamed_seen = code_aliases(router.business), []
        router_rows = list(svc.hotspot_users())
        present = {str(_clean(r).get('name', '')).upper() for r in router_rows}
        for raw in router_rows:
            row = _clean(raw)
            username = str(row.get('name', '')).strip()
            if not username: continue
            if username.upper() in binned:
                binned_seen.append(username); continue
            if username.upper() in renamed:
                renamed_seen.append((username, renamed[username.upper()].code)); continue
            profile_name = str(row.get('profile', 'default') or 'default')
            plan = profile_map.get(profile_name.lower()) or router.business.plans.filter(name__iexact=profile_name).first()
            max_devices = plan.max_devices if plan else 1
            duration_minutes = _routeros_minutes(row.get('limit-uptime', row.get('limit_uptime', '')), plan.duration_minutes if plan else 1440)
            disabled = ros_bool(row.get('disabled', False))
            existing_voucher = Voucher.all_objects.filter(code__iexact=username).first()
            router_pw = row.get('password')
            router_pw = None if router_pw is None or str(router_pw).startswith('•') else str(router_pw)
            is_member_row = router_pw is not None and router_pw != '' and router_pw != username
            source = 'taptap' if existing_voucher and existing_voucher.business_id == router.business_id and existing_voucher.source == 'taptap' else 'mikrotik'
            RouterHotspotUser.objects.update_or_create(
                router=router, username=username,
                defaults={
                    'business':router.business, 'mikrotik_id':str(row.get('id','')), 'profile':profile_name,
                    'mac_address':_normalize_mac(row.get('mac-address', row.get('mac_address',''))),
                    'comment':str(row.get('comment','')), 'limit_uptime':str(row.get('limit-uptime', row.get('limit_uptime',''))),
                    'uptime':str(row.get('uptime','')), 'disabled':disabled, 'source':source,
                    'is_present':True, 'raw_data':row, 'last_seen_at':now,
                },
            )
            summary['pulled_users'] += 1

            user_price = price_from_text(row.get('comment', ''), router.business.currency)
            plan_price = plan.price if plan and plan.price else None
            if existing_voucher:
                if existing_voucher.business_id != router.business_id:
                    summary['errors'].append(f'Voucher name {username} already belongs to another TapTap business; import skipped.')
                    continue
                summary['duplicate_vouchers_skipped'] += 1
                fields = ['mikrotik_sync_status','mikrotik_sync_error']
                existing_voucher.mikrotik_sync_status='Synced'; existing_voucher.mikrotik_sync_error=''
                if existing_voucher.router_id is None:
                    existing_voucher.router=router; fields.append('router')
                if existing_voucher.source == 'mikrotik' and not existing_voucher.frozen_at:
                    existing_voucher.mikrotik_id=str(row.get('id','')); existing_voucher.plan_name=profile_name
                    existing_voucher.duration_minutes=duration_minutes; existing_voucher.max_devices=max_devices
                    _was=existing_voucher.status
                    # Archived and expired are both one-way states. Router sync must never reopen them.
                    if _was == 'archived':
                        existing_voucher.status = 'archived'
                    elif _was == 'expired':
                        existing_voucher.status = 'expired'
                    else:
                        existing_voucher.status = 'disabled' if disabled else 'active'
                    if _was!=existing_voucher.status:
                        from .voucher_history import record
                        record(existing_voucher,'router_disabled' if disabled else 'router_enabled',source='router',via='Full sync',
                               status_before=_was,status_after=existing_voucher.status,text=f'Changed on {router.name}')
                    fields += ['mikrotik_id','plan_name','duration_minutes','max_devices','status']
                    if router_pw is not None:
                        existing_voucher.login_type='member' if is_member_row else existing_voucher.login_type
                        existing_voucher.password=router_pw[:64] if is_member_row else ''
                        fields += ['login_type','password']
                new_price = user_price or plan_price
                if new_price and not existing_voucher.price and not existing_voucher.sold_at:
                    existing_voucher.price = new_price; fields.append('price'); summary['prices_repaired'] = summary.get('prices_repaired', 0) + 1
                existing_voucher.save(update_fields=list(dict.fromkeys(fields)))
                if _has_uptime(row.get('uptime')) and not existing_voucher.used_at:
                    try:
                        when = first_use_estimate(row.get('uptime'), now, existing_voucher.created_at if existing_voucher.source == 'taptap' else None)
                        if mark_activated(existing_voucher, when): summary['activated_vouchers'] = summary.get('activated_vouchers', 0) + 1
                    except Exception as exc:
                        summary['errors'].append(f'Activation tracking for {username}: {exc}')
            else:
                try:
                    new_voucher = Voucher.objects.create(
                        business=router.business, router=router, code=username, plan_name=profile_name,
                        price=user_price or plan_price or 0, duration_minutes=duration_minutes, max_devices=max_devices,
                        status='disabled' if disabled else 'active', source='mikrotik', mikrotik_id=str(row.get('id','')),
                        mikrotik_sync_status='Synced', mikrotik_sync_error='',
                        login_type='member' if is_member_row else 'voucher', password=router_pw[:64] if is_member_row else '',
                    )
                    summary['pulled_vouchers'] += 1
                    if _has_uptime(row.get('uptime')):
                        when = first_use_estimate(row.get('uptime'), now)
                        if router.sales_baseline_at:
                            if mark_activated(new_voucher, when): summary['activated_vouchers'] = summary.get('activated_vouchers', 0) + 1
                        else:
                            Voucher.objects.filter(pk=new_voucher.pk).update(used_at=when)
                except Exception as exc:
                    summary['errors'].append(f'Could not import RouterOS voucher {username}: {exc}')

        if renamed_seen:
            try:
                rename_with_service(svc, renamed_seen, present)
                summary['codes_renamed'] = len(renamed_seen)
            except Exception as exc:
                summary['errors'].append(f'Could not rename {len(renamed_seen)} changed voucher code(s): {exc}')
        try:
            from .voucher_codes import repair_passwords, stale_passwords
            fixed = repair_passwords(router, stale_passwords(router.business, router, [_clean(r) for r in router_rows]), svc=svc)
            if fixed:
                summary['code_logins_repaired'] = fixed
        except Exception as exc:
            summary['errors'].append(f'Could not repair the login of changed voucher codes: {exc}')
        if binned_seen:
            try:
                remove_with_service(svc, binned_seen)
                Voucher.all_objects.filter(business=router.business, code__in=binned_seen).update(router_removal='removed', router_removal_note='Removed during full sync')
                summary['deleted_removed'] = len(binned_seen)
            except Exception as exc:
                summary['errors'].append(f'Could not remove {len(binned_seen)} deleted voucher(s): {exc}')
        notify(40, 'Router vouchers imported — pushing TapTap vouchers')

        try:
            from .sticky import apply_api
            apply_api(svc, router.business)
        except Exception as exc:
            summary['errors'].append(f'Sticky sessions: {exc}')

        # Important: archived vouchers stay in TapTap, but are NEVER published back to MikroTik.
        for voucher in router.vouchers.filter(source='taptap').exclude(status='archived'):
            try:
                plan = router.business.plans.filter(name__iexact=voucher.plan_name).first()
                profile_name, shared, rate = voucher_profile(voucher, plan)
                svc.ensure_hotspot_profile(profile_name, shared, rate)
                action, item_id = svc.upsert_voucher(
                    voucher.code, profile_name, limit_uptime=router_limit(voucher),
                    comment=(f'TapTap member {voucher.code}' if voucher.is_member else f'TapTap voucher {voucher.code}'),
                    disabled=(voucher.status != 'active'), password=voucher.login_password,
                )
                voucher.mikrotik_id=str(item_id or voucher.mikrotik_id); voucher.mikrotik_sync_status='Synced'; voucher.mikrotik_sync_error=''
                voucher.save(update_fields=['mikrotik_id','mikrotik_sync_status','mikrotik_sync_error'])
                if action=='created': summary['pushed_vouchers'] += 1
                else: summary['updated_vouchers'] += 1
            except Exception as exc:
                voucher.mikrotik_sync_status='Error'; voucher.mikrotik_sync_error=str(exc)
                voucher.save(update_fields=['mikrotik_sync_status','mikrotik_sync_error'])
                summary['errors'].append(f'Voucher {voucher.code}: {exc}')

        notify(55, 'TapTap vouchers pushed — importing RouterOS IP bindings')

        SyncedIPBinding.objects.filter(router=router).update(is_present=False)
        for raw in svc.bindings():
            row=_clean(raw); item_id=str(row.get('id','')); mac=_normalize_mac(row.get('mac-address',row.get('mac_address',''))); address=str(row.get('address',''))
            existing = SyncedIPBinding.objects.filter(router=router,mikrotik_id=item_id).first() if item_id else None
            if not existing and mac: existing=SyncedIPBinding.objects.filter(router=router,mac_address__iexact=mac,address=address).first()
            defaults={
                'business':router.business,'mikrotik_id':item_id,'mac_address':mac,'address':address,
                'server':row.get('server',''),'binding_type':row.get('type','bypassed'),'comment':row.get('comment',''),
                'disabled':ros_bool(row.get('disabled',False)),'source':existing.source if existing else 'mikrotik',
                'sync_status':'Synced','sync_error':'','is_present':True,'raw_data':row,'last_seen_at':now,
            }
            if existing:
                for key,value in defaults.items(): setattr(existing,key,value)
                existing.save()
            else: SyncedIPBinding.objects.create(router=router,**defaults)
            summary['pulled_bindings'] += 1

        for binding in router.synced_ip_bindings.filter(source='taptap'):
            try:
                action,item_id=svc.upsert_binding(binding); binding.mikrotik_id=str(item_id or binding.mikrotik_id)
                binding.sync_status='Synced';binding.sync_error='';binding.is_present=True;binding.last_seen_at=now
                binding.save(update_fields=['mikrotik_id','sync_status','sync_error','is_present','last_seen_at','updated_at'])
                summary['pushed_bindings' if action=='created' else 'updated_bindings'] += 1
            except Exception as exc:
                binding.sync_status='Error';binding.sync_error=str(exc);binding.save(update_fields=['sync_status','sync_error','updated_at'])
                summary['errors'].append(f'IP binding {binding.mac_address or binding.address}: {exc}')

        notify(70, 'Bindings synchronized — scanning MACs, ports and neighbors')

        topo_data = svc.topology_data()
        topology = _persist_topology(router, topo_data, now)
        summary['devices_discovered'] = router.devices.filter(is_online=True).count()

        notify(86, 'Topology discovered — reading RouterOS configuration')
        try:
            cfg=svc.configuration_snapshot()
            RouterConfigSnapshot.objects.update_or_create(
                router=router,
                defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']},
            )
            summary['config_sections']=len(cfg['sections'])
        except Exception as exc:
            summary['errors'].append(f'Configuration snapshot: {exc}')

        notify(96, 'Finalizing database inventory')
        router.status='Online';router.last_error='';router.last_tested_at=now
        router.save(update_fields=['status','last_error','last_tested_at'])
        log(router.business,'Router Sync',f'{router.name}: {summary["pulled_plans"]} new plans, {summary["pulled_vouchers"]} new vouchers, {summary["devices_discovered"]} devices; de-duplicated existing records'
            + (f'; {summary["prices_found"]} plan prices read from the router' if summary.get('prices_found') else '')
            + (f'; {summary["prices_repaired"]} voucher prices repaired' if summary.get('prices_repaired') else '')
            + (f'; no price found for: {", ".join(summary["plans_without_price"][:6])} — set it on the Plans page' if summary.get('plans_without_price') else ''))
        if not router.sales_baseline_at:
            type(router).objects.filter(pk=router.pk, sales_baseline_at__isnull=True).update(sales_baseline_at=timezone.now())
        notify(100, 'Synchronization complete')
        return summary
    finally:
        svc.close()
