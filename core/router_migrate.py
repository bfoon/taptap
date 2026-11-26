"""Router migration — bring what one MikroTik holds to another one, piece by piece.

Pieces (the owner ticks what to bring):

* ``profiles``      hotspot user profiles (created on the new router when it does not have them; its own stay)
* ``vouchers``      TapTap vouchers — MOVED: they belong to the new router from now on, keep their codes, their time
                    used and their end; the new router gets each one with exactly the time left
* ``members``       members, the same way
* ``router_users``  users made on the router itself (Mikhmon, WinBox) — copied with what is left of their limit,
                    and TapTap follows them on the new router
* ``bindings``      IP bindings (bypassed / blocked devices) — copied
* ``login_page``    the TapTap login page, installed on the new router
* ``sales``         Market batches and the agent programme that send new vouchers to the old router now send them
                    to the new one

The whole configuration (interfaces, addresses, DHCP, firewall…) travels a different way: a backup of the old
router restored on the new one (core/router_restore.py — the page drives its existing endpoints), or chosen
sections of its export as a script that skips what the new router already has (``sections_script``).

A TapTap voucher can only belong to one router, so vouchers and members are always moved, never copied. To keep
a second router holding them as a live backup, pair the two as twins (core/router_twin.py).
"""
from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone

from .models_router_twin import RouterMigration

logger = logging.getLogger('taptap.migrate')

PARTS = [
    ('profiles', 'Hotspot user profiles', 'bi-sliders', 'Speed, shared users — created on the new router when it does not have them.'),
    ('vouchers', 'TapTap vouchers', 'bi-ticket-perforated', 'Moved with their codes, time used and end time. Customers keep logging in.'),
    ('members', 'Members', 'bi-person-badge', 'Moved with their passwords and their time.'),
    ('router_users', 'Users made on the router', 'bi-person-gear', 'Mikhmon / WinBox users, with what is left of their time.'),
    ('bindings', 'IP bindings', 'bi-shield-lock', 'Bypassed and blocked devices.'),
    ('login_page', 'Login page', 'bi-window', 'Your TapTap login page, installed on the new router.'),
    ('sales', 'Where new vouchers go', 'bi-signpost-split', 'Market batches and the agent programme send new vouchers to the new router.'),
]
PART_KEYS = [p[0] for p in PARTS]
DEFAULT_PARTS = ['profiles', 'vouchers', 'members', 'router_users', 'bindings']

# Export sections that are usually safe to bring to another router, and those that describe THIS box.
SAFE_SECTIONS = ('/ip hotspot user profile', '/ip hotspot profile', '/ip pool', '/ip dhcp-server network', '/queue simple',
                 '/queue type', '/queue tree', '/ip firewall filter', '/ip firewall nat', '/ip firewall mangle',
                 '/ip firewall address-list', '/ip firewall layer7-protocol', '/ip dns static', '/ip hotspot walled-garden',
                 '/ip hotspot walled-garden ip', '/system scheduler', '/system script', '/ip hotspot ip-binding',
                 '/ip hotspot user', '/ip dns', '/system ntp client', '/system clock')
RISKY_PREFIXES = ('/interface', '/ip address', '/ip route', '/routing', '/system identity', '/user', '/certificate',
                  '/ip service', '/ip cloud', '/system routerboard', '/ip dhcp-client', '/ppp', '/ipv6', '/caps-man',
                  '/system license', '/disk', '/container', '/ip ipsec')
RISKY_EXACT = ('/ip hotspot', '/ip dhcp-server', '/ip neighbor discovery-settings')
SECTION_RE = re.compile(r'^/[a-z0-9][a-z0-9 /_-]{0,80}$')


class MigrateError(ValueError):
    pass


def _routers(business, source_id, target_id):
    try:
        source = business.routers.get(pk=int(source_id))
        target = business.routers.get(pk=int(target_id))
    except Exception:
        raise MigrateError('Choose the router to move from and the router to move to.')
    if source.pk == target.pk:
        raise MigrateError('Choose two different routers.')
    return source, target


def _event(m, text, level='info'):
    m.log = (m.log or []) + [{'at': timezone.now().isoformat(), 'text': str(text)[:300], 'level': level}]


# ─────────────────────────────── what would move ───────────────────────────────

def _router_users(source):
    """(rows ready for another router, skipped [(name, why)]) — users made on the router itself."""
    from .live import SKIP_USERS
    from .models import RouterHotspotUser
    from .router_twin import codes_lookup, router_user_row
    rows, skipped = [], []
    users = list(RouterHotspotUser.objects.filter(router=source, is_present=True).exclude(username__in=SKIP_USERS))
    known = codes_lookup(source.business, [u.username for u in users])
    for m in users:
        rid, src = known.get(m.username.upper(), (None, 'mikrotik'))
        if src == 'taptap' or rid not in (None, source.pk) or str(m.comment or '').startswith('TapTap-roam:'):
            continue
        row, why = router_user_row(m)
        if row is None:
            skipped.append((m.username, why))
        else:
            row['c'] = (m.comment or 'Router user')[:120]
            rows.append(row)
    return rows, skipped


def _profiles(source):
    from .models import RouterHotspotProfile
    from .router_time import _profile_shared
    from .router_twin import PROFILE_RE, RATE_RE
    out = []
    for p in RouterHotspotProfile.objects.filter(router=source, is_present=True).exclude(name='default'):
        if not PROFILE_RE.match(p.name or ''):
            continue
        rate = (p.rate_limit or '').strip()
        out.append({'name': p.name, 'shared': _profile_shared(p), 'rate': rate if (not rate or RATE_RE.match(rate)) else ''})
    return out


def _bindings(source):
    from .models import SyncedIPBinding
    out = []
    for b in SyncedIPBinding.objects.filter(router=source, is_present=True):
        mac, addr = str(b.mac_address or '').strip().upper(), str(b.address or '').strip()
        if not mac and not addr:
            continue
        btype = b.binding_type if b.binding_type in ('bypassed', 'regular', 'blocked') else 'bypassed'
        out.append((mac or f'ip:{addr}', {'mac': mac, 'address': addr, 'type': btype, 'comment': (b.comment or 'TapTap')[:120], 'disabled': bool(b.disabled)}))
    return out


def _voucher_qs(source, members):
    from .models import Voucher
    qs = Voucher.objects.filter(router=source, source='taptap')
    return qs.filter(login_type='member') if members else qs.exclude(login_type='member')


def _sales_links(source):
    out = []
    try:
        from .models import MarketListing
        out += [('Market batch', x) for x in MarketListing.objects.filter(router=source)]
    except Exception:
        pass
    try:
        from .models import AgentProgram
        out += [('Agent programme', x) for x in AgentProgram.objects.filter(router=source)]
    except Exception:
        pass
    return out


def preview(business, source_id, target_id):
    """Counts per piece and the warnings to read before starting."""
    from .router_twin import on_link, twin_of
    source, target = _routers(business, source_id, target_id)
    v = _voucher_qs(source, members=False)
    mem = _voucher_qs(source, members=True)
    live = ('active', 'disabled')
    users, skipped = _router_users(source)
    counts = {
        'profiles': len(_profiles(source)),
        'vouchers': v.count(), 'vouchers_live': v.filter(status__in=live).count(),
        'vouchers_in_use': v.filter(status='active', used_at__isnull=False).count(),
        'members': mem.count(), 'members_live': mem.filter(status__in=live).count(),
        'router_users': len(users), 'router_users_skipped': len(skipped),
        'bindings': len(_bindings(source)),
        'sales': len(_sales_links(source)),
        'login_page': 1,
    }
    warnings = []
    if str(target.status).lower() != 'online':
        warnings.append(('warn', f'{target.name} is {target.status.lower()} — TapTap sends everything as soon as it answers.'))
    if counts['vouchers_in_use']:
        warnings.append(('info', f'{counts["vouchers_in_use"]} voucher(s) are in use right now. Their customers keep their time: '
                                 f'on {target.name} they get exactly what is left.'))
    if skipped:
        warnings.append(('warn', f'{len(skipped)} router-made user(s) cannot be copied: TapTap does not know their password '
                                 f'(e.g. {skipped[0][0]}). Sync {source.name} over the API, or move them by hand.'))
    seen_twins = set()
    for r in (source, target):
        t = twin_of(r)
        if t is None or t.pk in seen_twins:
            continue
        seen_twins.add(t.pk)
        if {t.primary_id, t.standby_id} == {source.pk, target.pk}:
            warnings.append(('info', f'{source.name} and {target.name} are twins. To make the standby the main router for good, '
                                     'use Twins › Swap roles — it moves the vouchers and keeps the pair.'))
        else:
            warnings.append(('info', f'{r.name} is part of a twin ({t.primary.name} ⇄ {t.standby.name}): the standby keeps copies of '
                                     'what stays on the main router only.'))
    if on_link(target):
        warnings.append(('info', f'{target.name} is on TapTap Link: everything goes in safe batches at its check-ins (a few minutes for thousands).'))
    return {'source': {'id': source.pk, 'name': source.name, 'status': source.status},
            'target': {'id': target.pk, 'name': target.name, 'status': target.status},
            'counts': counts, 'warnings': warnings, 'skipped': skipped[:20],
            'parts': [{'key': k, 'label': l, 'icon': i, 'help': h} for k, l, i, h in PARTS]}


# ─────────────────────────────── doing it ───────────────────────────────

def start(business, source_id, target_id, parts, clean_source=False, user=None):
    source, target = _routers(business, source_id, target_id)
    parts = [p for p in PART_KEYS if p in set(parts or [])]
    if not parts:
        raise MigrateError('Tick at least one thing to bring over.')
    if RouterMigration.objects.filter(business=business, status='running', target=target).exists():
        raise MigrateError(f'A migration to {target.name} is already running.')
    m = RouterMigration.objects.create(business=business, source=source, target=target, source_name=source.name, target_name=target.name,
                                       parts=parts, clean_source=bool(clean_source), created_by=user)
    from .utils import log
    log(business, 'Router Migration', f'{source.name} → {target.name}: {", ".join(parts)}', router=target)
    try:
        from django.conf import settings
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise RuntimeError('eager')
        from .tasks import router_migration_task
        router_migration_task.delay(m.pk)
    except Exception:
        run(m.pk)
    return m


def move_vouchers(source, target, user=None, members=True, vouchers=True, codes_out=None):
    """TapTap vouchers/members of ``source`` belong to ``target`` from now on. Returns how many moved.
    Those that may be online are sent to ``target`` at once (fast lane), the rest at its next syncs."""
    from .models import Voucher
    qs = Voucher.objects.filter(router=source, source='taptap')
    if not members:
        qs = qs.exclude(login_type='member')
    if not vouchers:
        qs = qs.filter(login_type='member')
    ids = list(qs.values_list('pk', flat=True))
    if not ids:
        return 0
    live_rows = list(Voucher.objects.filter(pk__in=ids, status__in=('active', 'disabled')).values_list('pk', 'code'))
    live = [pk for pk, _c in live_rows]
    if codes_out is not None:
        codes_out.extend(c for _pk, c in live_rows)
    with transaction.atomic():
        Voucher.objects.filter(pk__in=live).update(router=target, mikrotik_sync_status='Pending', mikrotik_sync_error='', mikrotik_id='')
        Voucher.objects.filter(pk__in=ids).exclude(pk__in=live).update(router=target)
    try:
        from .voucher_fastlane import sync_now
        for i in range(0, len(live), 500):
            sync_now(live[i:i + 500], user=user, force=True)
    except Exception:
        logger.exception('migrate: first push to %s', target)
    return len(ids)


def run(migration_id):
    """Carry out a migration (worker, or inline without one). Each piece on its own: one that fails is logged and
    the others still happen."""
    from .router_twin import Carrier
    m = RouterMigration.objects.select_related('source', 'target', 'business').get(pk=migration_id)
    if m.status != 'running':
        return m
    source, target, parts = m.source, m.target, list(m.parts or [])
    if source is None or target is None:
        m.status, m.error, m.finished_at = 'failed', 'One of the routers was deleted.', timezone.now()
        m.save()
        return m
    state = {'counts': {}, 'problems': 0, 'moved': 0, 'codes': []}
    carrier = Carrier(target, user=m.created_by, tag={'migration_id': m.pk})
    needs_router = [p for p in parts if p in ('profiles', 'router_users', 'bindings')]
    opened = False
    if needs_router:
        try:
            carrier.__enter__()
            opened = True
        except Exception as exc:
            _event(m, f'{target.name} could not be reached ({exc}). Not copied: {", ".join(needs_router)} — '
                      'start the migration again for those when it is online.', 'bad')
            state['problems'] += 1
            parts = [p for p in parts if p not in needs_router]
    try:
        for part in parts:
            try:
                STEPS[part](m, source, target, carrier, state)
            except Exception as exc:
                logger.exception('migration %s: %s', m.pk, part)
                _event(m, f'{dict((k, l) for k, l, _i, _h in PARTS).get(part, part)}: stopped — {exc}', 'bad')
                state['problems'] += 1
        if m.clean_source and state['codes']:
            _event(m, _clean(source, m, state['codes']), 'info')
    finally:
        if opened:
            carrier.__exit__(None, None, None)
    m.status = 'partly' if state['problems'] else 'done'
    m.counts, m.finished_at = state['counts'], timezone.now()
    m.save()
    return m


def _step_profiles(m, source, target, carrier, state):
    profs = _profiles(source)
    if carrier.link:
        from .linkops import send
        from .router_twin import KIND, link_users_params
        for i in range(0, len(profs), 40):
            send(target, KIND, link_users_params([], profs[i:i + 40], target, {'migration_id': m.pk}),
                 label=f'Migration: {len(profs[i:i + 40])} profile(s) from {source.name}', user=m.created_by, minutes=60 * 12)
        state['counts']['profiles'] = len(profs)
        _event(m, f'{len(profs)} profile(s) queued for {target.name} (TapTap Link).', 'ok')
        return
    res = carrier.profiles(profs, only_missing=True)
    bad = [k for k, e in res.items() if e]
    state['counts']['profiles'] = len(res) - len(bad)
    _event(m, f'{state["counts"]["profiles"]} profile(s) on {target.name}' + (f'; not saved: {", ".join(bad[:5])}' if bad else '.'),
           'warn' if bad else 'ok')
    state['problems'] += bool(bad)


def _step_vouchers(m, source, target, carrier, state):
    n = move_vouchers(source, target, user=m.created_by, members=False, vouchers=True, codes_out=state['codes'])
    state['counts']['vouchers'] = n
    state['moved'] += n
    _event(m, f'{n} voucher(s) now belong to {target.name}; those still valid are being sent to it.', 'ok')


def _step_members(m, source, target, carrier, state):
    n = move_vouchers(source, target, user=m.created_by, members=True, vouchers=False, codes_out=state['codes'])
    state['counts']['members'] = n
    state['moved'] += n
    _event(m, f'{n} member(s) now belong to {target.name}.', 'ok')


def _step_router_users(m, source, target, carrier, state):
    from .models import Voucher
    rows, skipped = _router_users(source)
    profs = _profiles(source)
    names = []
    if carrier.link:
        for cmd, keys in carrier.users(rows, profs, label=f'Migration from {source.name}'):
            if cmd is None:
                _event(m, f'{len(keys)} router user(s) with an empty password can only be copied over the API: {", ".join(keys[:5])}', 'warn')
                state['problems'] += 1
            else:
                names += keys
    else:
        res = carrier.users(rows, profs)
        names = [k for k, e in res.items() if not e]
        bad = [k for k, e in res.items() if e]
        if bad:
            _event(m, f'{len(bad)} router user(s) not saved on {target.name}: {", ".join(bad[:5])}', 'warn')
            state['problems'] += 1
    if names:      # TapTap follows them on the new router
        wanted = {n.upper() for n in names}
        ids = [pk for pk, code in Voucher.objects.filter(router=source, source='mikrotik').values_list('pk', 'code') if code.upper() in wanted]
        Voucher.objects.filter(pk__in=ids).update(router=target)
    state['counts']['router_users'] = len(names)
    if skipped:
        _event(m, f'{len(skipped)} router user(s) skipped: TapTap does not know their password.', 'warn')
        state['problems'] += 1
    _event(m, f'{len(names)} router-made user(s) copied to {target.name}.', 'ok')


def _step_bindings(m, source, target, carrier, state):
    rows = _bindings(source)
    res = carrier.bindings(rows, label=f'Migration from {source.name}')
    bad = [k for k, r in res.items() if isinstance(r, str) and r]
    state['counts']['bindings'] = len(res) - len(bad)
    _event(m, f'{state["counts"]["bindings"]} IP binding(s) copied to {target.name}' + (f'; not copied: {len(bad)} ({res[bad[0]]})' if bad else '.'),
           'warn' if bad else 'ok')
    state['problems'] += bool(bad)


def _step_login_page(m, source, target, carrier, state):
    from .portal_deploy import deploy
    ok, msg = deploy(target, user=m.created_by)
    state['counts']['login_page'] = int(bool(ok))
    _event(m, f'Login page: {msg}', 'ok' if ok else 'warn')
    state['problems'] += not ok


def _step_sales(m, source, target, carrier, state):
    links = _sales_links(source)
    for _label, obj in links:
        type(obj).objects.filter(pk=obj.pk).update(router=target)
    state['counts']['sales'] = len(links)
    _event(m, f'{len(links)} sales setting(s) now send new vouchers to {target.name}.' if links
           else 'No sales setting pointed at the old router.', 'ok')


STEPS = {'profiles': _step_profiles, 'vouchers': _step_vouchers, 'members': _step_members, 'router_users': _step_router_users,
         'bindings': _step_bindings, 'login_page': _step_login_page, 'sales': _step_sales}


def _clean(source, m, codes):
    """Take the vouchers/members that moved off the old router."""
    from .router_twin import Carrier
    if not codes:
        return 'Nothing to take off the old router.'
    try:
        with Carrier(source, user=m.created_by, tag={'migration_id': m.pk}) as c:
            for i in range(0, len(codes), 100):
                c.remove_users(codes[i:i + 100], label=f'Migration to {m.target_name}: moved away')
        return f'{len(codes)} moved code(s) are being taken off {source.name}.'
    except Exception as exc:
        return f'{source.name} could not be reached ({exc}): the moved codes stay on it until you remove them.'


def link_ack(cmd, ok):
    """A migration command was answered by a TapTap Link router."""
    m = RouterMigration.objects.filter(pk=(cmd.params or {}).get('migration_id')).first()
    if m is None or ok:
        return
    _event(m, f'{cmd.label}: the router reported an error.', 'warn')
    m.save(update_fields=['log'])


def as_json(m):
    return {'id': m.pk, 'status': m.status, 'status_label': m.get_status_display(), 'source': m.source_name, 'target': m.target_name,
            'parts': m.parts, 'counts': m.counts, 'log': m.log, 'error': m.error,
            'created_at': m.created_at.isoformat(), 'finished_at': m.finished_at.isoformat() if m.finished_at else None}


# ─────────────────────────────── configuration ───────────────────────────────

def config_backups(router, limit=8):
    """Backups of ``router`` kept on the TapTap server that a restore can use."""
    from .models import RouterBackup
    out = []
    for b in RouterBackup.objects.filter(router=router).order_by('-created_at')[:30]:
        try:
            st = b.server_storage
        except Exception:
            st = None
        if st is None or st.status not in ('ready', 'partial'):
            continue
        out.append({'id': b.pk, 'name': b.name, 'at': b.created_at.isoformat(), 'ros': b.ros_version,
                    'backup_size': st.backup_size if st.backup_path else 0, 'export_size': st.server_export_size if st.export_path else 0,
                    'has_backup': bool(st.backup_path), 'has_export': bool(st.export_path or b.content)})
        if len(out) >= limit:
            break
    return out


def export_text(backup):
    """The .rsc export text of a backup (server copy, else what TapTap read when it was made)."""
    try:
        st = backup.server_storage
    except Exception:
        st = None
    if st is not None and st.export_path:
        try:
            from .backup_server import _full_path
            return _full_path(st.export_path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            logger.info('export of backup %s not readable', backup.pk, exc_info=True)
    return backup.content or ''


def sections(text):
    """[(path, [command lines])] of a RouterOS export; continuation lines joined."""
    out, cur, buf = [], None, ''
    for raw in str(text or '').splitlines():
        line = raw.rstrip()
        if buf:
            line = buf + line.lstrip()
            buf = ''
        if line.endswith('\\'):
            buf = line[:-1]
            continue
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('/'):
            head, _, rest = s.partition(' ')
            words = [head]
            for w in rest.split(' '):
                if w and '=' not in w and not w.startswith(('[', '"')) and w not in ('add', 'set', 'remove', 'print') and re.match(r'^[a-z0-9-]+$', w):
                    words.append(w)
                else:
                    break
            path = ' '.join(words)
            tail = s[len(path):].strip()
            cur = (path, [])
            out.append(cur)
            if tail:
                cur[1].append(tail)
            continue
        if cur is not None:
            cur[1].append(s)
    merged = {}
    order = []
    for path, lines in out:
        if path not in merged:
            merged[path] = []
            order.append(path)
        merged[path] += lines
    return [(p, merged[p]) for p in order]


def section_list(text):
    rows = []
    for path, lines in sections(text):
        risky = path in RISKY_EXACT or any(path == r or path.startswith(r + ' ') for r in RISKY_PREFIXES)
        safe = path in SAFE_SECTIONS
        rows.append({'path': path, 'count': len(lines), 'risky': risky and not safe, 'suggest': safe})
    return rows


def sections_script(text, chosen, source_name='', target_name='', skip_errors=True):
    """A .rsc with only the chosen sections, every command made absolute. With ``skip_errors`` each one runs on
    its own (``:do {…} on-error``), so an entry the new router already has does not stop the rest."""
    chosen = [c for c in chosen if SECTION_RE.match(c)]
    want = set(chosen)
    head = [f'# TapTap migration script: chosen sections of {source_name or "a router"}'
            + (f' for {target_name}' if target_name else ''),
            f'# made {timezone.localtime().strftime("%Y-%m-%d %H:%M")} — upload it in WinBox › Files, then: /import file-name=<this file>',
            '# Every command runs on its own: one that fails (already there, unknown interface) is logged and skipped.' if skip_errors
            else '# The import stops at the first command that fails.']
    body, n = [], 0
    for path, lines in sections(text):
        if path not in want:
            continue
        body.append(f'# {path}')
        for line in lines:
            cmd = f'{path} {line}'
            n += 1
            if skip_errors:
                body.append(f':do {{ {cmd} }} on-error={{ :log warning "TapTap migration: skipped a line of {path}" }}')
            else:
                body.append(cmd)
    if not n:
        raise MigrateError('Tick at least one section that has entries.')
    return '\n'.join(head + body) + '\n', n
