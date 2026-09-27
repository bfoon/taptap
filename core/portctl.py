"""Port control, traffic guard, reboot and configuration backups.

Safety first:
* Anything that must be undone later (timed shutdown, restart, guard hold, temporary
  limit) also gets a one-shot /system/scheduler job ON THE ROUTER, so the router
  restores itself even if TapTap can no longer reach it (for example when the port
  that was switched off is the one TapTap uses).
* Ports that carry TapTap's own connection or the Internet (WAN) are flagged; the
  guard may slow them down but never switch them off.
"""
import ipaddress
import logging
import re
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import PortRule, RouterBackup

logger = logging.getLogger('taptap.ports')
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
SCHED_PREFIX = 'taptap-'


def _int(v):
    try:
        return int(str(v or 0).strip() or 0)
    except ValueError:
        return 0


def _mbps(v):
    """8.5 → '8500k' (RouterOS rate string)."""
    kbps = max(0, int(round(float(v or 0) * 1000)))
    return f'{kbps}k' if kbps else '0'


# ─────────────────────────── risk ───────────────────────────
def detect_management_ports(svc, router):
    """Which physical port(s) TapTap's own API connection comes in on.

    /user/active shows the address of our API session; the ARP table turns it into a
    MAC and the bridge host table into the physical port. If TapTap connects from
    outside the router's networks, the path is a WAN link (already protected).
    """
    ports = set()
    try:
        me = [r for r in svc.safe_get('/user/active') if str(r.get('via', '')) == 'api' and str(r.get('name', '')) == router.username]
        addrs = {str(r.get('address', '')).split('%')[0] for r in me if r.get('address')}
        if not addrs:
            return None
        nets = []
        for r in svc.safe_get('/ip/address'):
            try:
                nets.append((ipaddress.ip_interface(str(r.get('address', ''))).network, str(r.get('interface', ''))))
            except ValueError:
                pass
        arp = {str(r.get('address', '')): str(r.get('mac-address', '')).upper() for r in svc.safe_get('/ip/arp')}
        hosts = {str(r.get('mac-address', '')).upper(): str(r.get('on-interface', '')) for r in svc.safe_get('/interface/bridge/host')}
        bridges = {str(r.get('name', '')) for r in svc.safe_get('/interface/bridge')}
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                continue
            local = next((iface for net, iface in nets if ip in net), None)
            if not local:
                continue  # comes in over the Internet → a WAN port, flagged separately
            if local in bridges:
                port = hosts.get(arp.get(a, ''), '')
                ports |= {port} if port else {local}
            else:
                ports.add(local)
    except Exception as exc:
        logger.info('management port detection %s: %s', router, exc)
        return None
    cache.set(f'tt:mgmt:{router.pk}', sorted(ports), 3600)
    return ports


def port_risk(router, iface, svc=None):
    """Why switching this port off could cut TapTap off (or customers off the Internet) — '' when safe."""
    try:
        snap = router.config_snapshot
    except Exception:
        snap = None
    wans = {l.get('interface') for l in ((snap.load_balancing if snap else {}) or {}).get('wan_links', []) if l.get('interface')}
    if iface in wans:
        return 'wan'
    mgmt = detect_management_ports(svc, router) if svc is not None else None
    if mgmt is None:
        cached = cache.get(f'tt:mgmt:{router.pk}')
        mgmt = set(cached) if cached is not None else None
    if mgmt is not None:
        return 'management' if iface in mgmt else ''
    # Unknown yet: only flag a port that carries the router's address itself (not merely its bridge).
    host = str(router.ip_address or '').split(':')[0]
    rows = ((snap.sections or {}).get('IP addresses') or {}).get('rows', []) if snap and snap.sections else []
    for r in rows:
        if str(r.get('interface', '')) == iface and str(r.get('address', '')).split('/')[0] == host:
            return 'management'
    return ''


RISK_TEXT = {'wan': 'This port carries Internet traffic (a WAN link). Turning it off cuts customers off the Internet.',
             'management': 'TapTap reaches this router through this port. If you turn it off, TapTap loses the router until the port comes back on.'}


# ─────────────────────────── router clock & scheduler ───────────────────────────
def router_now(svc):
    """The router's own clock (scheduler times are in router time)."""
    row = (svc.safe_get('/system/clock') or [{}])[0]
    t = str(row.get('time', '00:00:00'))
    d = str(row.get('date', ''))
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            return datetime.strptime(f'{d} {t}', '%Y-%m-%d %H:%M:%S'), 'iso'
        mon, day, year = d.split('/')
        return datetime(int(year), MONTHS.index(mon.lower()[:3]) + 1, int(day), *map(int, t.split(':'))), 'mdy'
    except Exception:
        return datetime.now(), 'iso'


def schedule_on_router(svc, name, delay_seconds, script):
    """One-shot job that runs `script` on the router after delay_seconds, then deletes itself."""
    now, fmt = router_now(svc)
    when = now + timedelta(seconds=max(5, int(delay_seconds)))
    date = when.strftime('%Y-%m-%d') if fmt == 'iso' else f'{MONTHS[when.month - 1]}/{when.day:02d}/{when.year}'
    res = svc.resource('/system/scheduler')
    for old in res.get(name=name):
        res.remove(id=old.get('id') or old.get('.id'))
    res.add(name=name, start_date=date, start_time=when.strftime('%H:%M:%S'), interval='0s',
            on_event=f'{script}; /system scheduler remove [find name="{name}"]', comment='TapTap automatic restore')
    return name


def cancel_on_router(svc, name):
    if not name:
        return
    try:
        res = svc.resource('/system/scheduler')
        for old in res.get(name=name):
            res.remove(id=old.get('id') or old.get('.id'))
    except Exception as exc:
        logger.info('cancel scheduler %s: %s', name, exc)


# ─────────────────────────── port power ───────────────────────────
def _iface_id(svc, iface):
    rows = svc.resource('/interface').get(name=iface)
    if not rows:
        raise ValueError(f'{iface} does not exist on the router.')
    return rows[0].get('id') or rows[0].get('.id'), rows[0]


def set_port(svc, iface, enabled):
    item, _ = _iface_id(svc, iface)
    svc.resource('/interface').set(id=item, disabled='no' if enabled else 'yes')


def restart_port(svc, iface, seconds=5):
    """Off for a few seconds, then on. A router-side job turns it back on even if this connection drops."""
    safety = f'{SCHED_PREFIX}restart-{iface}'
    schedule_on_router(svc, safety, seconds + 25, f'/interface enable [find name="{iface}"]')
    set_port(svc, iface, False)
    time.sleep(max(1, min(30, seconds)))
    set_port(svc, iface, True)
    cancel_on_router(svc, safety)


def turn_off_for(svc, router, iface, minutes, user=None):
    name = f'{SCHED_PREFIX}on-{iface}'
    schedule_on_router(svc, name, minutes * 60, f'/interface enable [find name="{iface}"]')
    set_port(svc, iface, False)
    PortRule.objects.filter(router=router, interface=iface, kind='timed_off').delete()
    return PortRule.objects.create(router=router, interface=iface, kind='timed_off', active=True, triggered_at=timezone.now(),
                                   restore_at=timezone.now() + timedelta(minutes=minutes), scheduler_name=name, created_by=user)


# ─────────────────────────── speed limits ───────────────────────────
def queue_name(iface, suffix='limit'):
    return f'TapTap {suffix} {iface}'


def apply_queue(svc, iface, down_mbps, up_mbps, name):
    """Simple queue on the port. For a port, 'download' is traffic going out to the devices on it."""
    res = svc.resource('/queue/simple')
    limit = f'{_mbps(up_mbps)}/{_mbps(down_mbps)}'
    existing = res.get(name=name)
    if existing:
        res.set(id=existing[0].get('id') or existing[0].get('.id'), target=iface, max_limit=limit, disabled='no')
    else:
        res.add(name=name, target=iface, max_limit=limit, comment='Managed by TapTap')


def remove_queue(svc, name):
    res = svc.resource('/queue/simple')
    for q in res.get(name=name):
        res.remove(id=q.get('id') or q.get('.id'))


def set_limit(svc, router, iface, down, up, minutes=0, user=None):
    name = queue_name(iface)
    apply_queue(svc, iface, down, up, name)
    rule, _ = PortRule.objects.update_or_create(router=router, interface=iface, kind='limit', defaults={
        'limit_down_mbps': down, 'limit_up_mbps': up, 'active': True, 'enabled': True, 'queue_name': name,
        'triggered_at': timezone.now(), 'restore_at': timezone.now() + timedelta(minutes=minutes) if minutes else None,
        'created_by': user, 'last_error': ''})
    sched = f'{SCHED_PREFIX}unlimit-{iface}'
    if minutes:
        schedule_on_router(svc, sched, minutes * 60, f'/queue simple remove [find name="{name}"]')
        rule.scheduler_name = sched; rule.save(update_fields=['scheduler_name'])
    else:
        cancel_on_router(svc, sched)
    return rule


def clear_limit(svc, router, iface):
    remove_queue(svc, queue_name(iface))
    cancel_on_router(svc, f'{SCHED_PREFIX}unlimit-{iface}')
    PortRule.objects.filter(router=router, interface=iface, kind='limit').delete()


# ─────────────────────────── traffic guard (runs every live pass) ───────────────────────────
def tick(router, svc, now=None):
    """Evaluate guards, finish timed actions, run the nightly backup. Returns a small summary."""
    from .live import push_event
    now = now or timezone.now()
    out = {'guards': 0, 'triggered': 0, 'restored': 0}
    if cache.add(f'tt:mgmt:refresh:{router.pk}', 1, 600):
        detect_management_ports(svc, router)
    rules = list(PortRule.objects.filter(router=router, enabled=True))
    if rules:
        ifaces = {r.interface for r in rules}
        key = f'tt:guard:ctr:{router.pk}'
        prev = cache.get(key) or {}
        cur, rates = {}, {}
        ts = now.timestamp()
        for row in svc.interfaces():
            n = str(row.get('name', ''))
            if n in ifaces:
                cur[n] = (_int(row.get('rx-byte')), _int(row.get('tx-byte')), ts, str(row.get('disabled', '')).lower() == 'true')
                p = prev.get(n)
                if p and 2 <= ts - p[2] <= 900 and cur[n][0] >= p[0] and cur[n][1] >= p[1]:
                    dt = ts - p[2]
                    # On a LAN port the router SENDS the devices' downloads (tx) and RECEIVES their uploads (rx).
                    rates[n] = {'down': (cur[n][1] - p[1]) * 8 / dt, 'up': (cur[n][0] - p[0]) * 8 / dt}
        cache.set(key, cur, 3600)

        for r in rules:
            try:
                if r.kind == 'guard':
                    out['guards'] += 1
                    if r.active:
                        if r.restore_at and now >= r.restore_at:
                            if r.action == 'throttle':
                                remove_queue(svc, queue_name(r.interface, 'guard'))
                            else:
                                set_port(svc, r.interface, True)
                            cancel_on_router(svc, r.scheduler_name)
                            r.active, r.restore_at = False, None
                            r.save(update_fields=['active', 'restore_at'])
                            cache.delete(f'tt:guard:over:{r.pk}')
                            out['restored'] += 1
                            push_event(router.business_id, f'Traffic guard on {router.name} {r.interface}: back to normal')
                        continue
                    rt = rates.get(r.interface)
                    if not rt:
                        continue
                    speed = max(rt['down'], rt['up']) if r.direction == 'any' else rt[r.direction]
                    okey = f'tt:guard:over:{r.pk}'
                    if speed >= r.threshold_mbps * 1e6:
                        since = cache.get(okey) or ts
                        cache.set(okey, since, 3600)
                        if ts - since >= r.sustain_seconds:
                            _trigger_guard(svc, router, r, speed, now)
                            out['triggered'] += 1
                    else:
                        cache.delete(okey)
                elif r.kind == 'timed_off' and r.restore_at and now >= r.restore_at + timedelta(seconds=20):
                    # The router's own scheduler should have switched it back on; make sure.
                    if cur.get(r.interface, (0, 0, 0, False))[3]:
                        set_port(svc, r.interface, True)
                    push_event(router.business_id, f'{router.name} {r.interface} is back on after a timed shutdown', 'good')
                    r.delete(); out['restored'] += 1
                elif r.kind == 'limit' and r.restore_at and now >= r.restore_at:
                    remove_queue(svc, r.queue_name or queue_name(r.interface))
                    cancel_on_router(svc, r.scheduler_name)
                    push_event(router.business_id, f'Speed limit on {router.name} {r.interface} ended')
                    r.delete(); out['restored'] += 1
            except Exception as exc:
                PortRule.objects.filter(pk=r.pk).update(last_error=str(exc)[:255])
                logger.info('port rule %s: %s', r.pk, exc)

    # Nightly backup between 02:00 and 04:59 local time.
    local = timezone.localtime(now)
    if router.auto_backup and 2 <= local.hour < 5 and (not router.last_backup_at or now - router.last_backup_at > timedelta(hours=20)):
        if cache.add(f'tt:autobackup:{router.pk}', 1, 3600):
            try:
                backup(svc, router, automatic=True)
            except Exception as exc:
                logger.info('auto backup %s: %s', router, exc)
    return out


def _trigger_guard(svc, router, r, speed, now):
    from .live import push_event
    risk = port_risk(router, r.interface, svc)
    action = 'throttle' if (r.action == 'shutdown' and risk) else r.action   # never switch off WAN/management
    sched = f'{SCHED_PREFIX}guard-{r.interface}'
    if action == 'throttle':
        qname = queue_name(r.interface, 'guard')
        apply_queue(svc, r.interface, r.throttle_mbps, r.throttle_mbps, qname)
        schedule_on_router(svc, sched, r.hold_minutes * 60 + 30, f'/queue simple remove [find name="{qname}"]')
        what = f'slowed to {r.throttle_mbps:g} Mb/s'
    else:
        schedule_on_router(svc, sched, r.hold_minutes * 60, f'/interface enable [find name="{r.interface}"]')
        set_port(svc, r.interface, False)
        what = 'switched off'
    r.active, r.triggered_at, r.restore_at, r.scheduler_name = True, now, now + timedelta(minutes=r.hold_minutes), sched
    r.times_triggered += 1; r.last_error = ''
    r.save(update_fields=['active', 'triggered_at', 'restore_at', 'scheduler_name', 'times_triggered', 'last_error'])
    push_event(router.business_id, f'Traffic guard: {router.name} {r.interface} reached {speed / 1e6:.1f} Mb/s — {what} for {r.hold_minutes} min', 'bad')
    from .notify import notify
    notify(router.business, 'traffic_guard', f'Traffic guard on {router.name} {r.interface}',
           f'{r.interface} reached {speed / 1e6:.1f} Mb/s and was {what} for {r.hold_minutes} minutes. It is restored automatically.',
           link='/topology/', key=f'guard:{r.pk}')


# ─────────────────────────── reboot & backup ───────────────────────────
def reboot(svc):
    try:
        svc.resource('/system').call('reboot')
    except Exception as exc:
        # The router drops the connection as it goes down — that is success.
        if not re.search(r'closed|reset|timed out|broken|eof|connection', str(exc), re.I):
            raise


def _read_file(svc, fname, size):
    """Download a text file from the router: /file/read (RouterOS 7.13+), else the 'contents' field (small files)."""
    chunks, offset = [], 0
    try:
        res = svc.resource('/file')
        while offset < max(size, 1) and offset < 8_000_000:
            rows = res.call('read', {'file': fname, 'chunk-size': '32768', 'offset': str(offset)})
            data = ''.join(str(r.get('data', '')) for r in rows or [])
            if not data:
                break
            chunks.append(data); offset += len(data.encode('utf-8', 'ignore'))
        if chunks:
            return ''.join(chunks)
    except Exception:
        pass
    for row in svc.safe_get('/file'):
        if str(row.get('name', '')) == fname and row.get('contents'):
            text = str(row.get('contents'))
            if len(text.encode()) >= size * 0.95 or size == 0:
                return text
    return ''


def backup(svc, router, user=None, automatic=False):
    """Binary backup + text export saved on the router; the export is also downloaded into TapTap when possible."""
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
    base = re.sub(r'[^A-Za-z0-9_-]', '-', f'taptap-{router.name}-{stamp}')[:60]
    rec = RouterBackup(router=router, name=base, automatic=automatic, created_by=user)
    try:
        rec.ros_version = str((svc.safe_get('/system/resource') or [{}])[0].get('version', ''))[:60]
    except Exception:
        pass
    errors = []
    try:
        try:
            svc.resource('/system/backup').call('save', {'name': base, 'dont-encrypt': 'yes'})
        except Exception:
            svc.resource('/system/backup').call('save', {'name': base})
        rec.backup_file = base + '.backup'
    except Exception as exc:
        errors.append(f'backup: {exc}')
    # Text export through a temporary script (works on RouterOS 6 and 7).
    scripts = svc.resource('/system/script')
    sname = f'{SCHED_PREFIX}export'
    try:
        for s in scripts.get(name=sname):
            scripts.remove(id=s.get('id') or s.get('.id'))
        scripts.add(name=sname, source=f'/export file={base}')
        sid = (scripts.get(name=sname) or [{}])[0]
        scripts.call('run', {'.id': sid.get('id') or sid.get('.id') or sname})
        fname, size = base + '.rsc', 0
        for _ in range(20):
            rows = [r for r in svc.safe_get('/file') if str(r.get('name', '')) == fname]
            if rows and _int(rows[0].get('size')) > 0:
                size = _int(rows[0].get('size')); break
            time.sleep(0.5)
        if size:
            rec.export_file, rec.export_size = fname, size
            rec.content = _read_file(svc, fname, size)
        else:
            errors.append('export: the router did not create the export file in time')
    except Exception as exc:
        errors.append(f'export: {exc}')
    finally:
        try:
            for s in scripts.get(name=sname):
                scripts.remove(id=s.get('id') or s.get('.id'))
        except Exception:
            pass
    rec.error = '; '.join(errors)[:255]
    rec.save()
    from .notify import notify
    ok = bool(rec.backup_file or rec.export_file)
    notify(router.business, 'backup_done' if ok else 'backup_failed', f'Backup {"saved" if ok else "failed"} on {router.name}',
           (f'{rec.backup_file or "no .backup"} and {rec.export_file or "no export"} saved on the router' + ('; the export is also stored in TapTap.' if rec.content else '.'))
           if ok else f'The backup could not be saved: {rec.error}', link='/topology/')
    type(router).objects.filter(pk=router.pk).update(last_backup_at=timezone.now())
    # keep the latest 30 per router
    old = list(router.backups.order_by('-created_at').values_list('pk', flat=True)[30:])
    if old:
        RouterBackup.objects.filter(pk__in=old).delete()
    return rec
